"""Work Items DB クライアントの単体テスト（Issue #38 / Workgraph Phase 2 / v0.7.0）

work_items_db.py の以下を検証する:
- build_dedupe_key: 入力が同じなら出力が同じで、16 文字 hex
- WorkItemsDBClient.find_by_dedupe_key: 既存検索の挙動
- WorkItemsDBClient.upsert_work_item: 新規作成 / 既存更新の分岐、Status の温存
- WorkItemsDBClient.update_status: 状態遷移専用 API
- relation（Workflow / Dependencies / Blocking Review Issues）の組み立て
- property_not_found 検出時のプロパティ除外リトライ
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.integrations.notion_dashboard.client import NotionAPIError
from hokusai.integrations.notion_dashboard.work_items_db import (
    CLAIM_TYPE_AGENT,
    CLAIM_TYPE_HUMAN,
    DEFAULT_LEASE_DURATION_SECONDS,
    LEASE_STATUS_ACTIVE,
    LEASE_STATUS_EXPIRED,
    LEASE_STATUS_RELEASED,
    STATUS_BLOCKED,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SKIPPED,
    WorkItemsDBClient,
    build_dedupe_key,
)


from tests._notion_test_helpers import FakeNotionAPIWithPruning as _FakeAPI


# ---------------------------------------------------------------------------
# build_dedupe_key
# ---------------------------------------------------------------------------


def test_build_dedupe_key_is_deterministic_and_16_hex():
    k1 = build_dedupe_key(workflow_id="wf-1", phase=4, title="auth refactor")
    k2 = build_dedupe_key(workflow_id="wf-1", phase=4, title="auth refactor")
    assert k1 == k2
    assert len(k1) == 16
    assert all(c in "0123456789abcdef" for c in k1)


def test_build_dedupe_key_differs_by_workflow_id():
    """同じ phase / title でも workflow_id が違えば別キー（Review Issues DB と同じ理由）"""
    k1 = build_dedupe_key(workflow_id="wf-1", phase=4, title="same")
    k2 = build_dedupe_key(workflow_id="wf-2", phase=4, title="same")
    assert k1 != k2


def test_build_dedupe_key_differs_by_phase():
    k1 = build_dedupe_key(workflow_id="wf-1", phase=4, title="same")
    k2 = build_dedupe_key(workflow_id="wf-1", phase=5, title="same")
    assert k1 != k2


def test_build_dedupe_key_normalizes_none_and_whitespace():
    """None / 前後空白は正規化されて同じキーになる"""
    k1 = build_dedupe_key(workflow_id=None, phase=None, title="   foo   ")
    k2 = build_dedupe_key(workflow_id="", phase=None, title="foo")
    assert k1 == k2


# ---------------------------------------------------------------------------
# WorkItemsDBClient コンストラクタ
# ---------------------------------------------------------------------------


def test_client_rejects_empty_database_id():
    with pytest.raises(ValueError, match="database_id"):
        WorkItemsDBClient(api=_FakeAPI(), database_id="")


# ---------------------------------------------------------------------------
# upsert_work_item: 新規作成
# ---------------------------------------------------------------------------


def test_upsert_creates_new_with_default_pending_status():
    api = _FakeAPI()
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.upsert_work_item(title="implement login", phase=5, workflow_id="wf-1")

    assert len(api.create_calls) == 1
    props = api.create_calls[0]["properties"]
    # 新規作成時は Status / Created At が含まれる
    assert props["Status"]["select"]["name"] == STATUS_PENDING
    assert "Created At" in props
    assert props["Title"]["title"][0]["text"]["content"] == "implement login"
    assert props["Phase"]["number"] == 5
    # Dedupe Key が自動生成されて含まれる
    assert "Dedupe Key" in props


def test_upsert_includes_workflow_relation_when_page_id_passed():
    api = _FakeAPI()
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.upsert_work_item(
        title="X", phase=4, workflow_id="wf-1", workflow_page_id="wf-page"
    )
    props = api.create_calls[0]["properties"]
    assert props["Workflow"]["relation"] == [{"id": "wf-page"}]


def test_upsert_includes_dependencies_relation():
    api = _FakeAPI()
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.upsert_work_item(
        title="X",
        phase=4,
        workflow_id="wf-1",
        dependency_page_ids=["dep-1", "dep-2"],
    )
    props = api.create_calls[0]["properties"]
    assert props["Dependencies"]["relation"] == [
        {"id": "dep-1"},
        {"id": "dep-2"},
    ]


def test_upsert_includes_blocking_review_issues_relation():
    api = _FakeAPI()
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.upsert_work_item(
        title="X",
        phase=4,
        workflow_id="wf-1",
        blocking_review_issue_page_ids=["ri-1"],
    )
    props = api.create_calls[0]["properties"]
    assert props["Blocking Review Issues"]["relation"] == [{"id": "ri-1"}]


def test_upsert_omits_empty_relations_to_avoid_clearing_existing():
    """空 list を渡しても relation キー自体が含まれない（既存依存を誤って消さない）"""
    api = _FakeAPI()
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.upsert_work_item(
        title="X",
        phase=4,
        workflow_id="wf-1",
        dependency_page_ids=[],
        blocking_review_issue_page_ids=[],
    )
    props = api.create_calls[0]["properties"]
    assert "Dependencies" not in props
    assert "Blocking Review Issues" not in props


# ---------------------------------------------------------------------------
# upsert_work_item: 既存更新（Status は温存）
# ---------------------------------------------------------------------------


def test_upsert_existing_updates_without_touching_status():
    """既存レコードの update では Status / Created At を payload に含めない
    （Phase 5 implement の in_progress → done 遷移や人手の遷移を巻き戻さない）"""
    api = _FakeAPI(existing_id="existing-page")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.upsert_work_item(
        title="X", phase=4, workflow_id="wf-1", description="updated"
    )

    assert len(api.create_calls) == 0
    assert len(api.update_calls) == 1
    page_id, payload = api.update_calls[0]
    assert page_id == "existing-page"
    props = payload["properties"]
    # update 時は Status / Created At は含まれない
    assert "Status" not in props
    assert "Created At" not in props
    # その他のプロパティは含まれる
    assert "Title" in props
    assert "Description" in props
    assert "Last Updated" in props


# ---------------------------------------------------------------------------
# update_status: 状態遷移専用 API
# ---------------------------------------------------------------------------


def test_update_status_writes_status_directly():
    api = _FakeAPI(existing_id="page-x")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.update_status("page-x", STATUS_IN_PROGRESS)

    assert len(api.update_calls) == 1
    _page_id, payload = api.update_calls[0]
    assert payload["properties"]["Status"]["select"]["name"] == STATUS_IN_PROGRESS


def test_update_status_rejects_invalid_status():
    client = WorkItemsDBClient(api=_FakeAPI(), database_id="wi-db")
    with pytest.raises(ValueError, match="Status の値が不正"):
        client.update_status("page-x", "not_a_real_status")


def test_update_status_accepts_all_seven_statuses():
    """schema 定義（setup.py）と enum 定数（work_items_db.py）が一致することを保証"""
    api = _FakeAPI()
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    for status in (
        STATUS_PENDING,
        STATUS_READY,
        STATUS_IN_PROGRESS,
        STATUS_BLOCKED,
        STATUS_DONE,
        STATUS_SKIPPED,
        STATUS_CANCELED,
    ):
        client.update_status("page-x", status)


# ---------------------------------------------------------------------------
# find_by_dedupe_key
# ---------------------------------------------------------------------------


def test_find_by_dedupe_key_returns_existing_id():
    api = _FakeAPI(existing_id="found-id")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    assert client.find_by_dedupe_key("abc1234567890def") == "found-id"


def test_find_by_dedupe_key_returns_none_when_empty():
    """空文字 dedupe_key は早期 None を返す（誤検索の抑止）"""
    client = WorkItemsDBClient(api=_FakeAPI(), database_id="wi-db")
    assert client.find_by_dedupe_key("") is None


# ---------------------------------------------------------------------------
# property_not_found リトライ
# ---------------------------------------------------------------------------


def test_property_not_found_retry_drops_missing_property():
    """Notion 側に該当プロパティが無い場合、その項目を除外して再試行する"""
    api = _FakeAPI(missing_property="Description")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    result = client.upsert_work_item(
        title="X", phase=4, workflow_id="wf-1", description="some desc"
    )
    # 2 回 create_page が呼ばれる（初回は Description あり、2 回目は除外済み）
    assert len(api.create_calls) == 2
    assert "Description" in api.create_calls[0]["properties"]
    assert "Description" not in api.create_calls[1]["properties"]
    assert result["id"] == "new-page-id"


# ---------------------------------------------------------------------------
# Lease API: claim_work_item / release_lease / expire_lease
# （Workgraph Phase 3 / Issue #42 / v0.8.0）
# ---------------------------------------------------------------------------


def test_claim_work_item_writes_active_lease_with_token():
    """claim_work_item は Lease Status=active / Started At / Expires At /
    Claimed By / Claim Type / Lease Token を書き込む"""
    api = _FakeAPI(existing_id="page-x")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.claim_work_item(
        "page-x", claimed_by="claude_code", lease_duration_seconds=600
    )

    assert len(api.update_calls) == 1
    page_id, payload = api.update_calls[0]
    assert page_id == "page-x"
    props = payload["properties"]
    assert props["Claimed By"]["rich_text"][0]["text"]["content"] == "claude_code"
    assert props["Claim Type"]["select"]["name"] == CLAIM_TYPE_AGENT
    assert props["Lease Status"]["select"]["name"] == LEASE_STATUS_ACTIVE
    assert "Lease Started At" in props
    assert "Lease Expires At" in props
    # token は uuid4 hex（32 文字）
    token = props["Lease Token"]["rich_text"][0]["text"]["content"]
    assert len(token) == 32
    # hex 文字のみ
    assert all(c in "0123456789abcdef" for c in token)


def test_claim_work_item_with_human_claim_type():
    """human claim type も受け付ける"""
    api = _FakeAPI(existing_id="page-x")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.claim_work_item(
        "page-x", claimed_by="alice@example.com", claim_type=CLAIM_TYPE_HUMAN
    )
    _, payload = api.update_calls[0]
    assert payload["properties"]["Claim Type"]["select"]["name"] == CLAIM_TYPE_HUMAN
    assert (
        payload["properties"]["Claimed By"]["rich_text"][0]["text"]["content"]
        == "alice@example.com"
    )


def test_claim_work_item_rejects_invalid_claim_type():
    client = WorkItemsDBClient(api=_FakeAPI(), database_id="wi-db")
    with pytest.raises(ValueError, match="Claim Type"):
        client.claim_work_item("page-x", claimed_by="x", claim_type="bot")


def test_claim_work_item_rejects_empty_claimed_by():
    client = WorkItemsDBClient(api=_FakeAPI(), database_id="wi-db")
    with pytest.raises(ValueError, match="claimed_by"):
        client.claim_work_item("page-x", claimed_by="")


def test_claim_work_item_rejects_nonpositive_duration():
    client = WorkItemsDBClient(api=_FakeAPI(), database_id="wi-db")
    with pytest.raises(ValueError, match="lease_duration_seconds"):
        client.claim_work_item(
            "page-x", claimed_by="x", lease_duration_seconds=0
        )
    with pytest.raises(ValueError, match="lease_duration_seconds"):
        client.claim_work_item(
            "page-x", claimed_by="x", lease_duration_seconds=-10
        )


def test_claim_work_item_default_duration_is_one_hour():
    """DEFAULT_LEASE_DURATION_SECONDS は 3600（1 時間）"""
    assert DEFAULT_LEASE_DURATION_SECONDS == 3600


def test_release_lease_writes_released_status_only():
    """release_lease は Lease Status を released に。Claimed By / Token は温存。"""
    api = _FakeAPI(existing_id="page-x")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.release_lease("page-x")

    assert len(api.update_calls) == 1
    _page_id, payload = api.update_calls[0]
    props = payload["properties"]
    assert props["Lease Status"]["select"]["name"] == LEASE_STATUS_RELEASED
    # Claimed By / Token は監査用に温存 → payload に含まれない（既存値を残す）
    assert "Claimed By" not in props
    assert "Lease Token" not in props
    # Last Updated は書き換える
    assert "Last Updated" in props


def test_expire_lease_writes_expired_status_only():
    """expire_lease は Lease Status を expired に。"""
    api = _FakeAPI(existing_id="page-x")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.expire_lease("page-x")

    assert len(api.update_calls) == 1
    _page_id, payload = api.update_calls[0]
    props = payload["properties"]
    assert props["Lease Status"]["select"]["name"] == LEASE_STATUS_EXPIRED
    assert "Last Updated" in props


def test_claim_then_release_lifecycle():
    """claim → release の典型 lifecycle を 2 回連続呼び出しで検証。"""
    api = _FakeAPI(existing_id="page-x")
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    client.claim_work_item("page-x", claimed_by="claude_code")
    client.release_lease("page-x")

    assert len(api.update_calls) == 2
    # 1 回目: active
    assert (
        api.update_calls[0][1]["properties"]["Lease Status"]["select"]["name"]
        == LEASE_STATUS_ACTIVE
    )
    # 2 回目: released
    assert (
        api.update_calls[1][1]["properties"]["Lease Status"]["select"]["name"]
        == LEASE_STATUS_RELEASED
    )


# ---------------------------------------------------------------------------
# list_ready_work_items_for_workflow（Issue #54 / Workgraph 完成）
# ---------------------------------------------------------------------------


class _PaginatedAPI:
    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.query_calls: list[dict] = []

    def query_database(self, database_id, *, filter_=None, start_cursor=None, page_size=None):
        self.query_calls.append({"filter": filter_, "start_cursor": start_cursor})
        idx = 0 if start_cursor is None else int(start_cursor.replace("cursor-", ""))
        results = self._pages[idx] if idx < len(self._pages) else []
        has_more = idx < len(self._pages) - 1
        return {
            "results": results,
            "has_more": has_more,
            "next_cursor": f"cursor-{idx + 1}" if has_more else None,
        }


def test_list_ready_work_items_for_workflow_returns_pages():
    api = _PaginatedAPI([[
        {"id": "wi-1", "properties": {"Status": {"select": {"name": "ready"}}}},
        {"id": "wi-2", "properties": {"Status": {"select": {"name": "in_progress"}}}},
    ]])
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    result = client.list_ready_work_items_for_workflow("wf-page")
    assert [r["id"] for r in result] == ["wi-1", "wi-2"]
    # filter: Status in {ready, in_progress} + Workflow contains
    call_filter = api.query_calls[0]["filter"]
    assert "and" in call_filter
    or_clause = next(c for c in call_filter["and"] if "or" in c)
    statuses = [c["select"]["equals"] for c in or_clause["or"]]
    assert sorted(statuses) == ["in_progress", "ready"]
    wf_clause = next(c for c in call_filter["and"] if c.get("property") == "Workflow")
    assert wf_clause["relation"]["contains"] == "wf-page"


def test_list_ready_work_items_for_workflow_returns_empty_for_blank_page_id():
    api = _PaginatedAPI([])
    client = WorkItemsDBClient(api=api, database_id="wi-db")
    assert client.list_ready_work_items_for_workflow("") == []
    assert client.list_ready_work_items_for_workflow(None) == []
    assert api.query_calls == []


def test_list_ready_work_items_returns_partial_on_api_failure():
    """API 失敗時は取得済み部分結果を保持して返す"""

    class _MidFailAPI:
        def __init__(self):
            self.calls = 0

        def query_database(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"results": [{"id": "wi-1"}], "has_more": True, "next_cursor": "c1"}
            raise NotionAPIError(503, "service unavailable")

    client = WorkItemsDBClient(api=_MidFailAPI(), database_id="wi-db")
    result = client.list_ready_work_items_for_workflow("wf-page")
    assert [r["id"] for r in result] == ["wi-1"]
