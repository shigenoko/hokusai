"""Project Memory DB クライアントの単体テスト（Issue #46 / Workgraph Phase 5）

project_memory_db.py の以下を検証する:
- build_dedupe_key: 入力が同じなら出力が同じで、16 文字 hex
- ProjectMemoryDBClient.find_by_dedupe_key: 既存検索の挙動
- upsert_memory: 新規作成 / 既存更新の分岐、Status 温存
- update_status: 状態遷移専用 API + Approved By / Approved At
- Memory Type / Status enum 検証
- property_not_found リトライ
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.integrations.notion_dashboard.client import NotionAPIError
from hokusai.integrations.notion_dashboard.project_memory_db import (
    ACTIVE_MEMORY_STATUSES,
    ALL_MEMORY_STATUSES,
    ALL_MEMORY_TYPES,
    DEFAULT_MEMORY_STATUS,
    MEMORY_STATUS_ACTIVE,
    MEMORY_STATUS_DEPRECATED,
    MEMORY_STATUS_DRAFT,
    MEMORY_STATUS_REJECTED,
    MEMORY_TYPE_ARCHITECTURE_DECISION,
    MEMORY_TYPE_AVOIDANCE,
    MEMORY_TYPE_DOMAIN_KNOWLEDGE,
    MEMORY_TYPE_HANDOVER_NOTE,
    MEMORY_TYPE_OPERATIONS_NOTE,
    MEMORY_TYPE_POLICY_NOTE,
    MEMORY_TYPE_PROJECT_RULE,
    ProjectMemoryDBClient,
    build_dedupe_key,
    is_valid_memory_status,
    is_valid_memory_type,
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
        return {"id": "new-memory-id", "properties": payload["properties"]}

    def update_page(self, page_id: str, payload: dict) -> dict:
        self.update_calls.append((page_id, copy.deepcopy(payload)))
        return {"id": page_id, "properties": payload["properties"]}


# ---------------------------------------------------------------------------
# build_dedupe_key
# ---------------------------------------------------------------------------


def test_build_dedupe_key_deterministic_16_hex():
    k1 = build_dedupe_key(
        workflow_id="wf-1",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        name="No API token in DB",
    )
    k2 = build_dedupe_key(
        workflow_id="wf-1",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        name="No API token in DB",
    )
    assert k1 == k2
    assert len(k1) == 16
    assert all(c in "0123456789abcdef" for c in k1)


def test_build_dedupe_key_differs_by_memory_type():
    a = build_dedupe_key(workflow_id="wf-1", memory_type=MEMORY_TYPE_PROJECT_RULE, name="same")
    b = build_dedupe_key(workflow_id="wf-1", memory_type=MEMORY_TYPE_AVOIDANCE, name="same")
    assert a != b


def test_build_dedupe_key_differs_by_name():
    a = build_dedupe_key(workflow_id="wf-1", memory_type=MEMORY_TYPE_PROJECT_RULE, name="rule A")
    b = build_dedupe_key(workflow_id="wf-1", memory_type=MEMORY_TYPE_PROJECT_RULE, name="rule B")
    assert a != b


def test_build_dedupe_key_normalizes_whitespace():
    k1 = build_dedupe_key(workflow_id="wf-1", memory_type=MEMORY_TYPE_PROJECT_RULE, name="  rule  ")
    k2 = build_dedupe_key(workflow_id="wf-1", memory_type=MEMORY_TYPE_PROJECT_RULE, name="rule")
    assert k1 == k2


# ---------------------------------------------------------------------------
# public helper / 定数
# ---------------------------------------------------------------------------


def test_is_valid_memory_type():
    """Memory Type の全 7 値で True、非 str / 不正値で False"""
    for value in (
        MEMORY_TYPE_PROJECT_RULE,
        MEMORY_TYPE_ARCHITECTURE_DECISION,
        MEMORY_TYPE_AVOIDANCE,
        MEMORY_TYPE_DOMAIN_KNOWLEDGE,
        MEMORY_TYPE_OPERATIONS_NOTE,
        MEMORY_TYPE_POLICY_NOTE,
        MEMORY_TYPE_HANDOVER_NOTE,
    ):
        assert is_valid_memory_type(value)
    assert not is_valid_memory_type("not_a_real_type")
    assert not is_valid_memory_type(None)
    assert not is_valid_memory_type(42)


def test_is_valid_memory_status():
    """Memory Status の全 4 値で True、非 str / 不正値で False"""
    for value in (
        MEMORY_STATUS_DRAFT,
        MEMORY_STATUS_ACTIVE,
        MEMORY_STATUS_DEPRECATED,
        MEMORY_STATUS_REJECTED,
    ):
        assert is_valid_memory_status(value)
    assert not is_valid_memory_status("approved")
    assert not is_valid_memory_status(None)


def test_active_memory_statuses_only_includes_active():
    """Agent prompt 注入対象は active のみ（要件 §8.5）"""
    assert MEMORY_STATUS_ACTIVE in ACTIVE_MEMORY_STATUSES
    assert MEMORY_STATUS_DRAFT not in ACTIVE_MEMORY_STATUSES
    assert MEMORY_STATUS_DEPRECATED not in ACTIVE_MEMORY_STATUSES
    assert MEMORY_STATUS_REJECTED not in ACTIVE_MEMORY_STATUSES


def test_default_memory_status_is_draft():
    """Agent 自動生成 memory は draft から開始（要件 §8.5）"""
    assert DEFAULT_MEMORY_STATUS == MEMORY_STATUS_DRAFT


def test_all_memory_constants_sets_have_expected_sizes():
    assert len(ALL_MEMORY_TYPES) == 7
    assert len(ALL_MEMORY_STATUSES) == 4


# ---------------------------------------------------------------------------
# ProjectMemoryDBClient コンストラクタ
# ---------------------------------------------------------------------------


def test_client_rejects_empty_database_id():
    with pytest.raises(ValueError, match="database_id"):
        ProjectMemoryDBClient(api=_FakeAPI(), database_id="")


# ---------------------------------------------------------------------------
# upsert_memory: 新規作成
# ---------------------------------------------------------------------------


def test_upsert_creates_new_with_default_draft_status():
    api = _FakeAPI()
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    client.upsert_memory(
        name="No API tokens in DB",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        content="API tokens must be stored in env vars or secret manager",
        workflow_id="wf-1",
    )
    assert len(api.create_calls) == 1
    props = api.create_calls[0]["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "No API tokens in DB"
    assert props["Type"]["select"]["name"] == MEMORY_TYPE_PROJECT_RULE
    # 新規作成時のみ Status / Created At が書かれる
    assert props["Status"]["select"]["name"] == MEMORY_STATUS_DRAFT
    assert "Created At" in props
    assert "Dedupe Key" in props
    assert "Content" in props


def test_upsert_includes_optional_fields_when_provided():
    api = _FakeAPI()
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    client.upsert_memory(
        name="X",
        memory_type=MEMORY_TYPE_ARCHITECTURE_DECISION,
        content="content",
        summary="short summary",
        profile="backend-team",
        applies_to=["phase4", "phase5"],
        workflow_id="wf-1",
        workflow_page_id="wf-page",
        pull_request_page_id="pr-page",
        approved_by="alice@example.com",
        approved_at="2026-05-22T10:00:00",
        expires_at="2026-12-31T23:59:59",
    )
    props = api.create_calls[0]["properties"]
    assert props["Summary"]["rich_text"][0]["text"]["content"] == "short summary"
    assert props["Profile"]["rich_text"][0]["text"]["content"] == "backend-team"
    # Applies To は multi_select
    applies_names = [opt["name"] for opt in props["Applies To"]["multi_select"]]
    assert applies_names == ["phase4", "phase5"]
    assert props["Workflow"]["relation"] == [{"id": "wf-page"}]
    assert props["Pull Request"]["relation"] == [{"id": "pr-page"}]
    assert props["Approved By"]["rich_text"][0]["text"]["content"] == "alice@example.com"
    assert "Approved At" in props
    assert "Expires At" in props


def test_upsert_rejects_invalid_memory_type():
    client = ProjectMemoryDBClient(api=_FakeAPI(), database_id="pm-db")
    with pytest.raises(ValueError, match="Memory Type"):
        client.upsert_memory(
            name="X", memory_type="not_a_real_type", content="c"
        )


def test_upsert_rejects_invalid_status():
    client = ProjectMemoryDBClient(api=_FakeAPI(), database_id="pm-db")
    with pytest.raises(ValueError, match="Memory Status"):
        client.upsert_memory(
            name="X",
            memory_type=MEMORY_TYPE_PROJECT_RULE,
            content="c",
            status="not_a_real_status",
        )


def test_upsert_rejects_empty_name():
    client = ProjectMemoryDBClient(api=_FakeAPI(), database_id="pm-db")
    with pytest.raises(ValueError, match="name"):
        client.upsert_memory(name="", memory_type=MEMORY_TYPE_PROJECT_RULE, content="c")


def test_upsert_rejects_empty_content():
    """memory は本文必須（要件 §8.3 で Content 列がある）"""
    client = ProjectMemoryDBClient(api=_FakeAPI(), database_id="pm-db")
    with pytest.raises(ValueError, match="content"):
        client.upsert_memory(name="X", memory_type=MEMORY_TYPE_PROJECT_RULE, content="")


# ---------------------------------------------------------------------------
# upsert_memory: 既存更新（Status 温存）
# ---------------------------------------------------------------------------


def test_upsert_existing_updates_without_touching_status():
    """update では Status / Created At を payload に含めない（要件 §8.5:
    人間が active に承認した状態を後発 upsert で draft に巻き戻さない）"""
    api = _FakeAPI(existing_id="existing-memory")
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    client.upsert_memory(
        name="X",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        content="updated content",
        workflow_id="wf-1",
    )
    assert len(api.create_calls) == 0
    assert len(api.update_calls) == 1
    _page_id, payload = api.update_calls[0]
    props = payload["properties"]
    assert "Status" not in props
    assert "Created At" not in props
    assert "Last Updated" in props
    assert props["Content"]["rich_text"][0]["text"]["content"] == "updated content"


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


def test_update_status_writes_status_and_optional_audit_fields():
    """update_status で Status + Approved By / Approved At を audit trail として書き込む"""
    api = _FakeAPI()
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    client.update_status(
        "memory-x",
        MEMORY_STATUS_ACTIVE,
        approved_by="alice@example.com",
        approved_at="2026-05-22T10:00:00",
    )
    assert len(api.update_calls) == 1
    _page_id, payload = api.update_calls[0]
    props = payload["properties"]
    assert props["Status"]["select"]["name"] == MEMORY_STATUS_ACTIVE
    assert (
        props["Approved By"]["rich_text"][0]["text"]["content"]
        == "alice@example.com"
    )
    assert "Approved At" in props


def test_update_status_rejects_invalid_status():
    client = ProjectMemoryDBClient(api=_FakeAPI(), database_id="pm-db")
    with pytest.raises(ValueError, match="Memory Status"):
        client.update_status("memory-x", "approved")  # 不正値（active が正）


def test_update_status_accepts_all_four_statuses():
    """schema (setup.py) と enum (project_memory_db.py) の整合性を保証"""
    api = _FakeAPI()
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    for status in (
        MEMORY_STATUS_DRAFT,
        MEMORY_STATUS_ACTIVE,
        MEMORY_STATUS_DEPRECATED,
        MEMORY_STATUS_REJECTED,
    ):
        client.update_status("memory-x", status)


# ---------------------------------------------------------------------------
# find_by_dedupe_key
# ---------------------------------------------------------------------------


def test_find_by_dedupe_key_returns_existing_id():
    api = _FakeAPI(existing_id="found-id")
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    assert client.find_by_dedupe_key("abc1234567890def") == "found-id"


def test_find_by_dedupe_key_returns_none_when_empty():
    client = ProjectMemoryDBClient(api=_FakeAPI(), database_id="pm-db")
    assert client.find_by_dedupe_key("") is None


# ---------------------------------------------------------------------------
# property_not_found リトライ（_property_pruning helper 経由）
# ---------------------------------------------------------------------------


class _FakeAPIWithMissingProperty:
    """create_page で 1 回だけ property_not_found を返す fake"""

    def __init__(self, missing_property: str):
        self._missing_property = missing_property
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []
        self.query_calls: list[tuple[str, dict | None]] = []
        self._first_create_call = True

    def query_database(self, database_id: str, *, filter_: dict | None = None) -> dict:
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
        return {"id": "new-memory-id", "properties": payload["properties"]}

    def update_page(self, page_id: str, payload: dict) -> dict:
        self.update_calls.append((page_id, copy.deepcopy(payload)))
        return {"id": page_id, "properties": payload["properties"]}


def test_property_not_found_retry_drops_missing_property():
    """schema 未追加環境で Notion 側にプロパティが存在しなくても、該当
    プロパティを除外して再試行することで同期が継続する。"""
    api = _FakeAPIWithMissingProperty(missing_property="Summary")
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    result = client.upsert_memory(
        name="X",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        content="c",
        summary="will be dropped",
        workflow_id="wf-1",
    )
    assert len(api.create_calls) == 2
    assert "Summary" in api.create_calls[0]["properties"]
    assert "Summary" not in api.create_calls[1]["properties"]
    # 他のプロパティは温存される
    assert "Name" in api.create_calls[1]["properties"]
    assert "Type" in api.create_calls[1]["properties"]
    assert result["id"] == "new-memory-id"
