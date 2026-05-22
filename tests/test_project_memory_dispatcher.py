"""Project Memory dispatcher 統合テスト（Issue #46 / Workgraph Phase 5）"""

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
    EVENT_PROJECT_MEMORY_STATUS_CHANGE,
    EVENT_PROJECT_MEMORY_UPSERT,
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


def _build_dispatcher_with_memory_db(
    store: SQLiteStore, monkeypatch, *, query_result=None
) -> tuple[NotionSyncDispatcher, _RecordingAPI]:
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_MEMORY_DB", "pm-db")

    cfg = _make_config()
    cfg.project_memory_db_id_env = "TEST_MEMORY_DB"
    api = _RecordingAPI(query_result=query_result or [])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    return _Disp(store=store, config=cfg), api


# ---------------------------------------------------------------------------
# project_memory_upsert
# ---------------------------------------------------------------------------


def test_dispatcher_memory_upsert_skips_when_db_id_unset(
    store: SQLiteStore, monkeypatch
):
    """Project Memory DB ID 未設定なら no-op（後方互換）"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.delenv("TEST_MEMORY_DB", raising=False)
    cfg = _make_config()
    cfg.project_memory_db_id_env = "TEST_MEMORY_DB"
    api = _RecordingAPI()

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_UPSERT, {
        "name": "Project rule",
        "memory_type": "project_rule",
        "content": "API tokens must not be stored in DB",
        "workflow_id": "wf-1",
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_memory_upsert_creates_record(
    store: SQLiteStore, monkeypatch
):
    """設定済みなら Project Memory DB に create_page される"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_UPSERT, {
        "name": "Project rule",
        "memory_type": "project_rule",
        "content": "API tokens must not be stored in DB",
        "workflow_id": "wf-1",
    })
    assert result is True
    creates = [c for c in api.calls if c[0] == "create"]
    assert len(creates) == 1
    props = creates[0][1]["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "Project rule"
    assert props["Type"]["select"]["name"] == "project_rule"
    assert props["Status"]["select"]["name"] == "draft"  # default
    assert "Content" in props
    assert "Dedupe Key" in props


def test_dispatcher_memory_upsert_requires_name_type_content(
    store: SQLiteStore, monkeypatch
):
    """name / memory_type / content のいずれか欠落で no-op"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    # memory_type 欠落
    result = disp.dispatch(EVENT_PROJECT_MEMORY_UPSERT, {
        "name": "X", "content": "c", "workflow_id": "wf-1",
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_memory_upsert_skips_invalid_memory_type(
    store: SQLiteStore, monkeypatch
):
    """enum 外の memory_type は skip（poison message 防止）"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_UPSERT, {
        "name": "X",
        "memory_type": "not_a_real_type",
        "content": "c",
        "workflow_id": "wf-1",
    })
    assert result is True
    assert [c for c in api.calls if c[0] == "create"] == []


def test_dispatcher_memory_upsert_skips_invalid_status(
    store: SQLiteStore, monkeypatch
):
    """enum 外の status は skip"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_UPSERT, {
        "name": "X",
        "memory_type": "project_rule",
        "content": "c",
        "status": "approved",  # 不正
        "workflow_id": "wf-1",
    })
    assert result is True
    assert [c for c in api.calls if c[0] == "create"] == []


def test_dispatcher_memory_upsert_skips_invalid_dedupe_key_type(
    store: SQLiteStore, monkeypatch
):
    """dedupe_key が str 以外なら skip"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_UPSERT, {
        "name": "X",
        "memory_type": "project_rule",
        "content": "c",
        "workflow_id": "wf-1",
        "dedupe_key": ["bad"],
    })
    assert result is True
    assert api.calls == []


# ---------------------------------------------------------------------------
# project_memory_status_change
# ---------------------------------------------------------------------------


def test_dispatcher_memory_status_change_updates_existing(
    store: SQLiteStore, monkeypatch
):
    """既存 memory を見つけて update_status を呼ぶ"""
    disp, api = _build_dispatcher_with_memory_db(
        store, monkeypatch, query_result=[{"id": "memory-existing"}]
    )
    result = disp.dispatch(EVENT_PROJECT_MEMORY_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "memory_type": "project_rule",
        "name": "X",
        "status": "active",
        "approved_by": "alice@example.com",
        "approved_at": "2026-05-22T10:00:00",
    })
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert len(updates) == 1
    page_id = updates[0][1]["page_id"]
    assert page_id == "memory-existing"
    props = updates[0][1]["properties"]
    assert props["Status"]["select"]["name"] == "active"
    assert (
        props["Approved By"]["rich_text"][0]["text"]["content"]
        == "alice@example.com"
    )


def test_dispatcher_memory_status_change_warns_when_page_not_found(
    store: SQLiteStore, monkeypatch
):
    """対象 memory が見つからない場合は warning + skip"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "memory_type": "project_rule",
        "name": "X",
        "status": "active",
    })
    assert result is True
    assert [c for c in api.calls if c[0] == "update"] == []


def test_dispatcher_memory_status_change_requires_status(
    store: SQLiteStore, monkeypatch
):
    """status 欠落で no-op"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "memory_type": "project_rule",
        "name": "X",
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_memory_status_change_skips_invalid_status(
    store: SQLiteStore, monkeypatch
):
    """enum 外の status は skip"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "memory_type": "project_rule",
        "name": "X",
        "status": "approved",  # 不正
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_memory_status_change_needs_type_and_name_when_no_dedupe_key(
    store: SQLiteStore, monkeypatch
):
    """dedupe_key も memory_type/name も無ければ同定不能で skip"""
    disp, api = _build_dispatcher_with_memory_db(store, monkeypatch)
    result = disp.dispatch(EVENT_PROJECT_MEMORY_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "status": "active",
    })
    assert result is True
    assert api.calls == []


# ---------------------------------------------------------------------------
# _count_pending_workflow_page_events_for excludes memory events
# ---------------------------------------------------------------------------


def test_dispatcher_excludes_memory_events_from_pending_count(
    store: SQLiteStore, monkeypatch
):
    """project_memory_* は workflow page と独立した同期なので pending 集計から除外"""
    cfg = _make_config()
    disp = NotionSyncDispatcher(store=store, config=cfg)

    store.enqueue_notion_sync(
        idempotency_key="wf-1:project_memory_upsert:abc",
        workflow_id="wf-1",
        event_type="project_memory_upsert",
        payload={"workflow_id": "wf-1", "name": "X"},
    )
    store.enqueue_notion_sync(
        idempotency_key="wf-1:project_memory_status_change:def",
        workflow_id="wf-1",
        event_type="project_memory_status_change",
        payload={"workflow_id": "wf-1", "status": "active"},
    )
    # memory イベント 2 件のみで workflow page sync の pending は 0
    assert disp._count_pending_workflow_page_events_for("wf-1") == 0
