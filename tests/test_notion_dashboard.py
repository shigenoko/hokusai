"""Notion メインダッシュボード同期のテスト

対象:
- 設定パース（_parse_notion_dashboard_config）
- SQLite outbox / errors の操作 API
- NotionAPIClient のレートリミット・リトライ・エラーハンドリング
- WorkflowsDBClient のプロパティマッピング
- NotionSyncDispatcher の通常パス・失敗パス・再送パス
"""

from __future__ import annotations

import json
import tempfile
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from hokusai.config.loaders import _parse_notion_dashboard_config
from hokusai.config.models import (
    NotionDashboardConfig,
    NotionSyncOutboxConfig,
    NotionSyncRateLimitConfig,
    NotionSyncRetryConfig,
)
from hokusai.integrations.notion_dashboard import client as client_module
from hokusai.integrations.notion_dashboard.client import (
    NotionAPIClient,
    NotionAPIError,
    NotionRateLimitError,
)
from hokusai.integrations.notion_dashboard.dispatcher import NotionSyncDispatcher
from hokusai.integrations.notion_dashboard.workflows_db import WorkflowsDBClient
from hokusai.persistence.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# 設定パース
# ---------------------------------------------------------------------------


def test_parse_notion_dashboard_returns_default_when_missing():
    cfg = _parse_notion_dashboard_config({})
    assert isinstance(cfg, NotionDashboardConfig)
    assert cfg.enabled is False
    assert cfg.api_token_env == "HOKUSAI_NOTION_API_TOKEN"


def test_parse_notion_dashboard_full_config():
    cfg = _parse_notion_dashboard_config({
        "notion_dashboard": {
            "enabled": True,
            "api_token_env": "MY_TOKEN",
            "workflows_db_id_env": "MY_DB",
            "sync_outbox": {"enabled": True, "max_retry_attempts": 5},
            "retry": {"max_attempts": 5, "backoff_seconds": 10},
            "rate_limit": {"requests_per_second": 3, "debounce_ms": 2000},
        }
    })
    assert cfg.enabled is True
    assert cfg.api_token_env == "MY_TOKEN"
    assert cfg.workflows_db_id_env == "MY_DB"
    assert cfg.sync_outbox.max_retry_attempts == 5
    assert cfg.retry.max_attempts == 5
    assert cfg.retry.backoff_seconds == 10.0
    assert cfg.rate_limit.requests_per_second == 3.0
    assert cfg.rate_limit.debounce_ms == 2000


def test_parse_notion_dashboard_clamps_extreme_values():
    cfg = _parse_notion_dashboard_config({
        "notion_dashboard": {
            "retry": {"max_attempts": 999, "backoff_seconds": 0.01},
            "rate_limit": {"requests_per_second": 100, "debounce_ms": 999999},
            "sync_outbox": {"max_retry_attempts": 99999},
        }
    })
    assert cfg.retry.max_attempts == 10
    assert cfg.retry.backoff_seconds == 0.5
    assert cfg.rate_limit.requests_per_second == 10.0
    assert cfg.rate_limit.debounce_ms == 30000
    assert cfg.sync_outbox.max_retry_attempts == 100


def test_parse_notion_dashboard_rejects_non_bool_enabled():
    cfg = _parse_notion_dashboard_config({
        "notion_dashboard": {"enabled": "yes"}
    })
    assert cfg.enabled is False


def test_parse_notion_dashboard_rejects_empty_env_name():
    cfg = _parse_notion_dashboard_config({
        "notion_dashboard": {"api_token_env": ""}
    })
    assert cfg.api_token_env == "HOKUSAI_NOTION_API_TOKEN"


# ---------------------------------------------------------------------------
# SQLiteStore: outbox / errors
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> SQLiteStore:
    with tempfile.TemporaryDirectory() as tmp:
        yield SQLiteStore(Path(tmp) / "test.db")


def test_enqueue_notion_sync_is_idempotent(store: SQLiteStore):
    is_new1 = store.enqueue_notion_sync("k1", "wf-1", "phase_changed", {"a": 1})
    is_new2 = store.enqueue_notion_sync("k1", "wf-1", "phase_changed", {"a": 1})
    assert is_new1 is True
    assert is_new2 is False
    pending = store.list_pending_notion_sync()
    assert len(pending) == 1
    assert pending[0]["payload"] == {"a": 1}


def test_mark_notion_sync_succeeded_removes_entry(store: SQLiteStore):
    store.enqueue_notion_sync("k1", "wf-1", "phase_changed", {})
    store.mark_notion_sync_succeeded("k1")
    assert store.count_notion_sync_pending() == 0


def test_mark_notion_sync_failed_increments_attempts(store: SQLiteStore):
    store.enqueue_notion_sync("k1", "wf-1", "phase_changed", {})
    store.mark_notion_sync_failed("k1", "boom", "2026-05-05T10:00:00")
    pending = store.list_pending_notion_sync()
    assert pending[0]["attempts"] == 1
    assert pending[0]["last_error"] == "boom"


def test_move_notion_sync_to_error_creates_error_record(store: SQLiteStore):
    store.enqueue_notion_sync("k1", "wf-1", "phase_changed", {"x": 1})
    store.mark_notion_sync_failed("k1", "first", "2026-05-05T10:00:00")
    store.move_notion_sync_to_error("k1", "permanent")
    assert store.count_notion_sync_pending() == 0
    assert store.count_notion_sync_errors() == 1


# ---------------------------------------------------------------------------
# NotionAPIClient
# ---------------------------------------------------------------------------


class _FakeResponse:
    """urlopen のコンテキストマネージャを模倣"""

    def __init__(self, body: dict, status: int = 200):
        self._body = json.dumps(body).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _make_http_error(status: int, message: str = "boom", retry_after: str | None = None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    body = json.dumps({"message": message}).encode("utf-8")
    return urllib.error.HTTPError(
        url="https://api.notion.com/v1/test",
        code=status,
        msg=message,
        hdrs=headers,
        fp=BytesIO(body),
    )


def test_notion_api_client_requires_token():
    with pytest.raises(ValueError):
        NotionAPIClient(api_token="")


def test_notion_api_client_create_page_success(monkeypatch):
    monkeypatch.setattr(
        client_module.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse({"id": "page-123"}),
    )
    api = NotionAPIClient(api_token="secret", requests_per_second=100)
    result = api.create_page({"parent": {"database_id": "db1"}})
    assert result["id"] == "page-123"


def test_notion_api_client_4xx_error_raises_immediately(monkeypatch):
    def fail(req, timeout=None):
        raise _make_http_error(401, "unauthorized")

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fail)
    api = NotionAPIClient(
        api_token="secret", max_attempts=3, requests_per_second=100, backoff_seconds=0.01
    )
    with pytest.raises(NotionAPIError) as excinfo:
        api.create_page({})
    assert excinfo.value.status == 401
    # token がメッセージに含まれないこと
    assert "secret" not in str(excinfo.value)


def test_notion_api_client_429_retries_then_succeeds(monkeypatch):
    call_count = {"n": 0}

    def maybe_fail(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _make_http_error(429, "rate limited", retry_after="1")
        return _FakeResponse({"id": "page-1"})

    monkeypatch.setattr(client_module.urllib.request, "urlopen", maybe_fail)
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    api = NotionAPIClient(
        api_token="secret", max_attempts=3, requests_per_second=100, backoff_seconds=0.01
    )
    result = api.create_page({})
    assert result["id"] == "page-1"
    assert call_count["n"] == 2


def test_notion_api_client_5xx_retries_then_fails(monkeypatch):
    def fail(req, timeout=None):
        raise _make_http_error(503, "server")

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fail)
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    api = NotionAPIClient(
        api_token="secret", max_attempts=2, requests_per_second=100, backoff_seconds=0.01
    )
    with pytest.raises(NotionAPIError) as excinfo:
        api.create_page({})
    assert excinfo.value.status == 503


def test_notion_api_client_429_eventually_raises(monkeypatch):
    def always_fail(req, timeout=None):
        raise _make_http_error(429, "rate", retry_after="1")

    monkeypatch.setattr(client_module.urllib.request, "urlopen", always_fail)
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    api = NotionAPIClient(
        api_token="secret", max_attempts=2, requests_per_second=100, backoff_seconds=0.01
    )
    with pytest.raises(NotionRateLimitError):
        api.create_page({})


# ---------------------------------------------------------------------------
# WorkflowsDBClient
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


def test_workflows_db_creates_when_not_exists():
    api = _RecordingAPI(query_result=[])
    client = WorkflowsDBClient(api=api, database_id="db1")
    client.apply_event("workflow_started", {
        "workflow_id": "wf-1",
        "task_title": "Test",
        "status": "running",
        "current_phase": 1,
        "started_at": "2026-05-05T00:00:00",
    })
    actions = [c[0] for c in api.calls]
    assert actions == ["query", "create"]
    create_payload = api.calls[1][1]
    assert create_payload["parent"] == {"database_id": "db1"}
    props = create_payload["properties"]
    assert props["Workflow ID"]["rich_text"][0]["text"]["content"] == "wf-1"
    assert props["Status"]["select"]["name"] == "Running"
    assert props["Current Phase"]["number"] == 1


def test_workflows_db_updates_when_exists():
    api = _RecordingAPI(query_result=[{"id": "page-existing"}])
    client = WorkflowsDBClient(api=api, database_id="db1")
    client.apply_event("phase_changed", {
        "workflow_id": "wf-1",
        "current_phase": 5,
        "current_phase_name": "Phase 5: 実装",
    })
    actions = [c[0] for c in api.calls]
    assert actions == ["query", "update"]
    update_payload = api.calls[1][1]
    assert update_payload["page_id"] == "page-existing"
    assert update_payload["properties"]["Current Phase"]["number"] == 5


def test_workflows_db_requires_workflow_id():
    api = _RecordingAPI()
    client = WorkflowsDBClient(api=api, database_id="db1")
    with pytest.raises(ValueError):
        client.apply_event("workflow_started", {"task_title": "x"})


def test_workflows_db_status_label_mapping():
    api = _RecordingAPI(query_result=[{"id": "p"}])
    client = WorkflowsDBClient(api=api, database_id="db1")
    client.apply_event("terminal_status_changed", {
        "workflow_id": "wf-1",
        "status": "waiting_for_human",
        "waiting_reason": "branch_hygiene",
        "next_action": "hokusai continue wf-1",
    })
    props = api.calls[-1][1]["properties"]
    assert props["Status"]["select"]["name"] == "Waiting for Human"
    assert props["Waiting Reason"]["select"]["name"] == "branch_hygiene"


def test_workflows_db_rejects_empty_database_id():
    api = _RecordingAPI()
    with pytest.raises(ValueError):
        WorkflowsDBClient(api=api, database_id="")


# ---------------------------------------------------------------------------
# property_not_found リトライ（DB スキーマ差異の吸収）
# ---------------------------------------------------------------------------


class _PropertyNotFoundRetryAPI(_RecordingAPI):
    """update_page / create_page で property_not_found を返してから成功するスタブ。"""

    def __init__(self, *, query_result, missing_props: list[str]):
        super().__init__(query_result=query_result)
        self._missing_queue = list(missing_props)

    def update_page(self, page_id: str, payload: dict) -> dict:
        self.calls.append(("update", {"page_id": page_id, **payload}))
        if self._missing_queue:
            from hokusai.integrations.notion_dashboard.client import NotionAPIError

            missing = self._missing_queue.pop(0)
            raise NotionAPIError(
                400,
                f'"{missing}" is not a property that exists.',
                code="validation_error",
            )
        return {"id": page_id}


def test_workflows_db_retries_when_property_not_found():
    """DB スキーマに無いプロパティを除外して再試行する。"""
    api = _PropertyNotFoundRetryAPI(
        query_result=[{"id": "page-1"}],
        missing_props=["Design Status", "Miro URL"],
    )
    client = WorkflowsDBClient(api=api, database_id="db1")
    client.apply_event("phase_changed", {
        "workflow_id": "wf-1",
        "task_title": "Test",
        "status": "running",
        "current_phase": 5,
        "design_integration_status": "ok",
        "miro_url": "https://miro.com/x",
    })
    # 3 回目の update_page で成功（property を 2 件除外して再試行）
    update_calls = [c for c in api.calls if c[0] == "update"]
    assert len(update_calls) == 3
    final_props = update_calls[-1][1]["properties"]
    assert "Design Status" not in final_props
    assert "Miro URL" not in final_props
    # 既存プロパティは残っている
    assert "Status" in final_props
    assert "Workflow ID" in final_props


def test_workflows_db_propagates_other_errors():
    """property_not_found 以外のエラーは即座に伝播する。"""
    from hokusai.integrations.notion_dashboard.client import NotionAPIError

    class _UnauthorizedAPI(_RecordingAPI):
        def update_page(self, page_id: str, payload: dict) -> dict:
            self.calls.append(("update", {"page_id": page_id, **payload}))
            raise NotionAPIError(401, "Unauthorized", code="unauthorized")

    api = _UnauthorizedAPI(query_result=[{"id": "page-1"}])
    client = WorkflowsDBClient(api=api, database_id="db1")
    with pytest.raises(NotionAPIError) as exc_info:
        client.apply_event("phase_changed", {
            "workflow_id": "wf-1",
            "status": "running",
        })
    assert exc_info.value.status == 401
    update_calls = [c for c in api.calls if c[0] == "update"]
    assert len(update_calls) == 1, "401 はリトライしないので 1 回のみ"


# ---------------------------------------------------------------------------
# NotionSyncDispatcher
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


def test_dispatcher_is_configured_false_when_disabled():
    disp = NotionSyncDispatcher(store=None, config=_make_config(enabled=False))
    assert disp.is_configured() is False


def test_dispatcher_is_configured_false_when_token_missing(monkeypatch):
    monkeypatch.delenv("TEST_TOKEN", raising=False)
    monkeypatch.setenv("TEST_DB", "db1")
    disp = NotionSyncDispatcher(store=None, config=_make_config())
    assert disp.is_configured() is False


def test_dispatcher_dispatch_skips_when_not_configured(store: SQLiteStore, monkeypatch):
    monkeypatch.delenv("TEST_TOKEN", raising=False)
    disp = NotionSyncDispatcher(store=store, config=_make_config())
    result = disp.dispatch("phase_changed", {"workflow_id": "wf-1"})
    assert result is False
    assert store.count_notion_sync_pending() == 0


def test_dispatcher_success_path(store: SQLiteStore, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "db1")

    sent: list[dict] = []

    def fake_send(self, event_type, payload, **kwargs):
        sent.append({"event_type": event_type, "payload": payload})

    monkeypatch.setattr(NotionSyncDispatcher, "_send_to_notion", fake_send)

    disp = NotionSyncDispatcher(store=store, config=_make_config())
    result = disp.dispatch("phase_changed", {
        "workflow_id": "wf-1",
        "current_phase": 5,
    })
    assert result is True
    assert len(sent) == 1
    assert sent[0]["event_type"] == "phase_changed"
    assert store.count_notion_sync_pending() == 0


def test_dispatcher_failure_enqueues_to_outbox(store: SQLiteStore, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "db1")

    def raising_send(self, event_type, payload, **kwargs):
        raise NotionAPIError(503, "server error")

    monkeypatch.setattr(NotionSyncDispatcher, "_send_to_notion", raising_send)

    disp = NotionSyncDispatcher(store=store, config=_make_config())
    result = disp.dispatch("phase_changed", {
        "workflow_id": "wf-1",
        "current_phase": 5,
    })
    assert result is False
    assert store.count_notion_sync_pending() == 1
    pending = store.list_pending_notion_sync()
    assert pending[0]["event_type"] == "phase_changed"
    assert pending[0]["attempts"] == 1


def test_dispatcher_does_not_propagate_unexpected_exception(store: SQLiteStore, monkeypatch):
    """ワークフロー本体を止めないこと"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "db1")

    def raising_send(self, event_type, payload, **kwargs):
        raise RuntimeError("totally unexpected")

    monkeypatch.setattr(NotionSyncDispatcher, "_send_to_notion", raising_send)

    disp = NotionSyncDispatcher(store=store, config=_make_config())
    # 例外が漏れないこと
    result = disp.dispatch("phase_changed", {"workflow_id": "wf-1"})
    assert result is False


def test_dispatcher_idempotency_key_built_from_payload(store: SQLiteStore, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "db1")

    def raising_send(self, event_type, payload, **kwargs):
        raise NotionAPIError(500, "x")

    monkeypatch.setattr(NotionSyncDispatcher, "_send_to_notion", raising_send)

    disp = NotionSyncDispatcher(store=store, config=_make_config())
    # 同じ payload で 2 回失敗 → outbox は 1 件
    disp.dispatch("phase_changed", {"workflow_id": "wf-1", "current_phase": 5, "revision": "1"})
    disp.dispatch("phase_changed", {"workflow_id": "wf-1", "current_phase": 5, "revision": "1"})
    assert store.count_notion_sync_pending() == 1


def test_dispatcher_retry_pending_succeeds(store: SQLiteStore, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "db1")

    # 1 回目失敗、2 回目（retry_pending）成功
    call_count = {"n": 0}

    def maybe_fail(self, event_type, payload, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise NotionAPIError(500, "transient")

    monkeypatch.setattr(NotionSyncDispatcher, "_send_to_notion", maybe_fail)

    disp = NotionSyncDispatcher(store=store, config=_make_config())
    disp.dispatch("phase_changed", {"workflow_id": "wf-1", "current_phase": 5})
    assert store.count_notion_sync_pending() == 1

    result = disp.retry_pending()
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert store.count_notion_sync_pending() == 0


def test_dispatcher_retry_pending_moves_to_error_after_max_attempts(
    store: SQLiteStore, monkeypatch
):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "db1")

    def always_fail(self, event_type, payload, **kwargs):
        raise NotionAPIError(500, "persistent")

    monkeypatch.setattr(NotionSyncDispatcher, "_send_to_notion", always_fail)

    cfg = _make_config()
    cfg.sync_outbox.max_retry_attempts = 2  # 1 回目 dispatch + 1 回目 retry → error へ
    disp = NotionSyncDispatcher(store=store, config=cfg)

    disp.dispatch("phase_changed", {"workflow_id": "wf-1", "current_phase": 5})
    # 1 回目 retry → 試行 2 回目 → max 到達で error queue へ
    result = disp.retry_pending()
    assert result["moved_to_error"] == 1
    assert store.count_notion_sync_pending() == 0
    assert store.count_notion_sync_errors() == 1


def test_dispatcher_safe_error_message_does_not_include_token():
    err = NotionAPIError(401, "Bearer secret-token-xxx invalid")
    msg = NotionSyncDispatcher._safe_error_message(err)
    assert "401" in msg
    # NotionAPIError 経由なら message は API 側のテキストなので token は含まれない想定
    # 実装上、token は header にしか載らない（client.py で _send は token を payload や
    # exception に含めないことを保証している）
    assert "NotionAPIError" in msg


# ---------------------------------------------------------------------------
# PullRequestsDBClient
# ---------------------------------------------------------------------------


from hokusai.integrations.notion_dashboard.pull_requests_db import PullRequestsDBClient


def test_pull_requests_db_create_record_minimal():
    api = _RecordingAPI()
    client = PullRequestsDBClient(api=api, database_id="pr-db")
    client.create_record(
        pr_number=123,
        url="https://gitlab.com/x/y/-/merge_requests/123",
    )
    assert len(api.calls) == 1
    action, payload = api.calls[0]
    assert action == "create"
    props = payload["properties"]
    assert props["PR Number"]["title"][0]["text"]["content"] == "123"
    assert props["URL"]["url"] == "https://gitlab.com/x/y/-/merge_requests/123"
    assert props["Status"]["select"]["name"] == "Draft"
    assert "Created At" in props


def test_pull_requests_db_create_record_with_repository_and_workflow():
    api = _RecordingAPI()
    client = PullRequestsDBClient(api=api, database_id="pr-db")
    client.create_record(
        pr_number=45,
        url="https://gitlab.com/x/y/-/merge_requests/45",
        repository="Backend",
        workflow_page_id="wf-page-id",
        status="Open",
        created_at="2026-05-05T10:00:00",
    )
    props = api.calls[0][1]["properties"]
    assert props["Repository"]["select"]["name"] == "Backend"
    assert props["Workflow"]["relation"][0]["id"] == "wf-page-id"
    assert props["Status"]["select"]["name"] == "Open"
    assert props["Created At"]["date"]["start"] == "2026-05-05T10:00:00"


def test_pull_requests_db_find_by_pr_number_returns_none_when_empty():
    api = _RecordingAPI(query_result=[])
    client = PullRequestsDBClient(api=api, database_id="pr-db")
    assert client.find_by_pr_number(99) is None


def test_pull_requests_db_find_by_pr_number_with_repository_filter():
    api = _RecordingAPI(query_result=[
        {"id": "page-A", "properties": {"Repository": {"select": {"name": "Backend"}}}},
        {"id": "page-B", "properties": {"Repository": {"select": {"name": "Frontend"}}}},
    ])
    client = PullRequestsDBClient(api=api, database_id="pr-db")
    assert client.find_by_pr_number(10, repository="Frontend") == "page-B"


def test_pull_requests_db_rejects_empty_database_id():
    api = _RecordingAPI()
    with pytest.raises(ValueError):
        PullRequestsDBClient(api=api, database_id="")


# ---------------------------------------------------------------------------
# Dispatcher: pr_created routing
# ---------------------------------------------------------------------------


def test_dispatcher_routes_pr_created_to_pr_db_when_configured(
    store: SQLiteStore, monkeypatch
):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_PR_DB", "pr-db")

    cfg = _make_config()
    cfg.pull_requests_db_id_env = "TEST_PR_DB"

    api = _RecordingAPI(query_result=[])  # workflow query 用

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)

    payload = {
        "workflow_id": "wf-1",
        "pull_requests": [
            {"number": 123, "url": "https://x/pr/123", "repository": "Backend"},
        ],
    }
    result = disp.dispatch("pr_created", payload)
    assert result is True
    actions = [c[0] for c in api.calls]
    # Workflows DB 検索 + 更新（apply_event 経由） + PR DB 検索 + 作成
    assert "create" in actions  # PR DB レコード作成
    assert "query" in actions   # Workflows DB の page_id 解決


def test_dispatcher_pr_created_skips_pr_db_when_db_id_unset(
    store: SQLiteStore, monkeypatch
):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.delenv("TEST_PR_DB", raising=False)

    cfg = _make_config()
    cfg.pull_requests_db_id_env = "TEST_PR_DB"

    api = _RecordingAPI(query_result=[])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch("pr_created", {
        "workflow_id": "wf-1",
        "pull_requests": [{"number": 1, "url": "https://x/1"}],
    })
    # Workflows DB の更新は走るが PR DB の create は走らない
    assert result is True
    create_calls = [c for c in api.calls if c[0] == "create"]
    # PR DB の create が無いこと（Workflows DB は update / create のいずれか）
    pr_db_creates = [
        c for c in create_calls
        if c[1].get("parent", {}).get("database_id") == "pr-db"
    ]
    assert pr_db_creates == []


class _DBAwareAPI:
    """database_id 別に query 結果を切り替えるテスト用 API モック"""

    def __init__(self, query_results_by_db: dict[str, list]):
        self.calls: list[tuple[str, dict]] = []
        self._query_results = query_results_by_db
        self._page_id_counter = 0

    def query_database(self, database_id: str, *, filter_: dict | None = None) -> dict:
        self.calls.append(("query", {"database_id": database_id, "filter": filter_}))
        return {"results": self._query_results.get(database_id, [])}

    def create_page(self, payload: dict) -> dict:
        self.calls.append(("create", payload))
        self._page_id_counter += 1
        return {"id": f"page-new-{self._page_id_counter}"}

    def update_page(self, page_id: str, payload: dict) -> dict:
        self.calls.append(("update", {"page_id": page_id, **payload}))
        return {"id": page_id}


def test_dispatcher_pr_created_passes_workflow_page_id_to_pr_db(
    store: SQLiteStore, monkeypatch
):
    """apply_event の戻り値の page_id が Pull Requests DB レコードに relation として渡る"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_PR_DB", "pr-db")

    cfg = _make_config()
    cfg.pull_requests_db_id_env = "TEST_PR_DB"

    # Workflows DB には既存ページ、PR DB には何もない（重複なし）
    api = _DBAwareAPI(query_results_by_db={
        "wf-db": [{"id": "wf-page-xyz"}],
        "pr-db": [],
    })

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch("pr_created", {
        "workflow_id": "wf-1",
        "pull_requests": [
            {"number": 100, "url": "https://x/100", "repository": "Backend"},
        ],
    })
    assert result is True

    pr_db_creates = [
        c for c in api.calls
        if c[0] == "create"
        and c[1].get("parent", {}).get("database_id") == "pr-db"
    ]
    assert len(pr_db_creates) == 1
    props = pr_db_creates[0][1]["properties"]
    assert "Workflow" in props
    assert props["Workflow"]["relation"][0]["id"] == "wf-page-xyz"


def test_dispatcher_pr_created_uses_new_page_id_when_workflow_is_new(
    store: SQLiteStore, monkeypatch
):
    """新規 workflow（apply_event が create を呼ぶ）でも、create の戻り値から page_id を relation に渡す"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_PR_DB", "pr-db")

    cfg = _make_config()
    cfg.pull_requests_db_id_env = "TEST_PR_DB"

    # 両 DB ともに既存なし → Workflows は新規作成、PR も新規作成
    api = _DBAwareAPI(query_results_by_db={"wf-db": [], "pr-db": []})

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    disp.dispatch("pr_created", {
        "workflow_id": "wf-1",
        "pull_requests": [
            {"number": 200, "url": "https://x/200", "repository": "Backend"},
        ],
    })

    # Workflows DB に新規作成された page_id（page-new-1）が PR DB の relation に入る
    pr_db_creates = [
        c for c in api.calls
        if c[0] == "create"
        and c[1].get("parent", {}).get("database_id") == "pr-db"
    ]
    assert len(pr_db_creates) == 1
    props = pr_db_creates[0][1]["properties"]
    assert props["Workflow"]["relation"][0]["id"] == "page-new-1"


def test_dispatcher_pr_created_skips_existing_prs(store: SQLiteStore, monkeypatch):
    """既存 PR レコードがあれば重複作成しない"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_PR_DB", "pr-db")

    cfg = _make_config()
    cfg.pull_requests_db_id_env = "TEST_PR_DB"

    # Workflows DB 検索結果: 既存ワークフローページ
    # PR DB 検索結果: 既存 PR レコードあり
    api = _RecordingAPI(query_result=[{"id": "existing-page"}])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch("pr_created", {
        "workflow_id": "wf-1",
        "pull_requests": [{"number": 99, "url": "https://x/99"}],
    })
    assert result is True
    # PR DB の create が走らない（既存があるため）
    creates = [c for c in api.calls if c[0] == "create"]
    assert creates == []


# ---------------------------------------------------------------------------
# WorkflowRunner: Notion sync hooks (top-level helpers)
# ---------------------------------------------------------------------------


from hokusai import workflow as workflow_module
from hokusai.state import PhaseStatus


def _phases_with(phase_num: int, status: str) -> dict:
    phases = {i: {"status": PhaseStatus.PENDING.value, "retry_count": 0} for i in range(1, 11)}
    phases[phase_num]["status"] = status
    return phases


def test_build_notion_payload_includes_revision_from_retry_count():
    state = {
        "workflow_id": "wf-1",
        "current_phase": 5,
        "phases": {5: {"retry_count": 3}},
    }
    p = workflow_module._build_notion_payload(state, status="running")
    assert p["workflow_id"] == "wf-1"
    assert p["current_phase"] == 5
    assert p["revision"] == "3"
    assert p["status"] == "running"
    assert "last_updated" in p


def test_build_notion_payload_handles_missing_phase_info():
    p = workflow_module._build_notion_payload({"workflow_id": "wf-2"})
    assert p["workflow_id"] == "wf-2"
    assert p["revision"] == "0"


def test_build_notion_payload_excludes_design_when_no_url():
    """design_integration_status=no_url の state では design 系キーを送らない。

    既存 DB に Design Review Required 等が無い環境で property_not_found を
    起こさないための安全策。
    """
    state = {
        "workflow_id": "wf-3",
        "design_integration_status": "no_url",
        "design_review_required": False,
    }
    p = workflow_module._build_notion_payload(state)
    assert "design_integration_status" not in p
    assert "design_review_required" not in p
    assert "miro_url" not in p
    assert "figma_url" not in p


def test_build_notion_payload_excludes_design_when_not_configured():
    """design_integration_status=not_configured では design 系キーを送らない。"""
    state = {
        "workflow_id": "wf-4",
        "design_integration_status": "not_configured",
        "design_review_required": False,
    }
    p = workflow_module._build_notion_payload(state)
    for key in ("design_integration_status", "design_review_required", "miro_url", "figma_url"):
        assert key not in p


def test_build_notion_payload_includes_design_when_ok():
    """design_integration_status=ok では design 系キーを送る。"""
    state = {
        "workflow_id": "wf-5",
        "design_integration_status": "ok",
        "design_review_required": True,
        "figma_url": "https://www.figma.com/file/Abc12345DEF/Test",
    }
    p = workflow_module._build_notion_payload(state)
    assert p["design_integration_status"] == "ok"
    assert p["design_review_required"] is True
    assert p["figma_url"] == "https://www.figma.com/file/Abc12345DEF/Test"


def test_build_notion_payload_includes_design_when_failed():
    """design_integration_status=failed でも送る（失敗が運用上見える必要がある）。"""
    state = {
        "workflow_id": "wf-6",
        "design_integration_status": "failed",
        "design_review_required": False,
        "figma_url": "https://www.figma.com/file/Abc12345DEF/Test",
    }
    p = workflow_module._build_notion_payload(state)
    assert p["design_integration_status"] == "failed"


def _make_runner():
    """SQLite を temp に逃がして WorkflowRunner を生成"""
    import tempfile
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    tmp = tempfile.mkdtemp()
    cfg = WorkflowConfig(
        data_dir=Path(tmp),
        database_path=Path(tmp) / "wf.db",
        checkpoint_db_path=Path(tmp) / "cp.db",
    )
    set_config(cfg)
    return workflow_module.WorkflowRunner()


def _capture_dispatch_on(runner, calls: list) -> None:
    """instance attribute として _safe_notion_dispatch を差し替え、calls に記録"""
    def fake(event_type, payload):
        calls.append({"event_type": event_type, "payload": payload})
    runner._safe_notion_dispatch = fake  # type: ignore[method-assign]


def test_emit_terminal_notion_sync_waiting_for_human():
    runner = _make_runner()
    calls: list = []
    _capture_dispatch_on(runner, calls)

    runner._emit_terminal_notion_sync(
        interrupt_reason="waiting_for_human",
        final_values={
            "workflow_id": "wf-1",
            "human_input_request": "branch_hygiene",
        },
    )
    assert len(calls) == 1
    call = calls[0]
    assert call["event_type"] == "terminal_status_changed"
    assert call["payload"]["status"] == "waiting_for_human"
    assert call["payload"]["waiting_reason"] == "branch_hygiene"
    assert "hokusai continue wf-1" in call["payload"]["next_action"]


def test_emit_terminal_notion_sync_failed_on_loop_detected():
    runner = _make_runner()
    calls: list = []
    _capture_dispatch_on(runner, calls)

    runner._emit_terminal_notion_sync(
        interrupt_reason="loop_detected",
        final_values={"workflow_id": "wf-2"},
    )
    assert calls[-1]["payload"]["status"] == "failed"


def test_emit_terminal_notion_sync_user_aborted_does_not_dispatch():
    runner = _make_runner()
    calls: list = []
    _capture_dispatch_on(runner, calls)

    runner._emit_terminal_notion_sync(
        interrupt_reason="user_aborted",
        final_values={"workflow_id": "wf-3"},
    )
    assert calls == []


def test_emit_terminal_notion_sync_done_when_phase10_completed():
    runner = _make_runner()
    calls: list = []
    _capture_dispatch_on(runner, calls)

    runner._emit_terminal_notion_sync(
        interrupt_reason=None,
        final_values={
            "workflow_id": "wf-4",
            "phases": _phases_with(10, PhaseStatus.COMPLETED.value),
        },
    )
    assert calls[-1]["payload"]["status"] == "done"


def test_emit_terminal_notion_sync_no_dispatch_when_phase10_pending():
    runner = _make_runner()
    calls: list = []
    _capture_dispatch_on(runner, calls)

    runner._emit_terminal_notion_sync(
        interrupt_reason=None,
        final_values={
            "workflow_id": "wf-5",
            "phases": _phases_with(10, PhaseStatus.PENDING.value),
        },
    )
    assert calls == []


def test_safe_notion_dispatch_swallows_exceptions():
    """WorkflowRunner._safe_notion_dispatch は dispatcher の例外を握り潰す"""
    runner = _make_runner()

    def raising(event_type, payload):
        raise RuntimeError("boom")

    runner.notion_dispatcher.dispatch = raising  # type: ignore[assignment]
    # 例外が漏れないこと
    runner._safe_notion_dispatch("phase_changed", {"workflow_id": "x"})


# ---------------------------------------------------------------------------
# Phase C: ServiceStatusPageClient
# ---------------------------------------------------------------------------


from hokusai.integrations.notion_dashboard.service_status import (
    ServiceStatusPageClient,
    sync_service_status_to_notion,
)


class _RecordingAPIWithRequest(_RecordingAPI):
    """ServiceStatusPageClient が _request を直接呼ぶため stub を追加"""

    def __init__(self, *, query_result=None, list_children_result=None):
        super().__init__(query_result=query_result)
        self._list_children_result = list_children_result or []

    def _request(self, method: str, path: str, *, body=None) -> dict:
        self.calls.append((f"{method} {path}", body))
        if method == "GET" and path.endswith("/children"):
            return {"results": self._list_children_result}
        if method == "PATCH" and "/blocks/" in path:
            return {"archived": True}
        return {}

    def append_block_children(self, block_id: str, children: list[dict]) -> dict:
        self.calls.append(("append_blocks", {"block_id": block_id, "children": children}))
        return {"results": children}


def test_service_status_replace_snapshot_archives_existing_and_appends_new():
    api = _RecordingAPIWithRequest(
        list_children_result=[{"id": "block-A"}, {"id": "block-B"}]
    )
    client = ServiceStatusPageClient(api=api, page_id="page-svc")
    client.replace_snapshot([
        {"id": "gh", "label": "GitHub", "status": "connected"},
        {"id": "glab", "label": "GitLab", "status": "not_authenticated"},
    ])

    actions = [c[0] for c in api.calls]
    # children 一覧 → block A を archive → block B を archive → append
    assert actions[0] == "GET /blocks/page-svc/children"
    assert actions[1] == "PATCH /blocks/block-A"
    assert actions[2] == "PATCH /blocks/block-B"
    assert actions[3] == "append_blocks"

    appended_blocks = api.calls[3][1]["children"]
    # heading + paragraph (timestamp) + 2 service rows
    assert len(appended_blocks) == 4
    assert appended_blocks[0]["type"] == "heading_2"
    # サービス行に label と display ラベルが含まれる
    text_a = appended_blocks[2]["bulleted_list_item"]["rich_text"][0]["text"]["content"]
    text_b = appended_blocks[3]["bulleted_list_item"]["rich_text"][0]["text"]["content"]
    assert "GitHub" in text_a and "Connected" in text_a
    assert "GitLab" in text_b and "Not Authenticated" in text_b


def test_service_status_replace_snapshot_handles_no_children():
    api = _RecordingAPIWithRequest(list_children_result=[])
    client = ServiceStatusPageClient(api=api, page_id="page-svc")
    client.replace_snapshot([{"id": "gh", "label": "GitHub", "status": "connected"}])
    actions = [c[0] for c in api.calls]
    assert "PATCH" not in " ".join(actions)  # archive 呼び出しなし
    assert any("append_blocks" in a for a in actions)


def test_service_status_rejects_empty_page_id():
    api = _RecordingAPIWithRequest()
    with pytest.raises(ValueError):
        ServiceStatusPageClient(api=api, page_id="")


# ---------------------------------------------------------------------------
# Dispatcher: service_status_checked routing
# ---------------------------------------------------------------------------


def test_dispatcher_routes_service_status_checked(store: SQLiteStore, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_SVC_PAGE", "svc-page-id")

    cfg = _make_config()
    cfg.service_status_page_id_env = "TEST_SVC_PAGE"

    api = _RecordingAPIWithRequest(list_children_result=[])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch(
        "service_status_checked",
        {"services": [{"id": "gh", "label": "GitHub", "status": "connected"}]},
    )
    assert result is True
    actions = [c[0] for c in api.calls]
    assert any("append_blocks" in a for a in actions)


def test_dispatcher_service_status_no_workflow_id_required(store: SQLiteStore, monkeypatch):
    """service_status_checked は workflow_id 無しでも送信される"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_SVC_PAGE", "svc-page-id")

    cfg = _make_config()
    cfg.service_status_page_id_env = "TEST_SVC_PAGE"

    api = _RecordingAPIWithRequest(list_children_result=[])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    # workflow_id を含まない payload でも True が返る
    result = disp.dispatch("service_status_checked", {"services": []})
    assert result is True


def test_dispatcher_service_status_skips_when_page_id_unset(store: SQLiteStore, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.delenv("TEST_SVC_PAGE", raising=False)

    cfg = _make_config()
    cfg.service_status_page_id_env = "TEST_SVC_PAGE"

    api = _RecordingAPIWithRequest()

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    result = disp.dispatch("service_status_checked", {"services": []})
    # 送信スキップだが、エラーではないので True 扱い
    assert result is True
    # API 呼び出しが発生していない
    assert api.calls == []


def test_sync_service_status_to_notion_skips_when_not_configured(store: SQLiteStore):
    cfg = _make_config(enabled=False)
    disp = NotionSyncDispatcher(store=store, config=cfg)
    assert sync_service_status_to_notion(disp) is False


def test_sync_service_status_to_notion_dispatches(store: SQLiteStore, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_SVC_PAGE", "svc-page-id")

    cfg = _make_config()
    cfg.service_status_page_id_env = "TEST_SVC_PAGE"

    api = _RecordingAPIWithRequest(list_children_result=[])

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)

    # connection_status をモック
    from hokusai.integrations import connection_status as cs

    monkeypatch.setattr(
        cs,
        "get_all_statuses",
        lambda *, refresh=False, mode=cs.MODE_SHALLOW: {
            "services": [
                {"id": "gh", "label": "GitHub", "status": "connected"},
            ]
        },
    )

    result = sync_service_status_to_notion(disp)
    assert result is True
    actions = [c[0] for c in api.calls]
    assert any("append_blocks" in a for a in actions)


# ---------------------------------------------------------------------------
# Phase E: Notion ページ URL 解決 + Next Action テンプレート
# ---------------------------------------------------------------------------


def test_workflows_db_get_workflow_page_url_returns_url():
    api = _RecordingAPI(query_result=[
        {"id": "page-1", "url": "https://notion.so/workspace/wf-1-abcdef"}
    ])
    client = WorkflowsDBClient(api=api, database_id="db1")
    url = client.get_workflow_page_url("wf-1")
    assert url == "https://notion.so/workspace/wf-1-abcdef"


def test_workflows_db_get_workflow_page_url_returns_none_when_missing():
    api = _RecordingAPI(query_result=[])
    client = WorkflowsDBClient(api=api, database_id="db1")
    assert client.get_workflow_page_url("wf-missing") is None


def test_dispatcher_resolve_workflow_page_url_returns_none_when_not_configured(store):
    cfg = _make_config(enabled=False)
    disp = NotionSyncDispatcher(store=store, config=cfg)
    assert disp.resolve_workflow_page_url("wf-1") is None


def test_dispatcher_resolve_workflow_page_url_swallows_errors(store, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")

    class _Disp(NotionSyncDispatcher):
        def _get_workflows_client(self):
            class _ClientRaises:
                def get_workflow_page_url(self, workflow_id):
                    raise RuntimeError("boom")

            return _ClientRaises()

    disp = _Disp(store=store, config=_make_config())
    assert disp.resolve_workflow_page_url("wf-1") is None


def test_workflow_runner_enrich_state_with_notion_url():
    runner = _make_runner()
    runner.notion_dispatcher.resolve_workflow_page_url = (  # type: ignore[assignment]
        lambda wf: f"https://notion.so/{wf}"
    )
    enriched = runner._enrich_state_with_notion_url({"workflow_id": "wf-9"})
    assert enriched["notion_dashboard_url"] == "https://notion.so/wf-9"


def test_workflow_runner_enrich_state_returns_unchanged_when_no_url():
    runner = _make_runner()
    runner.notion_dispatcher.resolve_workflow_page_url = lambda wf: None  # type: ignore[assignment]
    state = {"workflow_id": "wf-1"}
    enriched = runner._enrich_state_with_notion_url(state)
    assert "notion_dashboard_url" not in enriched


def test_workflow_runner_enrich_state_preserves_state_on_exception():
    runner = _make_runner()

    def raising(wf):
        raise RuntimeError("boom")

    runner.notion_dispatcher.resolve_workflow_page_url = raising  # type: ignore[assignment]
    state = {"workflow_id": "wf-1"}
    enriched = runner._enrich_state_with_notion_url(state)
    assert enriched is state  # 例外時はそのまま


def test_next_action_template_for_branch_hygiene():
    msg = workflow_module._next_action_for_waiting_reason("branch_hygiene", "wf-x")
    assert "wf-x" in msg
    assert "ブランチ衛生" in msg


def test_next_action_template_for_unknown_reason_uses_fallback():
    msg = workflow_module._next_action_for_waiting_reason("unknown_reason", "wf-x")
    assert "wf-x" in msg
    assert "Operations Console" in msg


def test_next_action_template_for_review_fix():
    msg = workflow_module._next_action_for_waiting_reason("review_fix", "wf-1")
    assert "レビュー修正" in msg
    assert "hokusai continue wf-1" in msg


# ---------------------------------------------------------------------------
# Phase E: Slack payload に notion_dashboard_url が含まれる
# ---------------------------------------------------------------------------


def test_slack_payload_includes_notion_dashboard_url():
    from hokusai.integrations.notifications.slack import build_text_payload

    state = {
        "workflow_id": "wf-1",
        "task_url": "https://example.com/task/1",
        "current_phase": 5,
        "notion_dashboard_url": "https://notion.so/x/wf-1-abc",
    }
    payload = build_text_payload("waiting_for_human", state, reason="branch_hygiene")
    assert "Dashboard:" in payload["text"]
    assert "https://notion.so/x/wf-1-abc" in payload["text"]


def test_slack_payload_omits_dashboard_line_when_url_absent():
    from hokusai.integrations.notifications.slack import build_text_payload

    state = {"workflow_id": "wf-1", "current_phase": 1}
    payload = build_text_payload("workflow_started", state)
    assert "Dashboard:" not in payload["text"]


# ---------------------------------------------------------------------------
# CLI: sync-service-status
# ---------------------------------------------------------------------------


def test_cli_sync_service_status_skips_when_disabled(capsys, monkeypatch):
    """notion_dashboard.enabled=false なら 0 を返してスキップ表示"""
    from hokusai.cli_main import _handle_sync_service_status
    from hokusai.config import set_config
    from hokusai.config.models import NotionDashboardConfig, WorkflowConfig

    set_config(WorkflowConfig(notion_dashboard=NotionDashboardConfig(enabled=False)))
    rc = _handle_sync_service_status()
    captured = capsys.readouterr()
    assert rc == 0
    assert "スキップ" in captured.out


def test_cli_sync_service_status_skips_when_env_missing(capsys, monkeypatch, tmp_path):
    """enabled=true でも環境変数未設定なら 0 + skipped"""
    from hokusai.cli_main import _handle_sync_service_status
    from hokusai.config import set_config
    from hokusai.config.models import NotionDashboardConfig, WorkflowConfig

    monkeypatch.delenv("HOKUSAI_NOTION_API_TOKEN", raising=False)
    monkeypatch.delenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", raising=False)

    set_config(WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        notion_dashboard=NotionDashboardConfig(enabled=True),
    ))
    rc = _handle_sync_service_status()
    captured = capsys.readouterr()
    assert rc == 0
    assert "未設定" in captured.out


def test_cli_sync_service_status_returns_zero_on_success(
    capsys, monkeypatch, tmp_path
):
    """同期成功時は 0 + 成功メッセージ"""
    from hokusai.cli_main import _handle_sync_service_status
    from hokusai.config import set_config
    from hokusai.config.models import NotionDashboardConfig, WorkflowConfig

    set_config(WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        notion_dashboard=NotionDashboardConfig(enabled=True),
    ))

    # NotionSyncDispatcher.is_configured と sync_service_status_to_notion をスタブ
    from hokusai.integrations import notion_dashboard as nd

    monkeypatch.setattr(
        nd, "sync_service_status_to_notion", lambda dispatcher: True
    )
    monkeypatch.setattr(
        nd.NotionSyncDispatcher, "is_configured", lambda self: True
    )

    rc = _handle_sync_service_status()
    captured = capsys.readouterr()
    assert rc == 0
    assert "✓" in captured.out


def test_cli_sync_service_status_returns_one_on_failure(
    capsys, monkeypatch, tmp_path
):
    from hokusai.cli_main import _handle_sync_service_status
    from hokusai.config import set_config
    from hokusai.config.models import NotionDashboardConfig, WorkflowConfig

    set_config(WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        notion_dashboard=NotionDashboardConfig(enabled=True),
    ))

    from hokusai.integrations import notion_dashboard as nd

    monkeypatch.setattr(
        nd, "sync_service_status_to_notion", lambda dispatcher: False
    )
    monkeypatch.setattr(
        nd.NotionSyncDispatcher, "is_configured", lambda self: True
    )

    rc = _handle_sync_service_status()
    captured = capsys.readouterr()
    assert rc == 1
    assert "✗" in captured.out


# ---------------------------------------------------------------------------
# Last Sync / Sync Errors の Notion 反映
# ---------------------------------------------------------------------------


def test_workflows_db_writes_last_sync_property():
    api = _RecordingAPI(query_result=[{"id": "page-1"}])
    client = WorkflowsDBClient(api=api, database_id="db1")
    client.apply_event("phase_changed", {
        "workflow_id": "wf-1",
        "current_phase": 5,
        "last_sync": "2026-05-05T12:00:00",
    })
    update_payload = api.calls[-1][1]
    props = update_payload["properties"]
    assert "Last Sync" in props
    assert props["Last Sync"]["date"]["start"] == "2026-05-05T12:00:00"


def test_workflows_db_writes_sync_errors_summary_when_pending():
    api = _RecordingAPI(query_result=[{"id": "page-1"}])
    client = WorkflowsDBClient(api=api, database_id="db1")
    client.apply_event("phase_changed", {
        "workflow_id": "wf-1",
        "sync_errors": "保留 2 件 / 永続失敗 1 件",
    })
    props = api.calls[-1][1]["properties"]
    assert "Sync Errors" in props
    summary = props["Sync Errors"]["rich_text"][0]["text"]["content"]
    assert "保留 2 件" in summary
    assert "永続失敗 1 件" in summary


def test_workflows_db_clears_sync_errors_when_empty():
    api = _RecordingAPI(query_result=[{"id": "page-1"}])
    client = WorkflowsDBClient(api=api, database_id="db1")
    client.apply_event("phase_changed", {
        "workflow_id": "wf-1",
        "sync_errors": "",
    })
    props = api.calls[-1][1]["properties"]
    assert "Sync Errors" in props
    assert props["Sync Errors"]["rich_text"][0]["text"]["content"] == ""


def test_dispatcher_enrich_with_sync_status_no_pending(store: SQLiteStore):
    """outbox / errors が空なら sync_errors は空文字"""
    cfg = _make_config()
    disp = NotionSyncDispatcher(store=store, config=cfg)
    enriched = disp._enrich_with_sync_status({"workflow_id": "wf-1"})
    assert enriched["last_sync"]
    assert enriched["sync_errors"] == ""


def test_dispatcher_enrich_with_sync_status_includes_pending_count(store: SQLiteStore):
    cfg = _make_config()
    disp = NotionSyncDispatcher(store=store, config=cfg)
    # outbox に 2 件、errors に 1 件
    store.enqueue_notion_sync("k1", "wf-1", "phase_changed", {})
    store.enqueue_notion_sync("k2", "wf-1", "phase_changed", {})
    store.enqueue_notion_sync("k3", "wf-1", "phase_changed", {})
    store.move_notion_sync_to_error("k3", "fatal")

    enriched = disp._enrich_with_sync_status({"workflow_id": "wf-1"})
    assert "保留 2 件" in enriched["sync_errors"]
    assert "永続失敗 1 件" in enriched["sync_errors"]


def test_dispatcher_enrich_only_counts_target_workflow(store: SQLiteStore):
    """別 workflow の outbox 件数を混入しない"""
    cfg = _make_config()
    disp = NotionSyncDispatcher(store=store, config=cfg)
    store.enqueue_notion_sync("k1", "wf-OTHER", "phase_changed", {})
    enriched = disp._enrich_with_sync_status({"workflow_id": "wf-1"})
    assert enriched["sync_errors"] == ""


def test_dispatcher_enrich_returns_unchanged_when_no_workflow_id(store: SQLiteStore):
    cfg = _make_config()
    disp = NotionSyncDispatcher(store=store, config=cfg)
    payload = {"services": []}
    enriched = disp._enrich_with_sync_status(payload)
    assert enriched is payload


def test_dispatcher_dispatch_writes_last_sync_via_workflows_db(store: SQLiteStore, monkeypatch):
    """通常ワークフロー系イベントの送信時に Last Sync が Notion に書き戻される"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")

    cfg = _make_config()
    api = _DBAwareAPI(query_results_by_db={"wf-db": [{"id": "wf-page-1"}]})

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    disp.dispatch("phase_changed", {
        "workflow_id": "wf-1",
        "current_phase": 5,
    })

    # update が呼ばれ、その properties に Last Sync が含まれる
    update_calls = [c for c in api.calls if c[0] == "update"]
    assert len(update_calls) >= 1
    last_update = update_calls[-1][1]
    assert "Last Sync" in last_update["properties"]
    assert "Sync Errors" in last_update["properties"]


def test_enrich_with_sync_status_excludes_self_idempotency_key(store: SQLiteStore):
    """retry_pending 用に「自分自身のエントリ」を除外して件数を数える"""
    cfg = _make_config()
    disp = NotionSyncDispatcher(store=store, config=cfg)

    # outbox に自分自身のエントリ 1 件のみ
    store.enqueue_notion_sync("self-key", "wf-1", "phase_changed", {})

    # exclude なしだと「保留 1 件」
    enriched_with = disp._enrich_with_sync_status({"workflow_id": "wf-1"})
    assert "保留 1 件" in enriched_with["sync_errors"]

    # exclude すると「0 件 → 空」
    enriched_excluded = disp._enrich_with_sync_status(
        {"workflow_id": "wf-1"}, exclude_idempotency_key="self-key"
    )
    assert enriched_excluded["sync_errors"] == ""


def test_retry_pending_clears_sync_errors_on_last_success(
    store: SQLiteStore, monkeypatch
):
    """最後の保留分を再送成功した瞬間に Sync Errors が空になる"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")

    cfg = _make_config()
    api = _DBAwareAPI(query_results_by_db={"wf-db": [{"id": "wf-page-1"}]})

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)

    # 1 件だけ outbox に積む
    store.enqueue_notion_sync(
        "wf-1:phase_changed:5:0", "wf-1", "phase_changed",
        {"workflow_id": "wf-1", "current_phase": 5},
    )
    assert store.count_notion_sync_pending() == 1

    # 再送
    result = disp.retry_pending()
    assert result["succeeded"] == 1
    assert store.count_notion_sync_pending() == 0

    # update が呼ばれ、その properties の Sync Errors は空文字
    update_calls = [c for c in api.calls if c[0] == "update"]
    assert len(update_calls) >= 1
    sync_errors_property = update_calls[-1][1]["properties"]["Sync Errors"]
    summary = sync_errors_property["rich_text"][0]["text"]["content"]
    # 自分自身を除外した結果、「保留 1 件」が残らないこと
    assert summary == ""


def test_retry_pending_shows_remaining_pending_when_other_entries_exist(
    store: SQLiteStore, monkeypatch
):
    """1 件再送成功しても、まだ別の保留分が残っていれば Sync Errors にはその件数が入る"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")

    cfg = _make_config()
    api = _DBAwareAPI(query_results_by_db={"wf-db": [{"id": "wf-page-1"}]})

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)

    # 同じ workflow_id で 2 件積む
    store.enqueue_notion_sync(
        "k1", "wf-1", "phase_changed",
        {"workflow_id": "wf-1", "current_phase": 5},
    )
    store.enqueue_notion_sync(
        "k2", "wf-1", "phase_changed",
        {"workflow_id": "wf-1", "current_phase": 6},
    )

    # 1 回目の再送（k1 を成功）
    result = disp.retry_pending(limit=1)
    assert result["succeeded"] == 1
    assert store.count_notion_sync_pending() == 1

    # update properties の Sync Errors は「保留 1 件」（k2 が残存）
    update_calls = [c for c in api.calls if c[0] == "update"]
    sync_errors_property = update_calls[-1][1]["properties"]["Sync Errors"]
    summary = sync_errors_property["rich_text"][0]["text"]["content"]
    assert "保留 1 件" in summary


def test_dispatcher_pr_created_propagates_last_sync_and_sync_errors(
    store: SQLiteStore, monkeypatch
):
    """pr_created 経由の Workflows DB 更新でも Last Sync / Sync Errors が反映される"""
    monkeypatch.setenv("TEST_TOKEN", "secret")
    monkeypatch.setenv("TEST_DB", "wf-db")
    monkeypatch.setenv("TEST_PR_DB", "pr-db")

    cfg = _make_config()
    cfg.pull_requests_db_id_env = "TEST_PR_DB"

    api = _DBAwareAPI(query_results_by_db={
        "wf-db": [{"id": "wf-page-xyz"}],
        "pr-db": [],
    })

    class _Disp(NotionSyncDispatcher):
        def _get_api(self):
            return api  # type: ignore[return-value]

    disp = _Disp(store=store, config=cfg)
    disp.dispatch("pr_created", {
        "workflow_id": "wf-1",
        "pull_requests": [
            {"number": 100, "url": "https://x/100", "repository": "Backend"},
        ],
    })

    # Workflows DB の update payload に Last Sync / Sync Errors が含まれる
    wf_updates = [
        c for c in api.calls
        if c[0] == "update" and c[1].get("page_id") == "wf-page-xyz"
    ]
    assert len(wf_updates) >= 1
    props = wf_updates[-1][1]["properties"]
    assert "Last Sync" in props
    assert "Sync Errors" in props
    # GitLab MR URL も合わせて入る
    assert "GitLab MR" in props
    assert props["GitLab MR"]["url"] == "https://x/100"
