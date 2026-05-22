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
    ALLOWED_APPLIES_TO_VALUES,
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
    MemoryAudit,
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

    def query_database(
        self,
        database_id: str,
        *,
        filter_: dict | None = None,
        start_cursor: str | None = None,
        page_size: int | None = None,
    ) -> dict:
        self.query_calls.append((database_id, filter_, start_cursor, page_size))
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
        audit=MemoryAudit(
            approved_by="alice@example.com",
            approved_at="2026-05-22T10:00:00",
            expires_at="2026-12-31T23:59:59",
        ),
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


def test_upsert_normalizes_applies_to_string_into_single_value():
    """applies_to に str を渡しても 1 文字ずつ split されない（Copilot 指摘）"""
    api = _FakeAPI()
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    client.upsert_memory(
        name="X",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        content="c",
        applies_to="phase3",
        workflow_id="wf-1",
    )
    props = api.create_calls[0]["properties"]
    names = [opt["name"] for opt in props["Applies To"]["multi_select"]]
    assert names == ["phase3"]


def test_upsert_filters_applies_to_out_of_whitelist():
    """ALLOWED_APPLIES_TO_VALUES に含まれない値は黙って除外（DB schema drift 防止）"""
    api = _FakeAPI()
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    client.upsert_memory(
        name="X",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        content="c",
        applies_to=["phase4", "unknown_phase", "phase11", 42, "phase10"],
        workflow_id="wf-1",
    )
    props = api.create_calls[0]["properties"]
    names = [opt["name"] for opt in props["Applies To"]["multi_select"]]
    assert names == ["phase4", "phase10"]


def test_upsert_omits_applies_to_when_no_valid_values():
    """全件 whitelist 外なら Applies To property 自体を含めない"""
    api = _FakeAPI()
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    client.upsert_memory(
        name="X",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        content="c",
        applies_to=["unknown_phase", "phase11"],
        workflow_id="wf-1",
    )
    props = api.create_calls[0]["properties"]
    assert "Applies To" not in props


def test_upsert_rejects_mapping_applies_to():
    """dict / Mapping を applies_to に渡しても黙ってキーが採用されない（Copilot 指摘）"""
    api = _FakeAPI()
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    client.upsert_memory(
        name="X",
        memory_type=MEMORY_TYPE_PROJECT_RULE,
        content="c",
        applies_to={"phase1": True, "phase2": False},  # Mapping は reject
        workflow_id="wf-1",
    )
    props = api.create_calls[0]["properties"]
    assert "Applies To" not in props


def test_allowed_applies_to_values_covers_phase1_through_phase10():
    """phase1〜phase10 の 10 値が ALLOWED_APPLIES_TO_VALUES に含まれる"""
    assert ALLOWED_APPLIES_TO_VALUES == frozenset(
        {f"phase{i}" for i in range(1, 11)}
    )


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


# ---------------------------------------------------------------------------
# list_active_memories（Workgraph Phase 6 / Issue #48 / `hokusai prime`）
# ---------------------------------------------------------------------------


class _PaginatedFakeAPI:
    """list_active_memories のページネーション検証用 fake API。

    pages = [<page list 1>, <page list 2>, ...] で順次返す。最後のページは
    has_more=False、それ以外は next_cursor を順番に付与する。
    """

    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.query_calls: list[dict] = []

    def query_database(
        self,
        database_id: str,
        *,
        filter_: dict | None = None,
        start_cursor: str | None = None,
        page_size: int | None = None,
    ) -> dict:
        self.query_calls.append({
            "filter_": filter_,
            "start_cursor": start_cursor,
            "page_size": page_size,
        })
        idx = 0 if start_cursor is None else int(start_cursor.replace("cursor-", ""))
        results = self._pages[idx] if idx < len(self._pages) else []
        has_more = idx < len(self._pages) - 1
        return {
            "results": results,
            "has_more": has_more,
            "next_cursor": f"cursor-{idx + 1}" if has_more else None,
        }


def _make_memory_page(
    *,
    page_id: str,
    name: str,
    memory_type: str = MEMORY_TYPE_PROJECT_RULE,
    status: str = MEMORY_STATUS_ACTIVE,
    profile: str | None = None,
    applies_to: list[str] | None = None,
    summary: str | None = None,
    content: str = "body",
) -> dict:
    """Notion page 形式のテスト fixture を組み立てる。"""
    props: dict = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Type": {"select": {"name": memory_type}},
        "Status": {"select": {"name": status}},
        "Content": {"rich_text": [{"text": {"content": content}}]},
    }
    if profile is not None:
        props["Profile"] = {"rich_text": [{"text": {"content": profile}}]}
    if applies_to is not None:
        props["Applies To"] = {
            "multi_select": [{"name": p} for p in applies_to]
        }
    if summary is not None:
        props["Summary"] = {"rich_text": [{"text": {"content": summary}}]}
    return {"id": page_id, "properties": props}


def test_list_active_memories_returns_all_when_no_filter():
    pages = [[
        _make_memory_page(page_id="m1", name="A"),
        _make_memory_page(page_id="m2", name="B"),
    ]]
    api = _PaginatedFakeAPI(pages)
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    result = client.list_active_memories()
    assert [m["id"] for m in result] == ["m1", "m2"]
    # サーバ side filter は Status == active のみ
    assert api.query_calls[0]["filter_"] == {
        "property": "Status",
        "select": {"equals": "active"},
    }


def test_list_active_memories_filters_by_type():
    pages = [[
        _make_memory_page(page_id="m1", name="A", memory_type=MEMORY_TYPE_PROJECT_RULE),
        _make_memory_page(page_id="m2", name="B", memory_type=MEMORY_TYPE_AVOIDANCE),
        _make_memory_page(page_id="m3", name="C", memory_type=MEMORY_TYPE_HANDOVER_NOTE),
    ]]
    api = _PaginatedFakeAPI(pages)
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    result = client.list_active_memories(
        types={MEMORY_TYPE_PROJECT_RULE, MEMORY_TYPE_HANDOVER_NOTE}
    )
    assert [m["id"] for m in result] == ["m1", "m3"]


def test_list_active_memories_skips_when_all_types_invalid():
    api = _PaginatedFakeAPI([])
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    result = client.list_active_memories(types={"unknown_type"})
    assert result == []
    # サーバへも問い合わせない（早期 return）
    assert api.query_calls == []


def test_list_active_memories_filters_by_profile_with_global_passthrough():
    pages = [[
        _make_memory_page(page_id="m1", name="A", profile="acme"),
        _make_memory_page(page_id="m2", name="B", profile="other-client"),
        # profile 未設定の memory（global）
        _make_memory_page(page_id="m3", name="C"),
    ]]
    api = _PaginatedFakeAPI(pages)
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    result = client.list_active_memories(profile="acme")
    assert [m["id"] for m in result] == ["m1", "m3"]


def test_list_active_memories_filters_by_phase_with_global_passthrough():
    pages = [[
        _make_memory_page(page_id="m1", name="A", applies_to=["phase5"]),
        _make_memory_page(page_id="m2", name="B", applies_to=["phase3", "phase4"]),
        # Applies To 未設定の memory（global）
        _make_memory_page(page_id="m3", name="C"),
    ]]
    api = _PaginatedFakeAPI(pages)
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    result = client.list_active_memories(phase="phase5")
    assert [m["id"] for m in result] == ["m1", "m3"]


def test_list_active_memories_paginates_until_has_more_false():
    pages = [
        [_make_memory_page(page_id="m1", name="A")],
        [_make_memory_page(page_id="m2", name="B")],
        [_make_memory_page(page_id="m3", name="C")],
    ]
    api = _PaginatedFakeAPI(pages)
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    result = client.list_active_memories()
    assert [m["id"] for m in result] == ["m1", "m2", "m3"]
    # ページ 3 つ全て探索
    assert len(api.query_calls) == 3
    assert api.query_calls[0]["start_cursor"] is None
    assert api.query_calls[1]["start_cursor"] == "cursor-1"
    assert api.query_calls[2]["start_cursor"] == "cursor-2"


def test_list_active_memories_respects_max_pages_safety_limit():
    pages = [
        [_make_memory_page(page_id=f"m{i}", name=f"M{i}")] for i in range(5)
    ]
    api = _PaginatedFakeAPI(pages)
    client = ProjectMemoryDBClient(api=api, database_id="pm-db")
    result = client.list_active_memories(max_pages=2)
    assert [m["id"] for m in result] == ["m0", "m1"]
    assert len(api.query_calls) == 2


def test_list_active_memories_returns_empty_on_api_failure():
    class _RaisingAPI:
        def query_database(self, *args, **kwargs):
            raise NotionAPIError(503, "service unavailable")

    client = ProjectMemoryDBClient(api=_RaisingAPI(), database_id="pm-db")
    result = client.list_active_memories()
    assert result == []
