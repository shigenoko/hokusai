"""Prime v2 MVP-2: extract_prime_index_entries() / --query / CLI handler の単体テスト

docs/design-prime-v2.md §8.1 MVP-2 (CLI フラグ + backfill) の以下 3 層を
1 ファイルで集約検証する:

- 純関数: `extract_prime_index_entries()` の Notion data → index entries 変換
- renderer: `render_prime_markdown()` / `render_prime_json()` の query
  セクション出力 (v1 互換 / None vs [] 区別 / error embed 含む)
- CLI handler 経路: `_handle_prime()` を直接呼ぶ e2e (backfill clear/upsert、
  --query / --query-limit / --type フィルタ、stale 保護、graceful degrade)
  + `_positive_int()` / `_sanitize_fts5_query()` 等のヘルパ

PR #135 Copilot Round 1 #4 指摘で CLI handler 経路の e2e を本ファイルに
追加し、Round 6 でその拡張範囲を明示するため docstring を更新済み。
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


class _FakeAPI:
    """テスト用 NotionAPIClient: コンストラクタは no-op、メソッド呼び出しは
    fixture 側で個別に monkeypatch される想定。"""
    def __init__(self, *a, **kw):
        return


def _make_fake_mem_client(*, memories_factory=None, raise_exc=None):
    """テスト用 ProjectMemoryDBClient を返すファクトリ。

    PR #135 Copilot Round 2 で SonarCloud 重複検知 (5.1%) に引っかかった
    `_FakeAPI` / `_FakeMemClient` の繰り返し定義を 1 箇所にまとめる。
    """
    memories = memories_factory() if memories_factory else []

    class _FakeClient:
        def __init__(self, *, api, database_id):
            return

        def list_active_memories(self, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            return memories

    return _FakeClient


def _patch_notion_clients(
    monkeypatch, *, memories_factory=None, raise_exc=None
) -> None:
    """NotionAPIClient と ProjectMemoryDBClient を fake に差し替える共通 helper"""
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _make_fake_mem_client(memories_factory=memories_factory, raise_exc=raise_exc),
    )


def _setup_prime_env(tmp_path, monkeypatch, *, memories_factory=None, raise_exc=None):
    """prime CLI handler のテスト用 env / config / mock を統括セットアップ。

    Returns:
        (cfg, store): WorkflowConfig と SQLiteStore のタプル
    """
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "tok")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")
    _patch_notion_clients(monkeypatch, memories_factory=memories_factory, raise_exc=raise_exc)
    return cfg, SQLiteStore(cfg.database_path)


def _run_handle_prime(cfg, **arg_overrides):
    """`_handle_prime` を redirect_stdout/stderr で実行して (rc, stdout, stderr) を返す"""
    out = io.StringIO()
    err = io.StringIO()
    args = _Args(
        workflow_id=arg_overrides.pop("workflow_id", "wf-1"),
        phase=arg_overrides.pop("phase", None),
        memory_types=arg_overrides.pop("memory_types", None),
        output=arg_overrides.pop("output", "markdown"),
        query=arg_overrides.pop("query", None),
        query_limit=arg_overrides.pop("query_limit", 10),
        **arg_overrides,
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    return rc, out.getvalue(), err.getvalue()


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


def test_handle_prime_query_path_runs_backfill_and_search(tmp_path, monkeypatch):
    """--query 経路で extract → clear → upsert → search が走り、検索結果が
    Markdown 出力に現れることを e2e で確認する。"""
    cfg, store = _setup_prime_env(
        tmp_path, monkeypatch,
        memories_factory=lambda: [
            _memory_page("page-1", "rule A", summary="outbox error observed"),
        ],
    )
    rc, body, _ = _run_handle_prime(cfg, query="outbox")
    assert rc == 0
    assert "## 検索結果（query: `outbox`）" in body
    assert "rule A" in body
    assert "**Source:** `memory`" in body
    assert len(store.search_prime_index('"outbox"')) >= 1


def test_handle_prime_clears_stale_when_active_context_becomes_empty(
    tmp_path, monkeypatch
):
    """active context が 0 件になった場合、過去 backfill 行を必ず clear する
    (PR #135 Copilot Round 1 #1 指摘)。"""
    cfg, store = _setup_prime_env(tmp_path, monkeypatch)
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="stale-1",
        title="old rule", body="old content",
    )
    assert len(store.search_prime_index('"old rule"')) == 1

    rc, _, _ = _run_handle_prime(cfg)
    assert rc == 0
    # stale 行が clear されている
    assert store.search_prime_index('"old rule"') == []


def test_handle_prime_keeps_stale_when_notion_fetch_errors(
    tmp_path, monkeypatch
):
    """Notion fetch エラー時は stale 行を残す（部分的に役立つ可能性があるため）
    (PR #135 Copilot Round 1 #1 指摘の境界条件)。"""
    cfg, store = _setup_prime_env(
        tmp_path, monkeypatch,
        raise_exc=RuntimeError("simulated Notion outage"),
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="kept-1",
        title="kept rule", body="kept content",
    )
    rc, _, _ = _run_handle_prime(cfg)
    assert rc == 0
    # Notion fetch が失敗したので clear されず、stale 行が残る
    assert len(store.search_prime_index('"kept rule"')) == 1


def test_handle_prime_keeps_stale_when_notion_not_attempted(
    tmp_path, monkeypatch
):
    """Notion fetch を試行していない (token / DB ID 未設定) 場合は stale 行を
    残す (PR #135 Copilot Round 2 #1 指摘)。"""
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    # token / DB ID env を意図的に削除（fetch 試行されない経路）
    monkeypatch.delenv("TEST_API_TOKEN", raising=False)
    monkeypatch.delenv("TEST_MEMORY_DB", raising=False)
    store = SQLiteStore(cfg.database_path)
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="kept-1",
        title="persisted rule", body="persisted body",
    )
    rc, _, _ = _run_handle_prime(cfg)
    assert rc == 0
    # 未試行なので stale 行は残る（保護される）
    assert len(store.search_prime_index('"persisted rule"')) == 1


def test_handle_prime_v1_compat_without_query(tmp_path, monkeypatch):
    """--query 未指定で JSON 出力に query / query_results が null として現れる
    (v1 互換確認)。"""
    cfg, _ = _setup_prime_env(tmp_path, monkeypatch)
    rc, out, _ = _run_handle_prime(cfg, output="json")
    assert rc == 0
    payload = json.loads(out)
    assert payload["query"] is None
    assert payload["query_results"] is None


def test_handle_prime_diagnostic_row_on_prime_index_error(
    tmp_path, monkeypatch
):
    """prime_index 失敗時に diagnostics に「prime_index: 失敗 (...)」が出る
    (PR #135 Copilot Round 1 #5 指摘)。"""
    cfg, _ = _setup_prime_env(tmp_path, monkeypatch)

    # SQLiteStore.clear_prime_index_for_workflow を強制例外化
    def _boom(self, *a, **kw):
        raise RuntimeError("simulated index failure")
    monkeypatch.setattr(SQLiteStore, "clear_prime_index_for_workflow", _boom)

    rc, out, err = _run_handle_prime(cfg, output="json")
    assert rc == 0
    payload = json.loads(out)
    diag = payload["diagnostics"] or []
    assert any("prime_index" in d and "失敗" in d for d in diag)
    # stderr にも warning が出る
    assert "prime_index backfill / search で失敗" in err


def test_handle_prime_partial_fetch_preserves_other_source_types(
    tmp_path, monkeypatch
):
    """部分 fetch (Project Memory のみ成功 / Workflows DB ID 未設定で
    work_item/review_issue/gate=None) でも、過去 backfill された他 source_type
    の行は保護される (PR #135 Copilot Round 3 #1 指摘)。
    """
    cfg, store = _setup_prime_env(
        tmp_path, monkeypatch,
        memories_factory=lambda: [
            _memory_page("page-1", "new memory", summary="fresh body")
        ],
    )
    # 過去 backfill された review_issue / work_item 行を直接入れる
    # (Workflows DB ID 未設定で fetch されないが、過去の backfill 結果として保持)
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="review_issue", source_id="ri-old",
        title="old issue", body="historical issue",
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="work_item", source_id="wi-old",
        title="old work", body="historical work",
    )

    # Workflows DB ID env を意図的に削除 → work_item/review_issue/gate は fetch されない
    monkeypatch.delenv("TEST_WORKFLOWS_DB", raising=False)
    rc, _, _ = _run_handle_prime(cfg)
    assert rc == 0

    # memory は新しい entry に置き換わっている
    assert len(store.search_prime_index('"fresh body"')) == 1
    # review_issue / work_item は fetch されなかったので過去 backfill が保護される
    assert len(store.search_prime_index('"old issue"')) == 1
    assert len(store.search_prime_index('"old work"')) == 1


def test_handle_prime_type_filter_preserves_other_memory_types(
    tmp_path, monkeypatch
):
    """`--type avoidance` のような type フィルタ指定時、Notion fetch も
    指定 type のみ実施されるので、過去 backfill された他 type (project_rule
    / handover_note 等) の memory 行を保護する (PR #135 Copilot Round 4 #1)。"""
    cfg, store = _setup_prime_env(
        tmp_path, monkeypatch,
        memories_factory=lambda: [
            _memory_page(
                "page-new", "new avoidance", summary="new avoidance body",
                memory_type="avoidance",
            )
        ],
    )
    # 過去 backfill されていた project_rule / handover_note を直接入れる
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="page-old-rule",
        title="old rule", body="historical rule",
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="page-old-handover",
        title="old handover", body="historical handover",
    )

    # --type avoidance だけ指定
    rc, _, _ = _run_handle_prime(cfg, memory_types=["avoidance"])
    assert rc == 0

    # 過去 backfill された他 type の memory 行は保護される
    assert len(store.search_prime_index('"historical rule"')) == 1
    assert len(store.search_prime_index('"historical handover"')) == 1
    # 新しい avoidance entry は (--type フィルタ無しの将来 fetch で) 入る経路。
    # 本テストでは fetched_source_types から memory が外れるので未 backfill。
    # これは設計上の trade-off（次回 --type なしで全 memory が refresh される）。


def test_render_markdown_v1_compat_without_gaps():
    """`--include-gaps` 未指定 (gaps=None) なら gap セクションは現れない"""
    out = render_prime_markdown(
        workflow_id="wf-1", profile=None, current_phase=None,
        memories=[],
    )
    assert "gap analysis" not in out
    assert "未確定 / 不足情報" not in out


def test_render_markdown_with_empty_gaps_shows_no_gap_message():
    out = render_prime_markdown(
        workflow_id="wf-1", profile=None, current_phase=None,
        memories=[], gaps=[],
    )
    assert "## 未確定 / 不足情報 (gap analysis)" in out
    assert "_検出された gap はありません_" in out


def test_render_markdown_with_gap_entries():
    out = render_prime_markdown(
        workflow_id="wf-1", profile=None, current_phase=None,
        memories=[],
        gaps=[
            {"kind": "audit_log_silence", "phase": None,
             "detail": "LLM Gateway enabled but audit empty"},
            {"kind": "notion_outbox_pending", "phase": 4,
             "detail": "3 件 pending"},
        ],
    )
    assert "### `audit_log_silence`" in out
    assert "> LLM Gateway enabled but audit empty" in out
    assert "### `notion_outbox_pending`" in out
    assert "**Phase:** `phase4`" in out


def test_render_json_v1_compat_gaps_null_when_unspecified():
    import json as _json
    payload = _json.loads(render_prime_json(
        workflow_id="wf-1", profile=None, current_phase=None,
        memories=[],
    ))
    assert payload["gaps"] is None


def test_render_json_gaps_present():
    import json as _json
    payload = _json.loads(render_prime_json(
        workflow_id="wf-1", profile=None, current_phase=None,
        memories=[],
        gaps=[{"kind": "x", "phase": None, "detail": "y"}],
    ))
    assert payload["gaps"] == [{"kind": "x", "phase": None, "detail": "y"}]


def test_handle_prime_include_gaps_e2e(tmp_path, monkeypatch):
    """--include-gaps で gap section が Markdown / JSON 両方に出る"""
    cfg, store = _setup_prime_env(tmp_path, monkeypatch)
    # outbox に 1 件投入 → notion_outbox_pending gap
    store.enqueue_notion_sync(
        idempotency_key="k1", workflow_id="wf-1",
        event_type="workflow_started", payload={},
    )
    rc, body, _ = _run_handle_prime(cfg, include_gaps=True)
    assert rc == 0
    assert "## 未確定 / 不足情報 (gap analysis)" in body
    assert "### `notion_outbox_pending`" in body

    # JSON でも gaps key が list として現れる
    rc, body, _ = _run_handle_prime(cfg, include_gaps=True, output="json")
    assert rc == 0
    payload = json.loads(body)
    kinds = [g["kind"] for g in payload["gaps"]]
    assert "notion_outbox_pending" in kinds


def test_handle_prime_include_gaps_mvp5_phase4_plan_missing(
    tmp_path, monkeypatch
):
    """MVP-5: current_phase>=5 で work_plan 空の workflow に --include-gaps
    を付けると phase4_plan_missing gap が CLI 経由で出る。"""
    cfg, store = _setup_prime_env(tmp_path, monkeypatch)
    # phase 6 / work_plan 空 で上書き保存 (seed は phase4 なので閾値未満)
    store.save_workflow("wf-1", {
        "workflow_id": "wf-1",
        "profile_name": "acme",
        "current_phase": 6,
        "work_plan": None,
    })
    rc, body, _ = _run_handle_prime(cfg, include_gaps=True, output="json")
    assert rc == 0
    payload = json.loads(body)
    kinds = [g["kind"] for g in payload["gaps"]]
    assert "phase4_plan_missing" in kinds


def test_handle_prime_without_include_gaps_keeps_v1_output(
    tmp_path, monkeypatch
):
    """--include-gaps なしなら gap section が現れない (MVP-1〜3 互換)"""
    cfg, _ = _setup_prime_env(tmp_path, monkeypatch)
    rc, body, _ = _run_handle_prime(cfg)
    assert rc == 0
    assert "gap analysis" not in body

    rc, body, _ = _run_handle_prime(cfg, output="json")
    assert rc == 0
    payload = json.loads(body)
    assert payload["gaps"] is None


def test_handle_prime_dry_run_skips_backfill_and_search(
    tmp_path, monkeypatch
):
    """--dry-run 時は backfill (clear + upsert) と search を skip し、
    SQLite を mutate しない (PR #135 Copilot Round 7 指摘)。
    """
    cfg, store = _setup_prime_env(
        tmp_path, monkeypatch,
        memories_factory=lambda: [
            _memory_page("page-1", "new rule", summary="new rule body")
        ],
    )
    # 過去 backfill された stale 行
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="stale-1",
        title="stale rule", body="stale rule body",
    )
    assert len(store.search_prime_index('"stale rule body"')) == 1

    rc, body, err = _run_handle_prime(cfg, query="rule", dry_run=True)
    assert rc == 0
    # SQLite が mutate されない: stale 行は残り、新 entry は upsert されない
    assert len(store.search_prime_index('"stale rule body"')) == 1
    assert store.search_prime_index('"new rule body"') == []
    # stderr に dry-run 通知が出る
    assert "--dry-run" in err and "skip" in err
    # query 指定だが search も skip されるので、Markdown 出力では
    # query_results=None 経路 (「インデックス利用不可」表示)
    assert "検索インデックスが利用不可" in body


def test_handle_prime_dry_run_without_query_no_stderr_noise(
    tmp_path, monkeypatch
):
    """dry-run + --query 未指定 + Notion fetch 試行ありなら stderr 通知が出る
    (backfill は skip されるため)。
    """
    cfg, _ = _setup_prime_env(
        tmp_path, monkeypatch,
        memories_factory=lambda: [
            _memory_page("page-1", "rule", summary="body")
        ],
    )
    rc, _, err = _run_handle_prime(cfg, dry_run=True)
    assert rc == 0
    # backfill が skip された旨が stderr に出る (fetched_source_types non-empty)
    assert "--dry-run" in err and "skip" in err


def test_handle_prime_query_limit_caps_result_count(tmp_path, monkeypatch):
    """--query-limit が search_prime_index に伝播し、検索結果数が cap される
    ことを e2e で検証 (PR #135 Copilot Round 6 #2 指摘)。"""
    # 5 件の memory を fetch する fixture（全件が body=`shared phrase` を含む）
    cfg, store = _setup_prime_env(
        tmp_path, monkeypatch,
        memories_factory=lambda: [
            _memory_page(f"page-{i}", f"rule {i}", summary=f"shared phrase {i}")
            for i in range(5)
        ],
    )

    # default limit (10) なら 5 件全部返る
    rc, body, _ = _run_handle_prime(cfg, query="shared")
    assert rc == 0
    assert body.count("**Source:** `memory`") == 5

    # --query-limit 2 で結果が 2 件に cap される
    rc, body, _ = _run_handle_prime(cfg, query="shared", query_limit=2)
    assert rc == 0
    assert body.count("**Source:** `memory`") == 2
    # backfill された entries は 5 件全部 SQLite 上に残る
    assert len(store.search_prime_index('"shared"')) == 5


def test_handle_prime_type_filter_skips_memory_upsert_too(
    tmp_path, monkeypatch
):
    """--type 指定時、memory は clear からも upsert からも除外されること
    (PR #135 Copilot Round 5 指摘): clear/upsert の対称性が崩れると、
    fetched memory rows が新規追加されつつ古い行が残って stale 化する。
    """
    cfg, store = _setup_prime_env(
        tmp_path, monkeypatch,
        memories_factory=lambda: [
            _memory_page(
                "page-new", "freshly fetched", summary="fresh body fetched",
                memory_type="avoidance",
            )
        ],
    )
    # 過去 backfill された memory entries (両方とも残るべき)
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="page-old-rule",
        title="old", body="historical kept rule",
    )

    # --type avoidance だけ指定: clear/upsert どちらからも memory が除外される
    rc, _, _ = _run_handle_prime(cfg, memory_types=["avoidance"])
    assert rc == 0

    # 過去 backfill は残る
    assert len(store.search_prime_index('"historical kept rule"')) == 1
    # 新規 fetched memory は upsert されない (filter view の不対称性回避)
    assert store.search_prime_index('"fresh body fetched"') == []


def test_clear_prime_index_source_types_filter(tmp_path):
    """SQLiteStore.clear_prime_index_for_workflow に source_types を渡すと、
    指定 source_type のみ削除される（その他は保護される）。"""
    store = SQLiteStore(tmp_path / "wf.db")
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="m-1",
        title="t", body="memory body",
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="review_issue", source_id="ri-1",
        title="t", body="review body",
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="work_item", source_id="wi-1",
        title="t", body="work body",
    )

    # memory と review_issue だけ削除
    deleted = store.clear_prime_index_for_workflow(
        "wf-1", source_types=["memory", "review_issue"]
    )
    assert deleted == 2
    # work_item は残る
    assert len(store.search_prime_index('"work body"')) == 1
    assert store.search_prime_index('"memory body"') == []
    assert store.search_prime_index('"review body"') == []


def test_clear_prime_index_empty_source_types_is_noop(tmp_path):
    """source_types=[] は no-op として 0 を返す"""
    store = SQLiteStore(tmp_path / "wf.db")
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="m-1",
        title="t", body="b",
    )
    deleted = store.clear_prime_index_for_workflow("wf-1", source_types=[])
    assert deleted == 0
    assert len(store.search_prime_index('"b"')) == 1


def test_handle_prime_query_error_message_embedded_in_section(
    tmp_path, monkeypatch
):
    """active context が non-empty で query 失敗時、prime_index_error を query
    セクション内に直接 embed する (PR #135 Copilot Round 3 #2 指摘)。
    diagnostics は has_any=True で抑制されるので diagnostics 参照では不十分。
    """
    cfg, _ = _setup_prime_env(
        tmp_path, monkeypatch,
        memories_factory=lambda: [
            _memory_page("page-1", "active rule", summary="active body")
        ],
    )

    # search_prime_index を強制例外化 (clear/upsert は成功する → has_any=True パス)
    def _boom_search(self, *a, **kw):
        raise RuntimeError("simulated search outage")
    monkeypatch.setattr(SQLiteStore, "search_prime_index", _boom_search)

    rc, body, _ = _run_handle_prime(cfg, query="anything")
    assert rc == 0
    # active context (memory) は表示される (has_any=True パス)
    assert "active rule" in body
    # query セクション内に error 詳細が embed される
    assert "## 検索結果（query: `anything`）" in body
    assert "検索インデックスが利用不可" in body
    assert "simulated search outage" in body


def test_handle_prime_query_section_distinguishes_none_vs_empty(
    tmp_path, monkeypatch
):
    """`--query` 経路で prime_index が失敗した場合 (query_results=None) は
    `_検索インデックスが利用不可..._` を表示し、検索成功で 0 件 (query_results=[])
    は `_該当する記録はありません_` を表示する (PR #135 Copilot Round 2 #2 指摘)。"""
    cfg, _ = _setup_prime_env(tmp_path, monkeypatch)

    # 成功で 0 件のケース
    rc, body, _ = _run_handle_prime(cfg, query="nothing")
    assert rc == 0
    assert "_該当する記録はありません_" in body
    assert "検索インデックスが利用不可" not in body

    # 失敗のケース: clear を強制例外化（query_results は None のまま）
    def _boom(self, *a, **kw):
        raise RuntimeError("simulated index failure")
    monkeypatch.setattr(SQLiteStore, "clear_prime_index_for_workflow", _boom)
    rc, body, _ = _run_handle_prime(cfg, query="anything")
    assert rc == 0
    assert "検索インデックスが利用不可のため検索できませんでした" in body
    assert "_該当する記録はありません_" not in body
