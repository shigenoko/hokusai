"""Step 4: Miro 書き戻し（create_card + dispatcher）のテスト"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hokusai.integrations.design.miro import (
    MiroAPIError,
    MiroClient,
    MiroRateLimitError,
)
from hokusai.integrations.design.writeback import (
    MAX_ATTEMPT_COUNT,
    MiroWritebackArgs,
    MiroWritebackDispatcher,
    OutboxStore,
    WritebackTarget,
)
from hokusai.persistence.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# MiroClient.create_card
# ---------------------------------------------------------------------------


def test_create_card_posts_to_correct_endpoint(monkeypatch):
    """POST /v2/boards/{board_id}/cards が body 付きで呼ばれる"""
    client = MiroClient(api_token="test-token")
    captured: dict[str, object] = {}

    def fake_send(method: str, url: str, *, body=None):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = body
        return {"id": "card-123", "data": body["data"]}

    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(client, "_enforce_rate_limit", lambda: None)

    response = client.create_card(
        "board-abc",
        title="title",
        description="desc",
        position={"x": 100.0, "y": 200.0},
        style={"fillColor": "#4FCC8B"},
    )
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/boards/board-abc/cards")
    assert captured["body"]["data"]["title"] == "title"
    assert captured["body"]["data"]["description"] == "desc"
    assert captured["body"]["position"] == {"x": 100.0, "y": 200.0}
    assert response["id"] == "card-123"


def test_create_card_rejects_empty_inputs():
    client = MiroClient(api_token="t")
    with pytest.raises(ValueError):
        client.create_card("", title="t")
    with pytest.raises(ValueError):
        client.create_card("b", title="")


def test_create_card_minimal_body(monkeypatch):
    """description / position / style 省略時は title のみが data に入る"""
    client = MiroClient(api_token="t")
    captured: dict[str, object] = {}

    def fake_send(method: str, url: str, *, body=None):
        captured["body"] = body
        return {"id": "x"}

    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(client, "_enforce_rate_limit", lambda: None)

    client.create_card("b", title="t")
    assert captured["body"] == {"data": {"title": "t"}}


# ---------------------------------------------------------------------------
# MiroWritebackDispatcher
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> OutboxStore:
    db = tmp_path / "wf.db"
    SQLiteStore(db)
    return OutboxStore(db, target=WritebackTarget.MIRO)


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock(spec=MiroClient)


def _args(**overrides) -> MiroWritebackArgs:
    defaults = dict(
        workflow_id="wf-1",
        profile_name="company-a",
        event_type="phase8a_completed",
        revision="abc1234",
        board_id="board-abc",
        frame_id="frame-xyz",
        frame_meta={"x": 100.0, "y": 200.0, "width": 300.0},
        mr_url="https://example.com/mr/1",
        commit_sha="abc1234",
    )
    defaults.update(overrides)
    return MiroWritebackArgs(**defaults)


def test_dispatch_success(store, mock_client):
    mock_client.create_card.return_value = {"id": "card-xyz"}
    dispatcher = MiroWritebackDispatcher(mock_client, store)

    result = dispatcher.dispatch(_args())

    assert result["status"] == "delivered"
    assert result["response_id"] == "card-xyz"
    assert store.is_already_delivered(result["idempotency_key"]) is True
    assert len(store.list_outbox()) == 0

    call = mock_client.create_card.call_args
    assert call.args == ("board-abc",)
    assert call.kwargs["title"] == "✅ Phase 8a 完了"
    # 主 frame の右側 50px（浮動小数比較は pytest.approx で行う）
    assert call.kwargs["position"]["x"] == pytest.approx(100.0 + 300.0 + 50.0)
    assert call.kwargs["position"]["y"] == pytest.approx(200.0)
    assert call.kwargs["style"]["fillColor"] == "#4FCC8B"


def test_dispatch_failure_enqueues(store, mock_client):
    mock_client.create_card.side_effect = MiroAPIError(403, "Forbidden")
    dispatcher = MiroWritebackDispatcher(mock_client, store)

    result = dispatcher.dispatch(_args())

    assert result["status"] == "enqueued"
    entries = store.list_outbox()
    assert len(entries) == 1
    assert entries[0].payload["board_id"] == "board-abc"
    assert entries[0].payload["frame_id"] == "frame-xyz"


def test_dispatch_rate_limit_enqueues(store, mock_client):
    mock_client.create_card.side_effect = MiroRateLimitError(60.0)
    dispatcher = MiroWritebackDispatcher(mock_client, store)

    result = dispatcher.dispatch(_args())
    assert result["status"] == "enqueued"
    assert len(store.list_outbox()) == 1


def test_dispatch_skips_already_delivered(store, mock_client):
    mock_client.create_card.return_value = {"id": "first"}
    dispatcher = MiroWritebackDispatcher(mock_client, store)

    r1 = dispatcher.dispatch(_args())
    assert r1["status"] == "delivered"

    r2 = dispatcher.dispatch(_args())
    assert r2["status"] == "skipped"
    assert mock_client.create_card.call_count == 1


def test_retry_increments_count(store, mock_client):
    mock_client.create_card.side_effect = MiroAPIError(500, "ServerError")
    dispatcher = MiroWritebackDispatcher(mock_client, store)

    dispatcher.dispatch(_args())
    outbox = store.list_outbox()[0]
    assert outbox.attempt_count == 0

    dispatcher.retry(outbox.id)
    dispatcher.retry(outbox.id)

    updated = store.get_outbox(outbox.id)
    assert updated is not None
    assert updated.attempt_count == 2


def test_retry_moves_to_errors_at_max(store, mock_client):
    mock_client.create_card.side_effect = MiroAPIError(500, "err")
    dispatcher = MiroWritebackDispatcher(mock_client, store)

    dispatcher.dispatch(_args())
    outbox = store.list_outbox()[0]

    last = None
    for _ in range(MAX_ATTEMPT_COUNT):
        last = dispatcher.retry(outbox.id)

    assert last is not None
    assert last["status"] == "moved_to_errors"
    assert len(store.list_outbox()) == 0
    assert len(store.list_errors()) == 1


def test_dispatcher_requires_miro_target(store, mock_client):
    figma_store = OutboxStore(store.db_path, target=WritebackTarget.FIGMA)
    with pytest.raises(ValueError):
        MiroWritebackDispatcher(mock_client, figma_store)


def test_dispatcher_rejects_unknown_on_failure(store, mock_client):
    """on_failure に未知の値を渡すとエラー"""
    with pytest.raises(ValueError):
        MiroWritebackDispatcher(mock_client, store, on_failure="unknown")


def test_on_failure_warn_enqueues_outbox(store, mock_client):
    """on_failure=warn: 失敗時 outbox に積み status=enqueued（既定動作）"""
    mock_client.create_card.side_effect = MiroAPIError(500, "err")
    dispatcher = MiroWritebackDispatcher(mock_client, store, on_failure="warn")
    result = dispatcher.dispatch(_args())
    assert result["status"] == "enqueued"
    assert len(store.list_outbox()) == 1


def test_on_failure_block_returns_blocked_status(store, mock_client):
    """on_failure=block: 失敗時 outbox に積み status=blocked（呼び出し側が止める）"""
    mock_client.create_card.side_effect = MiroAPIError(500, "err")
    dispatcher = MiroWritebackDispatcher(mock_client, store, on_failure="block")
    result = dispatcher.dispatch(_args())
    assert result["status"] == "blocked"
    assert result["on_failure"] == "block"
    assert len(store.list_outbox()) == 1


def test_on_failure_skip_no_enqueue(store, mock_client):
    """on_failure=skip: 失敗時 outbox にも積まない、status=skipped"""
    mock_client.create_card.side_effect = MiroAPIError(500, "err")
    dispatcher = MiroWritebackDispatcher(mock_client, store, on_failure="skip")
    result = dispatcher.dispatch(_args())
    assert result["status"] == "skipped"
    assert result.get("on_failure") == "skip"
    assert len(store.list_outbox()) == 0
