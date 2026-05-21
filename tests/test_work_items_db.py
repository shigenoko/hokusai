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
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    STATUS_READY,
    WorkItemsDBClient,
    build_dedupe_key,
)


class _FakeAPI:
    """NotionAPIClient のテスト用 fake。query / create / update を記録する。"""

    def __init__(
        self,
        *,
        existing_id: str | None = None,
        missing_property: str | None = None,
        missing_property_quote: str = '"',
    ):
        self._existing_id = existing_id
        self._missing_property = missing_property
        self._missing_property_quote = missing_property_quote
        self.query_calls: list[tuple[str, dict | None]] = []
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []
        self._first_create_call = True
        self._first_update_call = True

    def query_database(self, database_id: str, *, filter_: dict | None = None) -> dict:
        self.query_calls.append((database_id, filter_))
        if self._existing_id:
            return {"results": [{"id": self._existing_id}]}
        return {"results": []}

    def create_page(self, payload: dict) -> dict:
        self.create_calls.append(copy.deepcopy(payload))
        if (
            self._missing_property
            and self._first_create_call
            and self._missing_property in payload["properties"]
        ):
            self._first_create_call = False
            q = self._missing_property_quote
            raise NotionAPIError(
                400,
                f"{q}{self._missing_property}{q} is not a property that exists.",
                code="validation_error",
            )
        return {"id": "new-page-id", "properties": payload["properties"]}

    def update_page(self, page_id: str, payload: dict) -> dict:
        self.update_calls.append((page_id, copy.deepcopy(payload)))
        if (
            self._missing_property
            and self._first_update_call
            and self._missing_property in payload["properties"]
        ):
            self._first_update_call = False
            q = self._missing_property_quote
            raise NotionAPIError(
                400,
                f"{q}{self._missing_property}{q} is not a property that exists.",
                code="validation_error",
            )
        return {"id": page_id, "properties": payload["properties"]}


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
        "blocked",
        STATUS_DONE,
        "skipped",
        "canceled",
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
