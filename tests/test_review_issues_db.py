"""Review Issues DB クライアントの単体テスト（#36 / v0.5.0）

review_issues_db.py の以下を検証する:
- build_dedupe_key: 入力が同じなら出力が同じで、16 文字 hex
- ReviewIssuesDBClient.find_by_dedupe_key: 既存検索の挙動
- ReviewIssuesDBClient.upsert_record: 新規作成 / 既存更新の分岐
- property_not_found 検出時のプロパティ除外リトライ
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.integrations.notion_dashboard.client import NotionAPIError
from hokusai.integrations.notion_dashboard.review_issues_db import (
    ReviewIssuesDBClient,
    build_dedupe_key,
)


from tests._notion_test_helpers import FakeNotionAPIWithPruning as _FakeAPI


# ---------------------------------------------------------------------------
# build_dedupe_key
# ---------------------------------------------------------------------------


def test_build_dedupe_key_is_deterministic_and_16_hex():
    k1 = build_dedupe_key(
        source="final_review",
        rule="P01",
        file="auth/login.py:42",
        message="Missing validation",
    )
    k2 = build_dedupe_key(
        source="final_review",
        rule="P01",
        file="auth/login.py:42",
        message="Missing validation",
    )
    assert k1 == k2
    assert len(k1) == 16
    assert all(c in "0123456789abcdef" for c in k1)


def test_build_dedupe_key_differs_for_different_source():
    k_a = build_dedupe_key(
        source="final_review", rule="P01", file="x.py", message="same"
    )
    k_b = build_dedupe_key(
        source="verification_failure", rule="P01", file="x.py", message="same"
    )
    assert k_a != k_b


def test_build_dedupe_key_differs_for_different_repository():
    """同じ source / rule / message でも repository が違えば別キー（PR #37 Copilot 指摘）"""
    k_backend = build_dedupe_key(
        source="final_review",
        rule="P01",
        file=None,
        message="same",
        repository="Backend",
    )
    k_frontend = build_dedupe_key(
        source="final_review",
        rule="P01",
        file=None,
        message="same",
        repository="Frontend",
    )
    assert k_backend != k_frontend


def test_build_dedupe_key_differs_for_different_workflow_id():
    """同じ source / repo / rule / file / message でも workflow_id が違えば別キー

    PR #37 Copilot 8 回目指摘: workflow_id を含めないと、別 workflow が同じ
    rule/file/message を発火した時に同じ Notion ページに集約され、Workflow
    relation が最新の workflow page id で上書きされて先発の関連が失われる。
    """
    k_wf1 = build_dedupe_key(
        source="final_review",
        rule="P01",
        file="x.py",
        message="same",
        repository="Backend",
        workflow_id="wf-001",
    )
    k_wf2 = build_dedupe_key(
        source="final_review",
        rule="P01",
        file="x.py",
        message="same",
        repository="Backend",
        workflow_id="wf-002",
    )
    assert k_wf1 != k_wf2


def test_build_dedupe_key_handles_none_inputs():
    k = build_dedupe_key(source="ci_failure", rule=None, file=None, message="boom")
    assert len(k) == 16


def test_build_dedupe_key_normalizes_whitespace():
    """message の前後空白だけ違う場合は同じキーになる"""
    k1 = build_dedupe_key(source="x", rule=None, file=None, message="boom")
    k2 = build_dedupe_key(source="x", rule=None, file=None, message="  boom  ")
    assert k1 == k2


# ---------------------------------------------------------------------------
# ReviewIssuesDBClient
# ---------------------------------------------------------------------------


def test_init_rejects_empty_database_id():
    api = _FakeAPI()
    with pytest.raises(ValueError):
        ReviewIssuesDBClient(api=api, database_id="")


def test_find_by_dedupe_key_returns_none_when_no_results():
    api = _FakeAPI(existing_id=None)
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    assert client.find_by_dedupe_key("abc") is None
    assert len(api.query_calls) == 1
    # フィルタ条件: Dedupe Key プロパティで equals 検索
    assert api.query_calls[0][1] == {
        "property": "Dedupe Key",
        "rich_text": {"equals": "abc"},
    }


def test_find_by_dedupe_key_returns_existing_id():
    api = _FakeAPI(existing_id="existing-page")
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    assert client.find_by_dedupe_key("abc") == "existing-page"


def test_find_by_dedupe_key_empty_returns_none_without_query():
    api = _FakeAPI()
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    assert client.find_by_dedupe_key("") is None
    assert api.query_calls == []  # 空キーは API を呼ばない


def test_upsert_record_creates_new_when_no_existing():
    api = _FakeAPI(existing_id=None)
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    client.upsert_record(
        source="final_review",
        message="Missing validation",
        severity="high",
        rule="P01",
        file="auth/login.py:42",
        repository="Backend",
        workflow_page_id="wf-page-id",
        operator="alice",
    )
    assert len(api.create_calls) == 1
    assert api.update_calls == []
    props = api.create_calls[0]["properties"]
    assert props["Source"] == {"select": {"name": "final_review"}}
    assert props["Severity"] == {"select": {"name": "high"}}
    assert props["Status"] == {"select": {"name": "open"}}
    assert props["Repository"] == {"select": {"name": "Backend"}}
    assert props["Workflow"] == {"relation": [{"id": "wf-page-id"}]}
    assert props["Rule ID"]["rich_text"][0]["text"]["content"] == "P01"
    assert (
        props["File Path"]["rich_text"][0]["text"]["content"]
        == "auth/login.py:42"
    )
    assert props["Operator"]["rich_text"][0]["text"]["content"] == "alice"
    # 新規時のみ Created At が含まれる
    assert "Created At" in props
    # dedupe_key が自動生成されて含まれる
    assert "Dedupe Key" in props
    dedupe_value = props["Dedupe Key"]["rich_text"][0]["text"]["content"]
    assert len(dedupe_value) == 16
    # Title は [source] file — summary 形式
    title_value = props["Title"]["title"][0]["text"]["content"]
    assert "final_review" in title_value
    assert "auth/login.py" in title_value


def test_upsert_record_updates_existing_when_dedupe_key_matches():
    api = _FakeAPI(existing_id="existing-page")
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    client.upsert_record(
        source="final_review",
        message="Missing validation",
        rule="P01",
        file="auth/login.py:42",
    )
    assert api.create_calls == []
    assert len(api.update_calls) == 1
    page_id, payload = api.update_calls[0]
    assert page_id == "existing-page"
    # 更新時は Created At を含めない（既存値温存）
    assert "Created At" not in payload["properties"]
    # Last Updated は常に含む
    assert "Last Updated" in payload["properties"]


def test_upsert_record_does_not_overwrite_status_on_update():
    """更新時は Status を payload に含めない（人手の waived / resolved を温存）

    PR #37 Copilot 2 回目指摘: 再 dispatch で Status が default "open" に
    巻き戻ると、人手の運用判断が消える。
    """
    api = _FakeAPI(existing_id="existing-page")
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    client.upsert_record(
        source="final_review",
        message="Missing validation",
        status="open",  # 明示的に渡しても update 時は反映しない
        rule="P01",
    )
    payload = api.update_calls[0][1]
    assert "Status" not in payload["properties"]


def test_upsert_record_sets_status_on_create():
    """新規作成時は Status を初期値として書き込む"""
    api = _FakeAPI(existing_id=None)
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    client.upsert_record(
        source="final_review",
        message="Missing validation",
        status="open",
        rule="P01",
    )
    props = api.create_calls[0]["properties"]
    assert props["Status"] == {"select": {"name": "open"}}


def test_upsert_record_uses_single_timestamp_for_created_and_last_updated():
    """新規作成時、Created At と Last Updated は同一の datetime.now() を使う

    PR #37 Copilot 2 回目指摘: 別々に now() を呼ぶと Created At が Last Updated
    より遅れ得る。
    """
    api = _FakeAPI(existing_id=None)
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    client.upsert_record(
        source="final_review",
        message="Missing validation",
        rule="P01",
    )
    props = api.create_calls[0]["properties"]
    created_at = props["Created At"]["date"]["start"]
    last_updated = props["Last Updated"]["date"]["start"]
    assert created_at == last_updated


def test_upsert_record_uses_explicit_dedupe_key():
    api = _FakeAPI(existing_id=None)
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    client.upsert_record(
        source="ci_failure",
        message="anything",
        dedupe_key="custom-dedupe-key",
    )
    props = api.create_calls[0]["properties"]
    assert (
        props["Dedupe Key"]["rich_text"][0]["text"]["content"]
        == "custom-dedupe-key"
    )


def test_upsert_record_normalizes_empty_dedupe_key_to_generated():
    """空文字 dedupe_key は「未指定」と同義として generated value にフォールバック

    PR #37 Copilot 9 回目指摘: `dedupe_key is None` だけを未指定と判定すると
    empty string `""` がそのまま `find_by_dedupe_key("")` に渡り、early-return
    で lookup スキップ → 空 Dedupe Key の Notion レコードが毎回新規作成される。
    """
    api = _FakeAPI(existing_id=None)
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    client.upsert_record(
        source="ci_failure",
        message="boom",
        rule="R1",
        dedupe_key="",  # 空文字
    )
    props = api.create_calls[0]["properties"]
    stored = props["Dedupe Key"]["rich_text"][0]["text"]["content"]
    # 空のまま保存されない、自動生成された 16 hex に置き換わる
    assert stored != ""
    assert len(stored) == 16


def test_upsert_record_prunes_missing_property_on_create():
    """schema 未追加のプロパティが property_not_found で返るとき、除外して再試行"""
    api = _FakeAPI(existing_id=None, missing_property="Severity")
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    client.upsert_record(
        source="final_review",
        message="x",
        severity="high",
    )
    # 1 回目は Severity 込みで失敗、2 回目で Severity 抜きで成功
    assert len(api.create_calls) == 2
    assert "Severity" in api.create_calls[0]["properties"]
    assert "Severity" not in api.create_calls[1]["properties"]


def test_upsert_record_raises_when_non_property_validation_error():
    """property_not_found 以外の 400 エラーは prune せずに伝播"""
    api = _FakeAPI(existing_id=None)

    def _failing_create(payload):
        raise NotionAPIError(400, "invalid date format", code="validation_error")

    api.create_page = _failing_create  # type: ignore[assignment]
    client = ReviewIssuesDBClient(api=api, database_id="db-id")
    with pytest.raises(NotionAPIError):
        client.upsert_record(source="final_review", message="x")


# ---------------------------------------------------------------------------
# list_open_review_issues_for_workflow（Issue #54 / Workgraph 完成）
# ---------------------------------------------------------------------------


def test_list_open_review_issues_for_workflow_returns_pages():
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
        {"id": "ri-1", "properties": {"Title": {"title": [{"text": {"content": "Bug"}}]}}},
    ]])
    client = ReviewIssuesDBClient(api=api, database_id="ri-db")
    result = client.list_open_review_issues_for_workflow("wf-page")
    assert [r["id"] for r in result] == ["ri-1"]
    call_filter = api.query_calls[0]["filter"]
    assert "and" in call_filter
    status_clause = [c for c in call_filter["and"] if c.get("property") == "Status"]
    assert status_clause[0]["select"]["equals"] == "open"
    wf_clause = [c for c in call_filter["and"] if c.get("property") == "Workflow"]
    assert wf_clause[0]["relation"]["contains"] == "wf-page"


def test_list_open_review_issues_returns_empty_for_blank_page_id():
    api = _FakeAPI()
    client = ReviewIssuesDBClient(api=api, database_id="ri-db")
    assert client.list_open_review_issues_for_workflow("") == []
    assert client.list_open_review_issues_for_workflow(None) == []


def test_list_open_review_issues_returns_partial_on_api_failure():
    class _RaisingAPI:
        def query_database(self, *args, **kwargs):
            raise NotionAPIError(503, "service unavailable")

    client = ReviewIssuesDBClient(api=_RaisingAPI(), database_id="ri-db")
    assert client.list_open_review_issues_for_workflow("wf-page") == []
