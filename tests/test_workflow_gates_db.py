"""Workflow Gates DB クライアントの単体テスト（Issue #44 / Workgraph Phase 4）

workflow_gates_db.py の以下を検証する:
- build_dedupe_key: 入力が同じなら出力が同じで、16 文字 hex
- WorkflowGatesDBClient.find_by_dedupe_key: 既存検索の挙動
- WorkflowGatesDBClient.upsert_gate: 新規作成 / 既存更新の分岐、Status 温存
- WorkflowGatesDBClient.update_status: 状態遷移専用 API + Approver / Reason
- Gate Type / Status enum 検証
- property_not_found リトライ
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.integrations.notion_dashboard.client import NotionAPIError
from hokusai.integrations.notion_dashboard.workflow_gates_db import (
    BLOCKING_GATE_STATUSES,
    DEFAULT_GATE_STATUS,
    GATE_STATUS_BLOCKED,
    GATE_STATUS_CANCELED,
    GATE_STATUS_EXPIRED,
    GATE_STATUS_NOT_REQUIRED,
    GATE_STATUS_OPEN,
    GATE_STATUS_PENDING,
    GATE_TYPE_CI_PASSED,
    GATE_TYPE_DESIGN_APPROVED,
    GATE_TYPE_HUMAN_APPROVAL,
    WorkflowGatesDBClient,
    build_dedupe_key,
)
from tests._notion_test_helpers import FakeNotionAPIWithPruning as _FakeAPI

# ---------------------------------------------------------------------------
# build_dedupe_key
# ---------------------------------------------------------------------------


def test_build_dedupe_key_deterministic_16_hex():
    k1 = build_dedupe_key(
        workflow_id="wf-1",
        gate_type=GATE_TYPE_HUMAN_APPROVAL,
        required_by_phase=5,
    )
    k2 = build_dedupe_key(
        workflow_id="wf-1",
        gate_type=GATE_TYPE_HUMAN_APPROVAL,
        required_by_phase=5,
    )
    assert k1 == k2
    assert len(k1) == 16
    assert all(c in "0123456789abcdef" for c in k1)


def test_build_dedupe_key_differs_by_gate_type():
    a = build_dedupe_key(workflow_id="wf-1", gate_type=GATE_TYPE_HUMAN_APPROVAL)
    b = build_dedupe_key(workflow_id="wf-1", gate_type=GATE_TYPE_CI_PASSED)
    assert a != b


def test_build_dedupe_key_differs_by_phase():
    a = build_dedupe_key(workflow_id="wf-1", gate_type=GATE_TYPE_CI_PASSED, required_by_phase=5)
    b = build_dedupe_key(workflow_id="wf-1", gate_type=GATE_TYPE_CI_PASSED, required_by_phase=6)
    assert a != b


def test_build_dedupe_key_differs_by_work_item_dedupe_key():
    """同 workflow / gate_type / phase でも work_item レベルなら別 gate"""
    a = build_dedupe_key(workflow_id="wf-1", gate_type=GATE_TYPE_HUMAN_APPROVAL, required_by_phase=5)
    b = build_dedupe_key(workflow_id="wf-1", gate_type=GATE_TYPE_HUMAN_APPROVAL, required_by_phase=5, work_item_dedupe_key="wi-abc")
    assert a != b


# ---------------------------------------------------------------------------
# WorkflowGatesDBClient コンストラクタ
# ---------------------------------------------------------------------------


def test_client_rejects_empty_database_id():
    with pytest.raises(ValueError, match="database_id"):
        WorkflowGatesDBClient(api=_FakeAPI(), database_id="")


# ---------------------------------------------------------------------------
# upsert_gate: 新規作成
# ---------------------------------------------------------------------------


def test_upsert_creates_new_with_default_pending_status():
    api = _FakeAPI()
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    client.upsert_gate(
        name="Phase 5 human approval",
        gate_type=GATE_TYPE_HUMAN_APPROVAL,
        workflow_id="wf-1",
        required_by_phase=5,
    )
    assert len(api.create_calls) == 1
    props = api.create_calls[0]["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "Phase 5 human approval"
    assert props["Gate Type"]["select"]["name"] == GATE_TYPE_HUMAN_APPROVAL
    # 新規作成時のみ Status / Created At が書かれる
    assert props["Status"]["select"]["name"] == DEFAULT_GATE_STATUS
    assert "Created At" in props
    assert "Dedupe Key" in props
    assert props["Required By Phase"]["number"] == 5


def test_upsert_includes_relations_when_page_ids_passed():
    api = _FakeAPI()
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    client.upsert_gate(
        name="X",
        gate_type=GATE_TYPE_DESIGN_APPROVED,
        workflow_id="wf-1",
        workflow_page_id="wf-page",
        pull_request_page_id="pr-page",
        work_item_page_id="wi-page",
        review_issue_page_id="ri-page",
    )
    props = api.create_calls[0]["properties"]
    assert props["Workflow"]["relation"] == [{"id": "wf-page"}]
    assert props["Pull Request"]["relation"] == [{"id": "pr-page"}]
    assert props["Work Item"]["relation"] == [{"id": "wi-page"}]
    assert props["Review Issue"]["relation"] == [{"id": "ri-page"}]


def test_upsert_rejects_invalid_gate_type():
    client = WorkflowGatesDBClient(api=_FakeAPI(), database_id="wg-db")
    with pytest.raises(ValueError, match="Gate Type"):
        client.upsert_gate(name="X", gate_type="not_a_real_type")


def test_upsert_rejects_invalid_status():
    client = WorkflowGatesDBClient(api=_FakeAPI(), database_id="wg-db")
    with pytest.raises(ValueError, match="Status"):
        client.upsert_gate(
            name="X",
            gate_type=GATE_TYPE_HUMAN_APPROVAL,
            status="not_a_real_status",
        )


def test_upsert_rejects_empty_name():
    client = WorkflowGatesDBClient(api=_FakeAPI(), database_id="wg-db")
    with pytest.raises(ValueError, match="name"):
        client.upsert_gate(name="", gate_type=GATE_TYPE_HUMAN_APPROVAL)


# ---------------------------------------------------------------------------
# upsert_gate: 既存更新（Status 温存）
# ---------------------------------------------------------------------------


def test_upsert_existing_updates_without_touching_status():
    api = _FakeAPI(existing_id="existing-gate")
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    client.upsert_gate(
        name="X",
        gate_type=GATE_TYPE_HUMAN_APPROVAL,
        workflow_id="wf-1",
        required_by_phase=5,
        decision_reason="updated reason",
    )
    assert len(api.create_calls) == 0
    assert len(api.update_calls) == 1
    _page_id, payload = api.update_calls[0]
    props = payload["properties"]
    # Status / Created At は update 時 payload に含めない
    assert "Status" not in props
    assert "Created At" not in props
    assert "Last Updated" in props
    assert (
        props["Decision Reason"]["rich_text"][0]["text"]["content"]
        == "updated reason"
    )


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


def test_update_status_writes_status_and_optional_audit_fields():
    api = _FakeAPI()
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    client.update_status(
        "gate-x",
        GATE_STATUS_OPEN,
        approver="alice@example.com",
        decision_reason="approved on 2026-05-22",
    )
    assert len(api.update_calls) == 1
    _page_id, payload = api.update_calls[0]
    props = payload["properties"]
    assert props["Status"]["select"]["name"] == GATE_STATUS_OPEN
    assert (
        props["Approver"]["rich_text"][0]["text"]["content"]
        == "alice@example.com"
    )
    assert (
        props["Decision Reason"]["rich_text"][0]["text"]["content"]
        == "approved on 2026-05-22"
    )


def test_update_status_rejects_invalid_status():
    client = WorkflowGatesDBClient(api=_FakeAPI(), database_id="wg-db")
    with pytest.raises(ValueError, match="Status"):
        client.update_status("gate-x", "approved")  # 不正値（open が正）


def test_update_status_accepts_all_six_statuses():
    """schema (setup.py) と enum (workflow_gates_db.py) の整合性を保証"""
    api = _FakeAPI()
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    for status in (
        GATE_STATUS_NOT_REQUIRED,
        GATE_STATUS_PENDING,
        GATE_STATUS_OPEN,
        GATE_STATUS_BLOCKED,
        GATE_STATUS_EXPIRED,
        GATE_STATUS_CANCELED,
    ):
        client.update_status("gate-x", status)


# ---------------------------------------------------------------------------
# BLOCKING_GATE_STATUSES 定数
# ---------------------------------------------------------------------------


def test_blocking_gate_statuses_includes_pending_and_blocked():
    """ready 判定エンジンが workflow を阻害する gate status は pending / blocked"""
    assert GATE_STATUS_PENDING in BLOCKING_GATE_STATUSES
    assert GATE_STATUS_BLOCKED in BLOCKING_GATE_STATUSES
    # 他の状態は阻害しない
    assert GATE_STATUS_OPEN not in BLOCKING_GATE_STATUSES
    assert GATE_STATUS_NOT_REQUIRED not in BLOCKING_GATE_STATUSES
    assert GATE_STATUS_EXPIRED not in BLOCKING_GATE_STATUSES
    assert GATE_STATUS_CANCELED not in BLOCKING_GATE_STATUSES


# ---------------------------------------------------------------------------
# find_by_dedupe_key
# ---------------------------------------------------------------------------


def test_find_by_dedupe_key_returns_existing_id():
    api = _FakeAPI(existing_id="found-id")
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    assert client.find_by_dedupe_key("abc1234567890def") == "found-id"


def test_find_by_dedupe_key_returns_none_when_empty():
    client = WorkflowGatesDBClient(api=_FakeAPI(), database_id="wg-db")
    assert client.find_by_dedupe_key("") is None


def test_find_by_dedupe_key_returns_none_when_no_results():
    api = _FakeAPI()  # 空 results
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    assert client.find_by_dedupe_key("nonexistent") is None


# ---------------------------------------------------------------------------
# property_not_found リトライ（_property_pruning helper 経由）
# ---------------------------------------------------------------------------


class _FakeAPIWithMissingProperty:
    """create_page で 1 回だけ property_not_found を返す fake。
    既存 review_issues_db / work_items_db のテストと同じパターン
    （PR #45 Copilot 4 回目指摘で workflow_gates_db にも同等テストを追加）。"""

    def __init__(self, missing_property: str):
        self._missing_property = missing_property
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []
        self.query_calls: list[tuple[str, dict | None]] = []
        self._first_create_call = True

    def query_database(
        self,
        database_id: str,
        *,
        filter_: dict | None = None,
        start_cursor: str | None = None,
        page_size: int | None = None,
    ) -> dict:
        self.query_calls.append((database_id, filter_))
        return {"results": []}

    def create_page(self, payload: dict) -> dict:
        self.create_calls.append(copy.deepcopy(payload))
        if (
            self._first_create_call
            and self._missing_property in payload["properties"]
        ):
            self._first_create_call = False
            raise NotionAPIError(
                400,
                f'"{self._missing_property}" is not a property that exists.',
                code="validation_error",
            )
        return {"id": "new-gate-id", "properties": payload["properties"]}

    def update_page(self, page_id: str, payload: dict) -> dict:
        self.update_calls.append((page_id, copy.deepcopy(payload)))
        return {"id": page_id, "properties": payload["properties"]}


def test_property_not_found_retry_drops_missing_property():
    """schema 未追加環境で Notion 側にプロパティが存在しなくても、該当
    プロパティを除外して再試行することで同期が継続する。"""
    api = _FakeAPIWithMissingProperty(missing_property="Decision Reason")
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    result = client.upsert_gate(
        name="Phase 5 approval",
        gate_type=GATE_TYPE_HUMAN_APPROVAL,
        workflow_id="wf-1",
        required_by_phase=5,
        decision_reason="will be dropped",
    )
    # 2 回 create_page される（1 回目で property_not_found、2 回目は除外後）
    assert len(api.create_calls) == 2
    # 1 回目は Decision Reason を含む
    assert "Decision Reason" in api.create_calls[0]["properties"]
    # 2 回目は Decision Reason が除外されている
    assert "Decision Reason" not in api.create_calls[1]["properties"]
    # 他のプロパティは温存される
    assert "Name" in api.create_calls[1]["properties"]
    assert "Gate Type" in api.create_calls[1]["properties"]
    assert result["id"] == "new-gate-id"


# ---------------------------------------------------------------------------
# list_pending_gates_for_workflow（Issue #54 / Workgraph 完成）
# ---------------------------------------------------------------------------


def test_list_pending_gates_for_workflow_returns_pages():
    class _PaginatedAPI:
        def __init__(self, pages):
            self._pages = pages
            self.query_calls = []

        def query_database(self, db, *, filter_=None, start_cursor=None, page_size=None):
            self.query_calls.append({"filter": filter_, "start_cursor": start_cursor})
            idx = 0 if start_cursor is None else int(start_cursor.replace("c", ""))
            results = self._pages[idx] if idx < len(self._pages) else []
            has_more = idx < len(self._pages) - 1
            return {"results": results, "has_more": has_more, "next_cursor": f"c{idx + 1}" if has_more else None}

    api = _PaginatedAPI([[
        {"id": "g-1", "properties": {"Status": {"select": {"name": "pending"}}}},
    ]])
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    result = client.list_pending_gates_for_workflow("wf-page")
    assert [r["id"] for r in result] == ["g-1"]
    call_filter = api.query_calls[0]["filter"]
    assert "and" in call_filter
    or_clause = next(c for c in call_filter["and"] if "or" in c)
    statuses = sorted([c["select"]["equals"] for c in or_clause["or"]])
    assert statuses == ["blocked", "open", "pending"]
    wf_clause = next(c for c in call_filter["and"] if c.get("property") == "Workflow")
    assert wf_clause["relation"]["contains"] == "wf-page"


def test_list_pending_gates_returns_empty_for_blank_page_id():
    api = _FakeAPI()
    client = WorkflowGatesDBClient(api=api, database_id="wg-db")
    assert client.list_pending_gates_for_workflow("") == []
    assert client.list_pending_gates_for_workflow(None) == []


def test_list_pending_gates_returns_partial_on_api_failure():
    class _RaisingAPI:
        def query_database(self, *args, **kwargs):
            raise NotionAPIError(503, "service unavailable")

    client = WorkflowGatesDBClient(api=_RaisingAPI(), database_id="wg-db")
    assert client.list_pending_gates_for_workflow("wf-page") == []
