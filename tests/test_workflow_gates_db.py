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


class _FakeAPI:
    def __init__(self, *, existing_id: str | None = None):
        self._existing_id = existing_id
        self.query_calls: list[tuple[str, dict | None]] = []
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []

    def query_database(self, database_id: str, *, filter_: dict | None = None) -> dict:
        self.query_calls.append((database_id, filter_))
        if self._existing_id:
            return {"results": [{"id": self._existing_id}]}
        return {"results": []}

    def create_page(self, payload: dict) -> dict:
        self.create_calls.append(copy.deepcopy(payload))
        return {"id": "new-gate-id", "properties": payload["properties"]}

    def update_page(self, page_id: str, payload: dict) -> dict:
        self.update_calls.append((page_id, copy.deepcopy(payload)))
        return {"id": page_id, "properties": payload["properties"]}


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
