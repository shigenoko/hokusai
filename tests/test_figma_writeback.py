"""Step 3: Figma 書き戻し（post_comment + dispatcher）のテスト

対象:
- hokusai/integrations/design/figma.py::FigmaClient.post_comment
- hokusai/integrations/design/writeback/templates.py
- hokusai/integrations/design/writeback/figma_writeback.py
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hokusai.integrations.design.figma import (
    FigmaAPIError,
    FigmaClient,
    FigmaRateLimitError,
)
from hokusai.integrations.design.writeback import (
    FigmaWritebackArgs,
    FigmaWritebackDispatcher,
    OutboxStore,
    WritebackTarget,
)
from hokusai.integrations.design.writeback.templates import (
    build_figma_payload,
    build_miro_card_payload,
    render_figma_message,
)
from hokusai.persistence.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# templates.py
# ---------------------------------------------------------------------------


def test_render_figma_message_with_all_fields():
    msg = render_figma_message(
        mr_url="https://gitlab.com/foo/bar/-/merge_requests/123",
        commit_sha="a1b2c3d4e5f6789",
    )
    assert msg == (
        "✅ Phase 8a 完了 / MR: https://gitlab.com/foo/bar/-/merge_requests/123"
        " / commit: a1b2c3d"
    )


def test_render_figma_message_with_missing_fields():
    msg = render_figma_message(mr_url=None, commit_sha=None)
    assert "MR URL 不明" in msg
    assert "commit 不明" in msg


def test_build_figma_payload_default_offset():
    payload = build_figma_payload(
        node_id="node-1",
        node_offset=None,
        mr_url="https://example.com",
        commit_sha="abc1234",
    )
    assert payload["message"].startswith("✅ Phase 8a 完了")
    assert payload["client_meta"]["node_id"] == "node-1"
    assert payload["client_meta"]["node_offset"] == {"x": 0, "y": 0}


def test_build_figma_payload_custom_offset():
    payload = build_figma_payload(
        node_id="node-1",
        node_offset={"x": 10, "y": 20},
        mr_url=None,
        commit_sha=None,
    )
    assert payload["client_meta"]["node_offset"] == {"x": 10, "y": 20}


def test_build_miro_card_payload():
    payload = build_miro_card_payload(
        frame_meta={"x": 100.0, "y": 200.0, "width": 300.0},
        mr_url="https://gitlab.com/foo/bar/-/merge_requests/1",
        commit_sha="abc1234",
    )
    assert payload["data"]["title"] == "✅ Phase 8a 完了"
    assert "MR:" in payload["data"]["description"]
    assert "commit: abc1234" in payload["data"]["description"]
    # 主 frame の右側 50px、同じ y
    assert payload["position"]["x"] == 100.0 + 300.0 + 50.0
    assert payload["position"]["y"] == 200.0
    assert payload["style"]["fillColor"] == "#4FCC8B"


# ---------------------------------------------------------------------------
# FigmaClient.post_comment（_send / _request の挙動を mock で確認）
# ---------------------------------------------------------------------------


def test_post_comment_calls_correct_endpoint(monkeypatch):
    """POST /v1/files/{file_key}/comments が body 付きで呼ばれる"""
    client = FigmaClient(api_token="test-token")
    captured: dict[str, object] = {}

    def fake_send(method: str, url: str, *, body=None):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = body
        return {"id": "comment-xyz", "message": body["message"]}

    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(client, "_enforce_rate_limit", lambda: None)

    response = client.post_comment(
        "file-abc",
        message="hello",
        node_id="node-1",
        node_offset={"x": 0, "y": 0},
    )
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/files/file-abc/comments")
    assert captured["body"]["message"] == "hello"
    assert captured["body"]["client_meta"]["node_id"] == "node-1"
    assert response["id"] == "comment-xyz"


def test_post_comment_rejects_empty_inputs():
    client = FigmaClient(api_token="t")
    with pytest.raises(ValueError):
        client.post_comment("", message="m", node_id="n")
    with pytest.raises(ValueError):
        client.post_comment("f", message="", node_id="n")
    with pytest.raises(ValueError):
        client.post_comment("f", message="m", node_id="")


# ---------------------------------------------------------------------------
# FigmaWritebackDispatcher
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> OutboxStore:
    db = tmp_path / "wf.db"
    SQLiteStore(db)
    return OutboxStore(db, target=WritebackTarget.FIGMA)


@pytest.fixture
def mock_client() -> MagicMock:
    """FigmaClient のモック。post_comment のみ差し替え"""
    m = MagicMock(spec=FigmaClient)
    return m


def _args(**overrides) -> FigmaWritebackArgs:
    defaults = dict(
        workflow_id="wf-1",
        profile_name="company-a",
        event_type="phase8a_completed",
        revision="abc1234",
        file_key="file-abc",
        node_id="node-1",
        node_offset=None,
        mr_url="https://example.com/mr/1",
        commit_sha="abc1234",
    )
    defaults.update(overrides)
    return FigmaWritebackArgs(**defaults)


def test_dispatch_success_records_idempotency(store, mock_client):
    """投稿成功時に idempotency 記録 + outbox 残らない"""
    mock_client.post_comment.return_value = {"id": "comment-xyz", "message": "..."}
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    result = dispatcher.dispatch(_args())

    assert result["status"] == "delivered"
    assert result["response_id"] == "comment-xyz"
    assert result["error"] is None
    assert store.is_already_delivered(result["idempotency_key"]) is True
    assert len(store.list_outbox()) == 0
    mock_client.post_comment.assert_called_once_with(
        "file-abc",
        message=render_figma_message(
            mr_url="https://example.com/mr/1", commit_sha="abc1234",
        ),
        node_id="node-1",
        node_offset=None,
    )


def test_dispatch_failure_enqueues_outbox(store, mock_client):
    """API 失敗時に outbox に積む"""
    mock_client.post_comment.side_effect = FigmaAPIError(403, "Forbidden")
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    result = dispatcher.dispatch(_args())

    assert result["status"] == "enqueued"
    assert "403" in result["error"]
    entries = store.list_outbox()
    assert len(entries) == 1
    assert entries[0].profile_name == "company-a"
    assert entries[0].payload["file_key"] == "file-abc"
    assert entries[0].payload["node_id"] == "node-1"


def test_dispatch_rate_limit_enqueues_outbox(store, mock_client):
    """rate limit も outbox 行きとなる（自動 retry なし設計）"""
    mock_client.post_comment.side_effect = FigmaRateLimitError(60.0)
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    result = dispatcher.dispatch(_args())

    assert result["status"] == "enqueued"
    assert len(store.list_outbox()) == 1


def test_dispatch_skips_already_delivered(store, mock_client):
    """既に投稿済みのキーは skip して API を呼ばない"""
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    # 1 回目
    mock_client.post_comment.return_value = {"id": "first"}
    r1 = dispatcher.dispatch(_args())
    assert r1["status"] == "delivered"

    # 2 回目: 同じキー → skip
    r2 = dispatcher.dispatch(_args())
    assert r2["status"] == "skipped"
    assert r2["idempotency_key"] == r1["idempotency_key"]
    # API は 1 回だけ呼ばれた
    assert mock_client.post_comment.call_count == 1


def test_dispatch_skips_pending_outbox(store, mock_client):
    """outbox に pending がある状態で再 dispatch すると skip"""
    mock_client.post_comment.side_effect = FigmaAPIError(500, "Server Error")
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    r1 = dispatcher.dispatch(_args())
    assert r1["status"] == "enqueued"

    # 2 回目: outbox にあるので skip
    r2 = dispatcher.dispatch(_args())
    assert r2["status"] == "skipped"
    # API は 1 回だけ（最初の試行）
    assert mock_client.post_comment.call_count == 1


def test_dispatch_skips_errors_unless_force(store, mock_client):
    """errors にあれば既定で skip、force=True で再試行"""
    mock_client.post_comment.side_effect = FigmaAPIError(403, "Forbidden")
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    # 1 回失敗 → outbox 行きを errors に移動
    r1 = dispatcher.dispatch(_args())
    assert r1["status"] == "enqueued"
    outbox = store.list_outbox()[0]
    store.move_to_errors(outbox.id)

    # 2 回目: errors にあるので skip
    r2 = dispatcher.dispatch(_args())
    assert r2["status"] == "skipped"

    # force=True で API 呼び出しが走る（が、API が成功するモック）
    mock_client.post_comment.side_effect = None
    mock_client.post_comment.return_value = {"id": "comment-after-force"}
    r3 = dispatcher.dispatch(_args(), force=True)
    assert r3["status"] == "delivered"


def test_retry_increments_attempt_count(store, mock_client):
    """retry() が attempt_count を +1"""
    mock_client.post_comment.side_effect = FigmaAPIError(500, "ServerError")
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    dispatcher.dispatch(_args())
    outbox = store.list_outbox()[0]
    assert outbox.attempt_count == 0

    # 失敗のまま 2 回 retry → count = 2
    dispatcher.retry(outbox.id)
    dispatcher.retry(outbox.id)

    updated = store.get_outbox(outbox.id)
    assert updated is not None
    assert updated.attempt_count == 2


def test_retry_moves_to_errors_at_max_attempts(store, mock_client):
    """attempt_count = MAX_ATTEMPT_COUNT(5) で errors に移動"""
    from hokusai.integrations.design.writeback import MAX_ATTEMPT_COUNT
    mock_client.post_comment.side_effect = FigmaAPIError(500, "ServerError")
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    dispatcher.dispatch(_args())
    outbox = store.list_outbox()[0]

    # MAX_ATTEMPT_COUNT 回まで retry
    last_result = None
    for _ in range(MAX_ATTEMPT_COUNT):
        last_result = dispatcher.retry(outbox.id)

    # 最終 retry で errors 行きになる
    assert last_result is not None
    assert last_result["status"] == "moved_to_errors"
    assert len(store.list_outbox()) == 0
    assert len(store.list_errors()) == 1


def test_retry_not_found(store, mock_client):
    dispatcher = FigmaWritebackDispatcher(mock_client, store)
    result = dispatcher.retry(99999)
    assert result["status"] == "not_found"


def test_dispatcher_requires_figma_target(store, mock_client):
    """target=MIRO の store を渡すとエラー"""
    miro_store = OutboxStore(store.db_path, target=WritebackTarget.MIRO)
    with pytest.raises(ValueError):
        FigmaWritebackDispatcher(mock_client, miro_store)


def test_dispatcher_rejects_unknown_on_failure(store, mock_client):
    """on_failure に未知の値を渡すとエラー"""
    with pytest.raises(ValueError):
        FigmaWritebackDispatcher(mock_client, store, on_failure="unknown")


def test_on_failure_warn_enqueues_outbox(store, mock_client):
    """on_failure=warn: 失敗時 outbox に積み status=enqueued（既定動作）"""
    mock_client.post_comment.side_effect = FigmaAPIError(500, "err")
    dispatcher = FigmaWritebackDispatcher(mock_client, store, on_failure="warn")
    result = dispatcher.dispatch(_args())
    assert result["status"] == "enqueued"
    assert len(store.list_outbox()) == 1


def test_on_failure_block_returns_blocked_status(store, mock_client):
    """on_failure=block: 失敗時 outbox に積み status=blocked（呼び出し側が止める）"""
    mock_client.post_comment.side_effect = FigmaAPIError(500, "err")
    dispatcher = FigmaWritebackDispatcher(mock_client, store, on_failure="block")
    result = dispatcher.dispatch(_args())
    assert result["status"] == "blocked"
    assert result["on_failure"] == "block"
    # outbox にも積まれる（再送のため）
    assert len(store.list_outbox()) == 1


def test_on_failure_skip_no_enqueue(store, mock_client):
    """on_failure=skip: 失敗時 outbox にも積まない、status=skipped"""
    mock_client.post_comment.side_effect = FigmaAPIError(500, "err")
    dispatcher = FigmaWritebackDispatcher(mock_client, store, on_failure="skip")
    result = dispatcher.dispatch(_args())
    assert result["status"] == "skipped"
    assert result.get("on_failure") == "skip"
    # outbox に何も積まれない
    assert len(store.list_outbox()) == 0


def test_retry_executes_5th_attempt(store, mock_client):
    """retry の閾値到達回（5 回目）も実際に dispatch が走り、その結果として errors 移動"""
    from hokusai.integrations.design.writeback import MAX_ATTEMPT_COUNT
    mock_client.post_comment.side_effect = FigmaAPIError(500, "ServerError")
    dispatcher = FigmaWritebackDispatcher(mock_client, store)

    # 1 回目 dispatch（attempt_count=0 で enqueue）
    dispatcher.dispatch(_args())
    outbox = store.list_outbox()[0]
    initial_call_count = mock_client.post_comment.call_count

    # MAX_ATTEMPT_COUNT 回 retry → 各回で API 呼び出しが行われる
    for _ in range(MAX_ATTEMPT_COUNT):
        dispatcher.retry(outbox.id)

    # 初回 dispatch + retry × MAX_ATTEMPT_COUNT 回 = MAX_ATTEMPT_COUNT+1 回の API call
    assert mock_client.post_comment.call_count == initial_call_count + MAX_ATTEMPT_COUNT
    # 最終的に errors 移動
    assert len(store.list_outbox()) == 0
    assert len(store.list_errors()) == 1
