"""Prime v2 MVP-2: extract_prime_index_entries() と --query 出力の単体テスト

docs/design-prime-v2.md §8.1 MVP-2 (CLI フラグ + backfill) の純関数部分
(renderer / extractor) を検証する。CLI handler の e2e は別途 cli テストで
カバーする想定で、ここでは Notion data → index entries の変換と、
renderer の query セクション出力に絞る。
"""
from __future__ import annotations

from hokusai.integrations.notion_dashboard.prime_renderer import (
    extract_prime_index_entries,
    render_prime_json,
    render_prime_markdown,
)


def _memory_page(
    page_id: str,
    name: str,
    summary: str = "",
    content: str = "",
    applies_to: list[str] | None = None,
    memory_type: str = "project_rule",
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Summary": {"rich_text": [{"plain_text": summary}]},
            "Content": {"rich_text": [{"plain_text": content}]},
            "Applies To": {
                "multi_select": [{"name": v} for v in (applies_to or [])]
            },
            "Type": {"select": {"name": memory_type}},
        },
    }


def _work_item_page(
    page_id: str,
    title: str,
    description: str = "",
    phase: str | None = None,
    status: str = "ready",
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Description": {"rich_text": [{"plain_text": description}]},
            "Status": {"select": {"name": status}},
            "Phase": {"select": {"name": phase} if phase else None},
        },
    }


def _review_issue_page(
    page_id: str,
    title: str,
    message: str = "",
    file_path: str = "",
    severity: str = "blocker",
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Message": {"rich_text": [{"plain_text": message}]},
            "File Path": {"rich_text": [{"plain_text": file_path}]},
            "Severity": {"select": {"name": severity}},
        },
    }


def _gate_page(
    page_id: str,
    name: str,
    description: str = "",
    required_by_phase: int | None = None,
    status: str = "pending",
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Description": {"rich_text": [{"plain_text": description}]},
            "Required By Phase": {"number": required_by_phase},
            "Status": {"select": {"name": status}},
        },
    }


# ---------------------------------------------------------------------------
# extract_prime_index_entries
# ---------------------------------------------------------------------------


def test_extract_memory_entry_with_summary_priority():
    page = _memory_page(
        "page-1", name="rule A", summary="summary text", content="content text",
        applies_to=["phase4"],
    )
    entries = extract_prime_index_entries(memories=[page])
    assert len(entries) == 1
    assert entries[0]["source_type"] == "memory"
    assert entries[0]["source_id"] == "page-1"
    assert entries[0]["title"] == "rule A"
    # Summary が優先される (renderer と同じロジック)
    assert entries[0]["body"] == "summary text"
    assert entries[0]["phase"] == 4
    assert entries[0]["notion_page_id"] == "page-1"


def test_extract_memory_falls_back_to_content():
    page = _memory_page(
        "page-1", name="rule A", summary="   ", content="content text",
    )
    entries = extract_prime_index_entries(memories=[page])
    assert entries[0]["body"] == "content text"


def test_extract_skips_entry_without_page_id():
    page = {"properties": {"Name": {"title": [{"plain_text": "x"}]}}}  # no "id"
    entries = extract_prime_index_entries(memories=[page])
    assert entries == []


def test_extract_skips_entry_with_empty_title_and_body():
    page = _memory_page("page-1", name="", summary="", content="")
    entries = extract_prime_index_entries(memories=[page])
    assert entries == []


def test_extract_work_item():
    page = _work_item_page("wi-1", title="ready work", description="do A", phase="phase5")
    entries = extract_prime_index_entries(work_items=[page], memories=[])
    assert len(entries) == 1
    assert entries[0]["source_type"] == "work_item"
    assert entries[0]["source_id"] == "wi-1"
    assert entries[0]["title"] == "ready work"
    assert entries[0]["body"] == "do A"
    assert entries[0]["phase"] == 5


def test_extract_review_issue_with_file_path():
    page = _review_issue_page(
        "ri-1", title="bug A", message="msg text", file_path="src/x.py",
    )
    entries = extract_prime_index_entries(review_issues=[page], memories=[])
    assert len(entries) == 1
    assert entries[0]["source_type"] == "review_issue"
    assert entries[0]["title"] == "bug A"
    assert entries[0]["body"] == "msg text"
    assert entries[0]["file_path"] == "src/x.py"
    assert entries[0]["phase"] is None


def test_extract_gate_with_required_phase():
    page = _gate_page("g-1", name="security review", description="needed", required_by_phase=7)
    entries = extract_prime_index_entries(gates=[page], memories=[])
    assert len(entries) == 1
    assert entries[0]["source_type"] == "gate"
    assert entries[0]["title"] == "security review"
    assert entries[0]["body"] == "needed"
    assert entries[0]["phase"] == 7


def test_extract_handles_all_categories_together():
    entries = extract_prime_index_entries(
        memories=[_memory_page("m-1", "memory A", summary="m body")],
        work_items=[_work_item_page("wi-1", "work A", description="wi body")],
        review_issues=[_review_issue_page("ri-1", "issue A", message="ri body")],
        gates=[_gate_page("g-1", "gate A", description="g body")],
    )
    source_types = sorted(e["source_type"] for e in entries)
    assert source_types == ["gate", "memory", "review_issue", "work_item"]


# ---------------------------------------------------------------------------
# renderer query セクション
# ---------------------------------------------------------------------------


def test_render_markdown_v1_compat_without_query():
    """`--query` 未指定なら検索結果セクションは出力されない (v1 互換)"""
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile="hokusai",
        current_phase="phase4",
        memories=[],
    )
    assert "検索結果" not in out


def test_render_markdown_with_query_empty_results():
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        query="something",
        query_results=[],
    )
    assert "検索結果（query: `something`）" in out
    assert "_該当する記録はありません_" in out


def test_render_markdown_with_query_one_result():
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        query="outbox",
        query_results=[
            {
                "workflow_id": "wf-1",
                "source_type": "review_issue",
                "source_id": "ri-1",
                "phase": 4,
                "title": "Notion outbox 404",
                "body": "share missing",
                "notion_page_id": "np-1",
                "pr_url": None,
                "file_path": None,
            }
        ],
    )
    assert "## 検索結果（query: `outbox`）" in out
    assert "### Notion outbox 404" in out
    assert "**Source:** `review_issue`" in out
    assert "**Workflow:** `wf-1`" in out
    assert "**Phase:** `phase4`" in out
    assert "**Page:** `np-1`" in out
    assert "> share missing" in out


def test_render_markdown_query_result_with_pr_and_file():
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        query="docs",
        query_results=[
            {
                "workflow_id": "wf-1",
                "source_type": "pr",
                "source_id": "pr-130",
                "phase": None,
                "title": "Notion DB share docs",
                "body": "added §2.2.1",
                "notion_page_id": None,
                "pr_url": "https://github.com/shigenoko/hokusai/pull/130",
                "file_path": "docs/notion-dashboard-operation-guide.md",
            }
        ],
    )
    assert "**PR:** https://github.com/shigenoko/hokusai/pull/130" in out
    assert "**File:** `docs/notion-dashboard-operation-guide.md`" in out
    # phase は None なので表示されない
    assert "Phase:" not in out.split("検索結果")[1].split("Source:")[0]


def test_render_markdown_query_result_untitled_fallback():
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        query="x",
        query_results=[
            {
                "workflow_id": "wf-1",
                "source_type": "memory",
                "source_id": "m-1",
                "title": "",
                "body": "only body",
            }
        ],
    )
    assert "### (untitled)" in out
    assert "> only body" in out


def test_render_json_v1_compat_query_keys_null_when_unspecified():
    """`--query` 未指定なら JSON の query / query_results が null (v1 互換)"""
    import json
    out = render_prime_json(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
    )
    payload = json.loads(out)
    assert payload["query"] is None
    assert payload["query_results"] is None


def test_render_json_query_results_present():
    import json
    out = render_prime_json(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        query="outbox",
        query_results=[{"source_type": "memory", "title": "t", "body": "b"}],
    )
    payload = json.loads(out)
    assert payload["query"] == "outbox"
    assert payload["query_results"] == [
        {"source_type": "memory", "title": "t", "body": "b"}
    ]
