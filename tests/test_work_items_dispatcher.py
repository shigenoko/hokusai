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
from tests._notion_test_helpers import NotionRecordingAPI as _RecordingAPI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def test_dispatcher_work_item_status_change_skips_when_dedupe_key_and_title_both_missing(
    store: SQLiteStore, monkeypatch
):
    """dedupe_key が無く title も無い場合は同定不能なので skip（誤更新防止）
    （PR #41 Copilot 4 回目指摘: 空 title で build_dedupe_key を走らせると
    同一 workflow_id/phase 配下の全 Work Item が同一 dedupe_key に潰れて
    別 Work Item を誤って更新するリスクがあるため、title 必須に）。"""
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
        "phase": 4,
        "status": "done",
        # title も dedupe_key も省略
    })
    assert result is True
    # find_by_dedupe_key も呼ばれないことを確認
    queries = [c for c in api.calls if c[0] == "query"]
    assert queries == []


def _build_status_change_dispatcher_with_missing_page(
    store: SQLiteStore, monkeypatch
) -> tuple[NotionSyncDispatcher, _RecordingAPI]:
    """status_change テスト用の共通 setup（page 未存在シナリオ）。

    PR #41 SonarCloud 6 回目対応で 2 テストの boilerplate 重複を集約。
    """
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"

    api = _RecordingAPI(query_result=[])  # find_by_dedupe_key → 空

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    return _Disp(store=store, config=cfg), api


def _dispatch_status_change_for_test(disp: NotionSyncDispatcher) -> bool:
    """共通の status_change dispatch ペイロードで dispatch を呼ぶ。"""
    return disp.dispatch(EVENT_WORK_ITEM_STATUS_CHANGE, {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
        "status": "done",
    })


def test_dispatcher_work_item_status_change_defers_when_upsert_pending(
    store: SQLiteStore, monkeypatch
):
    """status_change で page が見つからず、**同じ dedupe_key の** work_item_upsert
    が outbox に pending なら NotionAPIError(503) で defer する
    （PR #41 Copilot 7/8 回目指摘: silent drop 防止 + dedupe_key 単位での絞り込み）。
    """
    from hokusai.integrations.notion_dashboard.work_items_db import build_dedupe_key

    disp, api = _build_status_change_dispatcher_with_missing_page(store, monkeypatch)
    # dispatcher と同じ dedupe_key を outbox に積む（exact match で defer 発火）
    dkey = build_dedupe_key(workflow_id="wf-1", phase=4, title="X")
    store.enqueue_notion_sync(
        idempotency_key=f"wf-1:work_item_upsert:{dkey}",
        workflow_id="wf-1",
        event_type="work_item_upsert",
        payload={"workflow_id": "wf-1", "title": "X", "phase": 4},
    )
    result = _dispatch_status_change_for_test(disp)
    # dispatch は False（outbox にキューイング）。update_status は呼ばれない。
    assert result is False
    updates = [c for c in api.calls if c[0] == "update"]
    assert updates == []


def test_dispatcher_work_item_status_change_genuine_miss_skips_with_warning(
    store: SQLiteStore, monkeypatch, caplog
):
    """status_change で page が見つからず、pending upsert も無いケースは
    genuine miss として warning + skip（後続には影響しない）。"""
    disp, api = _build_status_change_dispatcher_with_missing_page(store, monkeypatch)
    result = _dispatch_status_change_for_test(disp)
    # pending upsert が無いので defer はせず、warning + skip で成功扱い
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert updates == []


def test_dispatcher_work_item_status_change_does_not_defer_on_unrelated_pending_upsert(
    store: SQLiteStore, monkeypatch
):
    """別の Work Item の upsert が pending でも、対象の dedupe_key と一致
    しない限り status_change は genuine miss として skip する
    （PR #41 Copilot 8 回目指摘: workflow_id 単位の broad defer を避ける）。
    """
    disp, _api = _build_status_change_dispatcher_with_missing_page(store, monkeypatch)
    # 全く別の dedupe_key を持つ upsert を outbox に積む
    store.enqueue_notion_sync(
        idempotency_key="wf-1:work_item_upsert:unrelated_dedupe_key",
        workflow_id="wf-1",
        event_type="work_item_upsert",
        payload={"workflow_id": "wf-1", "title": "OTHER", "phase": 4},
    )
    result = _dispatch_status_change_for_test(disp)
    # 対象 dedupe_key と一致しないので defer は発生しない（success skip）
    assert result is True


# ---------------------------------------------------------------------------
# work_item_claim / work_item_lease_release イベント（Workgraph Phase 3 / #42）
# ---------------------------------------------------------------------------


def _build_lease_dispatcher_with_existing_page(
    store: SQLiteStore, monkeypatch
) -> tuple[NotionSyncDispatcher, _RecordingAPI]:
    """claim / lease_release テスト共通 setup（page 存在シナリオ）"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"
    # find_by_dedupe_key → 既存ページが見つかる
    api = _RecordingAPI(query_result=[{"id": "existing-page"}])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    return _Disp(store=store, config=cfg), api


def test_dispatcher_work_item_claim_writes_active_lease(
    store: SQLiteStore, monkeypatch
):
    """claim イベントは Lease Status=active 等を Notion に書き込む"""
    disp, api = _build_lease_dispatcher_with_existing_page(store, monkeypatch)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": "wf-1",
        "title": "implement login",
        "phase": 4,
        "claimed_by": "claude_code",
        "lease_duration_seconds": 600,
    })
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert len(updates) == 1
    props = updates[0][1]["properties"]
    assert props["Lease Status"]["select"]["name"] == "active"
    assert props["Claimed By"]["rich_text"][0]["text"]["content"] == "claude_code"
    assert props["Claim Type"]["select"]["name"] == "agent"
    assert "Lease Token" in props


def test_dispatcher_work_item_claim_requires_claimed_by(
    store: SQLiteStore, monkeypatch
):
    """claimed_by 欠落時は API を呼ばずに skip"""
    disp, api = _build_lease_dispatcher_with_existing_page(store, monkeypatch)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
        # claimed_by 省略
    })
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert updates == []


def test_dispatcher_work_item_lease_release_writes_released(
    store: SQLiteStore, monkeypatch
):
    """lease_release イベントは Lease Status=released を書き込む"""
    disp, api = _build_lease_dispatcher_with_existing_page(store, monkeypatch)
    result = disp.dispatch("work_item_lease_release", {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
    })
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert len(updates) == 1
    props = updates[0][1]["properties"]
    assert props["Lease Status"]["select"]["name"] == "released"
    # Claimed By / Lease Token は監査用に温存（payload に含まれない）
    assert "Claimed By" not in props
    assert "Lease Token" not in props


def test_dispatcher_work_item_claim_skips_when_db_id_unset(
    store: SQLiteStore, monkeypatch
):
    """Work Items DB ID 未設定なら no-op で成功扱い"""
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
    result = disp.dispatch("work_item_claim", {
        "workflow_id": "wf-1", "title": "X", "claimed_by": "x", "phase": 4,
    })
    assert result is True
    assert api.calls == []


def test_dispatcher_work_item_claim_defers_when_upsert_pending(
    store: SQLiteStore, monkeypatch
):
    """page 未存在 + 同じ dedupe_key の upsert pending → 503 で defer"""
    from hokusai.integrations.notion_dashboard.work_items_db import build_dedupe_key

    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"
    api = _RecordingAPI(query_result=[])  # 未存在
    dkey = build_dedupe_key(workflow_id="wf-1", phase=4, title="X")
    store.enqueue_notion_sync(
        idempotency_key=f"wf-1:work_item_upsert:{dkey}",
        workflow_id="wf-1",
        event_type="work_item_upsert",
        payload={"workflow_id": "wf-1", "title": "X", "phase": 4},
    )

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
        "claimed_by": "claude_code",
    })
    # outbox にキューイングで False
    assert result is False


def test_dispatcher_work_item_claim_skips_when_lease_duration_non_numeric(
    store: SQLiteStore, monkeypatch
):
    """lease_duration_seconds が非数値 (str / list 等) なら warning + skip
    （poison message 化防止、PR #43 Copilot 2 回目指摘）"""
    disp, api = _build_lease_dispatcher_with_existing_page(store, monkeypatch)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
        "claimed_by": "claude_code",
        "lease_duration_seconds": "abc",  # 非数値
    })
    assert result is True
    # claim_work_item は呼ばれない
    updates = [c for c in api.calls if c[0] == "update"]
    assert updates == []


def test_dispatcher_work_item_claim_skips_when_lease_duration_zero_or_negative(
    store: SQLiteStore, monkeypatch
):
    """lease_duration_seconds が 0 以下なら warning + skip"""
    disp, api = _build_lease_dispatcher_with_existing_page(store, monkeypatch)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
        "claimed_by": "claude_code",
        "lease_duration_seconds": 0,
    })
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert updates == []

    # 負数も同じ
    disp2, api2 = _build_lease_dispatcher_with_existing_page(store, monkeypatch)
    result2 = disp2.dispatch("work_item_claim", {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
        "claimed_by": "claude_code",
        "lease_duration_seconds": -100,
    })
    assert result2 is True
    assert [c for c in api2.calls if c[0] == "update"] == []


def test_dispatcher_work_item_claim_skips_when_claim_type_invalid(
    store: SQLiteStore, monkeypatch
):
    """claim_type が agent/human 以外なら warning + skip"""
    disp, api = _build_lease_dispatcher_with_existing_page(store, monkeypatch)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": 4,
        "claimed_by": "claude_code",
        "claim_type": "bot",  # 不正
    })
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert updates == []


def test_dispatcher_work_item_claim_skips_when_workflow_id_invalid_type(
    store: SQLiteStore, monkeypatch
):
    """workflow_id が str/int 以外（list 等）なら warning + skip
    （PR #43 Copilot 3 回目指摘の poison message 防止）"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"
    api = _RecordingAPI(query_result=[])  # 未存在

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": ["bad", "type"],  # list は str/int でないので skip
        "title": "X",
        "phase": 4,
        "claimed_by": "claude_code",
    })
    # poison message にならず success（no-op）
    assert result is True


def test_dispatcher_work_item_claim_accepts_int_workflow_id(
    store: SQLiteStore, monkeypatch
):
    """workflow_id が int でも str に正規化されて build_dedupe_key 経由で動く"""
    disp, api = _build_lease_dispatcher_with_existing_page(store, monkeypatch)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": 12345,  # numeric ID
        "title": "X",
        "phase": 4,
        "claimed_by": "claude_code",
    })
    # int は str() で正規化されて build_dedupe_key を通る
    assert result is True
    updates = [c for c in api.calls if c[0] == "update"]
    assert len(updates) == 1


def test_dispatcher_work_item_claim_skips_when_phase_invalid_type(
    store: SQLiteStore, monkeypatch
):
    """phase が int に変換できない（非数値 str / dict 等）なら warning + skip"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")

    cfg = _make_config()
    cfg.work_items_db_id_env = "TEST_WORK_ITEMS_DB"
    api = _RecordingAPI(query_result=[])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch("work_item_claim", {
        "workflow_id": "wf-1",
        "title": "X",
        "phase": {"bad": "type"},  # int 変換不能
        "claimed_by": "claude_code",
    })
    assert result is True
