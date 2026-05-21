"""Notion Dashboard 系テストで共用する fake / stub 群

test_notion_dashboard.py / test_work_items_dispatcher.py で完全に同形の
`_RecordingAPI` を別々に持っていたため SonarCloud 重複行検知に引っかかり、
quality gate を fail させていた（PR #41 Round 9 対応で集約）。
"""

from __future__ import annotations


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
