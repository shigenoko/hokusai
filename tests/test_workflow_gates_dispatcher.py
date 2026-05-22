"""Workflow Gates dispatcher 統合テスト（Issue #44 / Workgraph Phase 4）

dispatcher.NotionSyncDispatcher の workflow_gate_upsert /
workflow_gate_status_change ハンドラを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.config.models import (
    NotionDashboardConfig,
    NotionSyncOutboxConfig,
    NotionSyncRateLimitConfig,
    NotionSyncRetryConfig,
)
from hokusai.integrations.notion_dashboard.dispatcher import (
    EVENT_GATE_STATUS_CHANGE,
    EVENT_GATE_UPSERT,
    NotionSyncDispatcher,
)
from hokusai.persistence.sqlite_store import SQLiteStore
from tests._notion_test_helpers import NotionRecordingAPI as _RecordingAPI


def _make_config(enabled: bool = True) -> NotionDashboardConfig:
    return NotionDashboardConfig(
        enabled=enabled,
        api_token_env="TEST_TOKEN",
        workflows_db_id_env="TEST_DB",
        sync_outbox=NotionSyncOutboxConfig(enabled=True, max_retry_attempts=3),
        retry=NotionSyncRetryConfig(max_attempts=2, backoff_seconds=0.5),
        rate_limit=NotionSyncRateLimitConfig(requests_per_second=100, debounce_ms=0),
    )


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "test.db")


def _build_dispatcher_with_gates_db(
    store: SQLiteStore, monkeypatch, *, query_result=None
) -> tuple[NotionSyncDispatcher, _RecordingAPI]:
    """Gate event テスト共通 setup"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_GATES_DB", "wg-db")

    cfg = _make_config()
    cfg.workflow_gates_db_id_env = "TEST_GATES_DB"
    api = _RecordingAPI(query_result=query_result or [])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    return _Disp(store=store, config=cfg), api


# ---------------------------------------------------------------------------
# workflow_gate_upsert
# ---------------------------------------------------------------------------


def test_dispatcher_gate_upsert_skips_when_db_id_unset(
    store: SQLiteStore, monkeypatch
):
    """Workflow Gates DB ID 未設定なら no-op（後方互換）"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.delenv("TEST_GATES_DB", raising=False)

    cfg = _make_config()
    cfg.workflow_gates_db_id_env = "TEST_GATES_DB"
    api = _RecordingAPI()

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_GATE_UPSERT, {
        "name": "Phase 5 approval",
        "gate_type": "human_approval",
        "workflow_id": "wf-1",
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_gate_upsert_creates_record(
    store: SQLiteStore, monkeypatch
):
    """設定済みなら Workflow Gates DB に create_page される"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_UPSERT, {
        "name": "Phase 5 approval",
        "gate_type": "human_approval",
        "workflow_id": "wf-1",
        "required_by_phase": 5,
    })
    assert result is True
    creates = [c for c in api.calls if c[0] == "create"]
    assert len(creates) == 1
    props = creates[0][1]["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "Phase 5 approval"
    assert props["Gate Type"]["select"]["name"] == "human_approval"
    assert props["Required By Phase"]["number"] == 5
    assert props["Status"]["select"]["name"] == "pending"  # default
    assert "Dedupe Key" in props


def test_dispatcher_gate_upsert_requires_name_and_gate_type(
    store: SQLiteStore, monkeypatch
):
    """name / gate_type 欠落で no-op"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    # gate_type 欠落
    result = disp.dispatch(EVENT_GATE_UPSERT, {
        "name": "X",
        "workflow_id": "wf-1",
    })
    assert result is True
    assert api.calls == []
    # name 欠落
    disp2, api2 = _build_dispatcher_with_gates_db(store, monkeypatch)
    result2 = disp2.dispatch(EVENT_GATE_UPSERT, {
        "gate_type": "human_approval",
        "workflow_id": "wf-1",
    })
    assert result2 is True
    assert api2.calls == []


def test_dispatcher_gate_upsert_skips_invalid_gate_type(
    store: SQLiteStore, monkeypatch
):
    """enum 外の gate_type は warning + skip（poison message 防止）"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_UPSERT, {
        "name": "X",
        "gate_type": "not_a_real_type",
        "workflow_id": "wf-1",
    })
    assert result is True
    creates = [c for c in api.calls if c[0] == "create"]
    assert creates == []


def test_dispatcher_gate_upsert_skips_invalid_status(
    store: SQLiteStore, monkeypatch
):
    """enum 外の status は warning + skip"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_UPSERT, {
        "name": "X",
        "gate_type": "human_approval",
        "status": "approved",  # 不正値（open が正）
        "workflow_id": "wf-1",
    })
    assert result is True
    creates = [c for c in api.calls if c[0] == "create"]
    assert creates == []


def test_dispatcher_gate_upsert_skips_invalid_dedupe_key_type(
    store: SQLiteStore, monkeypatch
):
    """dedupe_key が str 以外なら skip"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_UPSERT, {
        "name": "X",
        "gate_type": "human_approval",
        "workflow_id": "wf-1",
        "dedupe_key": ["bad", "type"],
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_gate_upsert_skips_invalid_required_by_phase(
    store: SQLiteStore, monkeypatch
):
    """required_by_phase が int 変換不能なら skip"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_UPSERT, {
        "name": "X",
        "gate_type": "human_approval",
        "workflow_id": "wf-1",
        "required_by_phase": {"bad": "type"},
    })
    assert result is True
    assert api.calls == []


# ---------------------------------------------------------------------------
# workflow_gate_status_change
# ---------------------------------------------------------------------------


def test_dispatcher_gate_status_change_updates_existing(
    store: SQLiteStore, monkeypatch
):
    """既存 gate を見つけて update_status を呼ぶ"""
    disp, api = _build_dispatcher_with_gates_db(
        store, monkeypatch, query_result=[{"id": "gate-existing"}]
    )
    result = disp.dispatch(EVENT_GATE_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "gate_type": "human_approval",
        "required_by_phase": 5,
        "status": "open",
        "approver": "alice@example.com",
        "decision_reason": "approved 2026-05-22",
    })
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert len(updates) == 1
    page_id = updates[0][1]["page_id"]
    assert page_id == "gate-existing"
    props = updates[0][1]["properties"]
    assert props["Status"]["select"]["name"] == "open"
    assert (
        props["Approver"]["rich_text"][0]["text"]["content"]
        == "alice@example.com"
    )


def test_dispatcher_gate_status_change_warns_when_page_not_found(
    store: SQLiteStore, monkeypatch
):
    """対象 gate が見つからない場合は warning + skip"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "gate_type": "human_approval",
        "status": "open",
    })
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert updates == []


def test_dispatcher_gate_status_change_requires_status(
    store: SQLiteStore, monkeypatch
):
    """status 欠落で no-op"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "gate_type": "human_approval",
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_gate_status_change_skips_invalid_status(
    store: SQLiteStore, monkeypatch
):
    """enum 外の status は skip"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "gate_type": "human_approval",
        "status": "approved",  # 不正値
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_gate_status_change_needs_gate_type_when_no_dedupe_key(
    store: SQLiteStore, monkeypatch
):
    """dedupe_key も gate_type も無ければ同定不能で skip"""
    disp, api = _build_dispatcher_with_gates_db(store, monkeypatch)
    result = disp.dispatch(EVENT_GATE_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "status": "open",
    })
    assert result is True
    assert api.calls == []


# ---------------------------------------------------------------------------
# _count_pending_workflow_page_events_for excludes gate events
# ---------------------------------------------------------------------------


def test_dispatcher_excludes_gate_events_from_pending_count(
    store: SQLiteStore, monkeypatch
):
    """workflow_gate_upsert / workflow_gate_status_change は workflow page と
    独立した同期なので pending 集計から除外され self-deferral ループに陥らない"""
    cfg = _make_config()
    disp = NotionSyncDispatcher(store=store, config=cfg)

    store.enqueue_notion_sync(
        idempotency_key="wf-1:workflow_gate_upsert:abc",
        workflow_id="wf-1",
        event_type="workflow_gate_upsert",
        payload={"workflow_id": "wf-1", "name": "X", "gate_type": "human_approval"},
    )
    store.enqueue_notion_sync(
        idempotency_key="wf-1:workflow_gate_status_change:def",
        workflow_id="wf-1",
        event_type="workflow_gate_status_change",
        payload={"workflow_id": "wf-1", "status": "open"},
    )
    # gate イベント 2 件しか無いので workflow page sync の pending は 0
    assert disp._count_pending_workflow_page_events_for("wf-1") == 0


def test_dispatcher_gate_status_change_skips_invalid_gate_type(
    store: SQLiteStore, monkeypatch
):
    """status_change で dedupe_key 未指定 + gate_type が enum 外なら skip
    （PR #45 Copilot 2 回目指摘の poison message 防止）"""
    disp, api = _build_dispatcher_with_gates_db(
        store, monkeypatch, query_result=[{"id": "gate-existing"}]
    )
    result = disp.dispatch(EVENT_GATE_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "gate_type": "not_a_real_type",  # enum 外
        "required_by_phase": 5,
        "status": "open",
    })
    assert result is True
    # find_by_dedupe_key も update_status も呼ばれない
    queries = [c for c in api.calls if c[0] == "query"]
    updates = [c for c in api.calls if c[0] == "update"]
    assert queries == []
    assert updates == []
