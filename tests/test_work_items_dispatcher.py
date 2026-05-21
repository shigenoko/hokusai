"""Work Items DB dispatcher 統合テスト（Issue #38 / Workgraph Phase 2）

dispatcher.NotionSyncDispatcher の work_item_upsert / work_item_status_change
ハンドラを検証する。`WorkflowRunner._drain_pending_work_items` の drain 経路
テストは tests/test_notion_dashboard.py 側に置く（Review Issues drain と同じ
fixture を共用するため）。
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
    EVENT_WORK_ITEM_STATUS_CHANGE,
    EVENT_WORK_ITEM_UPSERT,
    NotionSyncDispatcher,
)
from hokusai.persistence.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingAPI:
    """API クライアントの動作を記録するスタブ"""

    def __init__(self, *, query_result: list | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._query_result = query_result or []

    def query_database(self, database_id: str, *, filter_: dict | None = None) -> dict:
        self.calls.append(("query", {"database_id": database_id, "filter": filter_}))
        return {"results": self._query_result}

    def create_page(self, payload: dict) -> dict:
        self.calls.append(("create", payload))
        return {"id": "page-new"}

    def update_page(self, page_id: str, payload: dict) -> dict:
        self.calls.append(("update", {"page_id": page_id, **payload}))
        return {"id": page_id}


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


# ---------------------------------------------------------------------------
# dispatcher: work_item_upsert
# ---------------------------------------------------------------------------


def test_dispatcher_work_item_upsert_skips_when_db_id_unset(
    store: SQLiteStore, monkeypatch
):
    """Work Items DB ID が未設定なら no-op で成功扱い（後方互換）"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.delenv("TEST_WORK_ITEMS_DB", raising=False)

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"

    api = _RecordingAPI()

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_WORK_ITEM_UPSERT, {
        "workflow_id": "wf-1",
        "title": "implement login",
        "phase": 5,
    })
    assert result is True
    # API は呼ばれない
    assert api.calls == []


def test_dispatcher_work_item_upsert_creates_record(
    store: SQLiteStore, monkeypatch
):
    """設定済みなら Work Items DB に create_page される"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"

    # workflow_page lookup の find_by_dedupe_key 用 query で「未存在」を返し
    # (find_by_dedupe_key → []), create パスへ。Workflow relation は無い。
    api = _RecordingAPI(query_result=[])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_WORK_ITEM_UPSERT, {
        "workflow_id": "wf-1",
        "title": "implement login",
        "phase": 5,
        "status": "pending",
        "description": "Login form validation",
    })
    assert result is True

    creates = [c for c in api.calls if c[0] == "create"]
    assert len(creates) == 1
    props = creates[0][1]["properties"]
    assert props["Title"]["title"][0]["text"]["content"] == "implement login"
    assert props["Phase"]["number"] == 5
    assert props["Status"]["select"]["name"] == "pending"
    assert (
        props["Description"]["rich_text"][0]["text"]["content"]
        == "Login form validation"
    )


def test_dispatcher_work_item_upsert_requires_title(
    store: SQLiteStore, monkeypatch
):
    """title 欠落時は API を呼ばずに no-op"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"

    api = _RecordingAPI()

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_WORK_ITEM_UPSERT, {
        "workflow_id": "wf-1",
        "phase": 5,
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_work_item_upsert_defers_when_workflow_sync_pending(
    store: SQLiteStore, monkeypatch
):
    """workflow page が未同期かつ outbox に workflow_started が pending なら
    NotionAPIError(503) で deferして outbox に積み直す（race condition 対応）"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"

    # workflow page lookup → 未存在
    api = _RecordingAPI(query_result=[])
    # 同じ workflow_id の workflow_started が outbox に pending
    store.enqueue_notion_sync(
        idempotency_key="wf-1:workflow_started:1:0",
        workflow_id="wf-1",
        event_type="workflow_started",
        payload={"workflow_id": "wf-1", "task_title": "Test"},
    )

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_WORK_ITEM_UPSERT, {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 5,
    })
    # dispatch は False（outbox にキューイング）。create_page は呼ばれない。
    assert result is False
    creates = [c for c in api.calls if c[0] == "create"]
    assert creates == []


# ---------------------------------------------------------------------------
# _count_pending_workflow_page_events_for: work_item_upsert も除外される
# ---------------------------------------------------------------------------


def test_dispatcher_excludes_work_item_upsert_from_pending_count(
    store: SQLiteStore, monkeypatch
):
    """work_item_upsert / review_issue_raised は workflow page と独立した同期なので、
    pending 集計から除外され self-deferral ループに陥らない"""
    cfg = _make_config()
    disp = NotionSyncDispatcher(store=store, config=cfg)

    store.enqueue_notion_sync(
        idempotency_key="wf-1:work_item_upsert:abc",
        workflow_id="wf-1",
        event_type="work_item_upsert",
        payload={"workflow_id": "wf-1", "title": "X"},
    )
    store.enqueue_notion_sync(
        idempotency_key="wf-1:review_issue_raised:def",
        workflow_id="wf-1",
        event_type="review_issue_raised",
        payload={"workflow_id": "wf-1", "source": "x", "message": "y"},
    )
    # work_item_upsert + review_issue_raised の 2 件しか outbox に無い場合、
    # workflow page sync の pending は 0
    assert disp._count_pending_workflow_page_events_for("wf-1") == 0


# ---------------------------------------------------------------------------
# work_item_status_change: 状態遷移専用イベント
# ---------------------------------------------------------------------------


def test_dispatcher_work_item_status_change_calls_update_status(
    store: SQLiteStore, monkeypatch
):
    """work_item_status_change は find_by_dedupe_key で page を見つけて
    WorkItemsDBClient.update_status を呼ぶ"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"

    # find_by_dedupe_key → ["existing"] を返す
    api = _RecordingAPI(query_result=[{"id": "existing-page"}])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_WORK_ITEM_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "title": "implement login",
        "phase": 4,
        "status": "done",
    })
    assert result is True
    # update_page (status only) が呼ばれる
    updates = [c for c in api.calls if c[0] == "update"]
    assert len(updates) == 1
    page_id = updates[0][1]["page_id"]
    assert page_id == "existing-page"
    props = updates[0][1]["properties"]
    assert props["Status"]["select"]["name"] == "done"


def test_dispatcher_work_item_status_change_warns_when_page_not_found(
    store: SQLiteStore, monkeypatch, caplog
):
    """対応 Work Item が見つからない場合は warning で skip（API は呼ばない）"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"

    api = _RecordingAPI(query_result=[])  # find_by_dedupe_key → 空

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_WORK_ITEM_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
        "status": "done",
    })
    assert result is True
    # query は呼ばれるが update は呼ばれない
    updates = [c for c in api.calls if c[0] == "update"]
    assert updates == []


def test_dispatcher_work_item_status_change_requires_status(
    store: SQLiteStore, monkeypatch
):
    """status 欠落時は API を呼ばずに no-op"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"

    api = _RecordingAPI()

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(EVENT_WORK_ITEM_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
    })
    assert result is True
    assert api.calls == []
