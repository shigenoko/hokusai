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


# ---------------------------------------------------------------------------
# CLI handler 経路 (PR #135 Copilot Round 1 #4 指摘の e2e カバレッジ追加)
# ---------------------------------------------------------------------------


import io  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.cli_main import (  # noqa: E402
    _handle_prime,
    _positive_int,
    _sanitize_fts5_query,
)
from hokusai.config.models import (  # noqa: E402
    NotionDashboardConfig,
    NotionSyncRateLimitConfig,
    WorkflowConfig,
)
from hokusai.persistence.sqlite_store import SQLiteStore  # noqa: E402


class _Args:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_config(tmp_path: Path) -> WorkflowConfig:
    return WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
        notion_dashboard=NotionDashboardConfig(
            enabled=True,
            api_token_env="TEST_API_TOKEN",
            project_memory_db_id_env="TEST_MEMORY_DB",
            workflows_db_id_env="TEST_WORKFLOWS_DB",
            rate_limit=NotionSyncRateLimitConfig(
                requests_per_second=100, debounce_ms=0
            ),
        ),
    )


def _seed_workflow(config: WorkflowConfig, workflow_id: str = "wf-1") -> None:
    store = SQLiteStore(config.database_path)
    store.save_workflow(workflow_id, {
        "workflow_id": workflow_id,
        "profile_name": "acme",
        "current_phase": 4,
    })


def test_positive_int_validator():
    """--query-limit が正の整数のみ受け付けることを単体で検証"""
    import argparse
    assert _positive_int("1") == 1
    assert _positive_int("10") == 10
    assert _positive_int("100") == 100
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("abc")


def test_sanitize_fts5_query_wraps_phrase():
    """raw クエリを phrase 形式 `"..."` でラップする"""
    assert _sanitize_fts5_query("Notion outbox") == '"Notion outbox"'
    # 引用符は `""` に escape
    assert _sanitize_fts5_query('he said "hi"') == '"he said ""hi"""'
    # 予約文字を含む入力もエラーなく phrase 化される
    assert _sanitize_fts5_query("foo:bar (baz)") == '"foo:bar (baz)"'
    assert _sanitize_fts5_query("-leading-dash") == '"-leading-dash"'


def test_sanitize_fts5_query_blank_passthrough():
    """空文字 / 空白のみは raw を返す（search_prime_index 側で空 result 返却）"""
    assert _sanitize_fts5_query("") == ""
    assert _sanitize_fts5_query("   ") == "   "


def test_handle_prime_query_path_runs_backfill_and_search(
    tmp_path, monkeypatch
):
    """--query 経路で extract → clear → upsert → search が走り、検索結果が
    Markdown 出力に現れることを e2e で確認する。"""
    out = io.StringIO()
    err = io.StringIO()
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)

    monkeypatch.setenv("TEST_API_TOKEN", "tok")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    class _FakeAPI:
        def __init__(self, *a, **kw):
            return

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            return

        def list_active_memories(self, *, profile, phase, types, **kwargs):
            return [_memory_page("page-1", "rule A", summary="outbox error observed")]

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )

    args = _Args(
        workflow_id="wf-1", phase=None, memory_types=None, output="markdown",
        query="outbox", query_limit=10,
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    body = out.getvalue()
    # 検索結果セクションが現れる
    assert "## 検索結果（query: `outbox`）" in body
    # backfill された memory が citation 付きで現れる
    assert "rule A" in body
    assert "**Source:** `memory`" in body
    # backfill 経由で prime_index にデータが入ったことを直接確認
    store = SQLiteStore(cfg.database_path)
    assert len(store.search_prime_index('"outbox"')) >= 1


def test_handle_prime_clears_stale_when_active_context_becomes_empty(
    tmp_path, monkeypatch
):
    """active context が 0 件になった場合、過去 backfill 行を必ず clear する
    (PR #135 Copilot Round 1 #1 指摘)。"""
    out = io.StringIO()
    err = io.StringIO()
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    # 過去 backfill された stale 行を直接書き込む
    store = SQLiteStore(cfg.database_path)
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="stale-1",
        title="old rule", body="old content",
    )
    assert len(store.search_prime_index('"old rule"')) == 1

    monkeypatch.setenv("TEST_API_TOKEN", "tok")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    class _FakeAPI:
        def __init__(self, *a, **kw):
            return

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            return

        def list_active_memories(self, **kwargs):
            # active context が 0 件になったケース
            return []

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )

    args = _Args(
        workflow_id="wf-1", phase=None, memory_types=None, output="markdown",
        query=None, query_limit=10,
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    # stale 行が clear されている
    assert store.search_prime_index('"old rule"') == []


def test_handle_prime_keeps_stale_when_notion_fetch_errors(
    tmp_path, monkeypatch
):
    """Notion fetch エラー時は stale 行を残す（部分的に役立つ可能性があるため）
    (PR #135 Copilot Round 1 #1 指摘の境界条件)。"""
    out = io.StringIO()
    err = io.StringIO()
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    store = SQLiteStore(cfg.database_path)
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="kept-1",
        title="kept rule", body="kept content",
    )

    monkeypatch.setenv("TEST_API_TOKEN", "tok")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    class _FakeAPI:
        def __init__(self, *a, **kw):
            return

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            return

        def list_active_memories(self, **kwargs):
            raise RuntimeError("simulated Notion outage")

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )

    args = _Args(
        workflow_id="wf-1", phase=None, memory_types=None, output="markdown",
        query=None, query_limit=10,
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    # Notion fetch が失敗したので clear されず、stale 行が残る
    assert len(store.search_prime_index('"kept rule"')) == 1


def test_handle_prime_v1_compat_without_query(tmp_path, monkeypatch):
    """--query 未指定で JSON 出力に query / query_results が null として現れる
    (v1 互換確認)。"""
    out = io.StringIO()
    err = io.StringIO()
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "tok")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    class _FakeAPI:
        def __init__(self, *a, **kw):
            return

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            return

        def list_active_memories(self, **kwargs):
            return []

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )

    args = _Args(
        workflow_id="wf-1", phase=None, memory_types=None, output="json",
        query=None, query_limit=10,
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["query"] is None
    assert payload["query_results"] is None


def test_handle_prime_diagnostic_row_on_prime_index_error(
    tmp_path, monkeypatch
):
    """prime_index 失敗時に diagnostics に「prime_index: 失敗 (...)」が出る
    (PR #135 Copilot Round 1 #5 指摘)。"""
    out = io.StringIO()
    err = io.StringIO()
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "tok")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    class _FakeAPI:
        def __init__(self, *a, **kw):
            return

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            return

        def list_active_memories(self, **kwargs):
            return []

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )

    # SQLiteStore.clear_prime_index_for_workflow を強制例外化
    def _boom(self, *a, **kw):
        raise RuntimeError("simulated index failure")
    monkeypatch.setattr(SQLiteStore, "clear_prime_index_for_workflow", _boom)

    args = _Args(
        workflow_id="wf-1", phase=None, memory_types=None, output="json",
        query=None, query_limit=10,
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    payload = json.loads(out.getvalue())
    diag = payload["diagnostics"] or []
    assert any("prime_index" in d and "失敗" in d for d in diag)
    # stderr にも warning が出る
    assert "prime_index backfill / search で失敗" in err.getvalue()
