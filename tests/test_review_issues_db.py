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

    def query_database(self, database_id: str, *, filter_: dict | None = None) -> dict:
        self.query_calls.append((database_id, filter_))
        if self._existing_id:
            return {"results": [{"id": self._existing_id}]}
        return {"results": []}

    def create_page(self, payload: dict) -> dict:
        # _submit_with_property_pruning は同じ properties dict を mutate して
        # 再呼び出しするため、リトライ毎の状態を捉えるには deep copy が必要。
        self.create_calls.append(copy.deepcopy(payload))
        # property_not_found を一度だけ返すモード
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
        return {"id": page_id, "properties": payload["properties"]}


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
