"""Notion Dashboard 系テストで共用する fake / stub 群

test_notion_dashboard.py / test_work_items_dispatcher.py で完全に同形の
`_RecordingAPI` を別々に持っていたため SonarCloud 重複行検知に引っかかり、
quality gate を fail させていた（PR #41 Round 9 対応で集約）。
"""

from __future__ import annotations


class FakeNotionAPIWithPruning:
    """NotionAPIClient の共通 fake（property_not_found リトライ対応）。

    Issue #54 / Workgraph 完成で SonarCloud duplication（test_work_items_db /
    test_review_issues_db / test_workflow_gates_db で同形の `_FakeAPI` class
    が重複検出）を解消するため、共通基底として本 module に集約。

    Args:
        existing_id: `query_database` が `{"results": [{"id": ...}]}` を返す
            ようにしたい時に指定。None なら常に空。
        missing_property: 指定すると `create_page` / `update_page` の初回呼び
            出しで property_not_found を 1 度だけ返す（pruning リトライ検証用）。
        missing_property_quote: Notion API のエラーメッセージで property 名を
            囲む引用符（`"` または `'`）。
    """

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

    def query_database(
        self,
        database_id: str,
        *,
        filter_: dict | None = None,
        start_cursor: str | None = None,
        page_size: int | None = None,
    ) -> dict:
        self.query_calls.append((database_id, filter_))
        if self._existing_id:
            return {"results": [{"id": self._existing_id}]}
        return {"results": []}

    def create_page(self, payload: dict) -> dict:
        import copy as _copy
        self.create_calls.append(_copy.deepcopy(payload))
        if (
            self._missing_property
            and self._first_create_call
            and self._missing_property in payload["properties"]
        ):
            self._first_create_call = False
            self._raise_missing_property()
        return {"id": "new-page-id", "properties": payload["properties"]}

    def update_page(self, page_id: str, payload: dict) -> dict:
        import copy as _copy
        self.update_calls.append((page_id, _copy.deepcopy(payload)))
        if (
            self._missing_property
            and self._first_update_call
            and self._missing_property in payload["properties"]
        ):
            self._first_update_call = False
            self._raise_missing_property()
        return {"id": page_id, "properties": payload["properties"]}

    def _raise_missing_property(self) -> None:
        from hokusai.integrations.notion_dashboard.client import NotionAPIError
        q = self._missing_property_quote
        raise NotionAPIError(
            400,
            f"{q}{self._missing_property}{q} is not a property that exists.",
            code="validation_error",
        )


class NotionRecordingAPI:
    """NotionAPIClient のテスト用 fake。query / create / update 呼び出しを記録する。

    Notion Dashboard 系の dispatcher / drain / DB クライアントテストで共用する。
    """

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


# 検証が走る（subpage_persistence_active が True になる）有効な Notion task_url。
# 末尾 32hex を持つため _extract_page_id / _is_notion_page_ref を通る（§15）。
NOTION_TASK_URL = (
    "https://www.notion.so/task-page-aabbccdd11223344aabbccdd11223344"
)


def build_subpage_verify_state(**overrides):
    """Phase 2/3 の subpage 検証テスト用の共通 state を返す（§15）。

    phase2 `_verify_subpage_content` / `_verify_notion_state` と phase3
    `_verify_design_subpage_content` のテストが同一の fixture を copy-paste して
    いたのを 1 箇所に集約し、テストコードの重複を解消する。task_url は検証が
    skip されない有効な Notion URL（`NOTION_TASK_URL`）を既定値にする。
    """
    phase_template = {
        "status": "pending",
        "started_at": None,
        "completed_at": None,
        "error_message": None,
        "retry_count": 0,
    }
    state = {
        "task_url": NOTION_TASK_URL,
        "task_name": "テストタスク",
        "repo_path": "/work/test-repo",  # /tmp は避ける（SonarCloud S5443）
        "workflow_id": "test-wf-001",
        "phases": {i: {**phase_template} for i in range(1, 11)},
        "audit_log": [],
        "schema_change_required": False,
        "research_result": "",
    }
    state.update(overrides)
    return state
