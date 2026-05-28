"""`hokusai audit list/show` サブコマンドと SQLiteStore helper のテスト（PR #123 / F3）

dogfooding-findings.md §7 F3 で記録した「audit_logs を CLI から覗く経路が無い」
運用穴を埋める PR の回帰防止テスト。
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from hokusai.persistence.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# SQLiteStore.list_audit_logs / get_audit_log
# ---------------------------------------------------------------------------


@pytest.fixture
def store_with_audit_rows(tmp_path: Path) -> SQLiteStore:
    """audit_logs に複数行入れた SQLiteStore を返す"""
    db_path = tmp_path / "workflow.db"
    store = SQLiteStore(str(db_path))
    # audit_logs の FK 制約 (workflow_id REFERENCES workflows) を満たすため
    # 先に workflow 行を作る（PRAGMA foreign_keys=OFF が default だが念のため）
    store.save_workflow(
        "wf-test-001",
        {
            "workflow_id": "wf-test-001",
            "task_url": "https://example.com/1",
            "task_title": "T1",
            "current_phase": 2,
            "branch_name": "feature/test",
            "base_branch": "main",
            "run_mode": "auto",
            "schema_change_required": False,
        },
    )
    store.save_workflow(
        "wf-test-002",
        {
            "workflow_id": "wf-test-002",
            "task_url": "https://example.com/2",
            "task_title": "T2",
            "current_phase": 7,
            "branch_name": "feature/test2",
            "base_branch": "main",
            "run_mode": "auto",
            "schema_change_required": False,
        },
    )
    # audit_logs: workflow_id / phase / action / status 違いを 5 件
    store.add_audit_log("wf-test-001", 2, "llm_gateway_decision", "log",
                        {"decision": "log", "policy_hits": []})
    store.add_audit_log("wf-test-001", 2, "llm_gateway_decision", "block",
                        {"decision": "block",
                         "policy_hits": ["unknown_provider"]})
    store.add_audit_log("wf-test-001", 3, "research_output_retry", "warning",
                        {"reason": "validation_failed"})
    store.add_audit_log("wf-test-002", 7, "llm_gateway_decision", "log",
                        {"decision": "log"})
    store.add_audit_log("wf-test-002", 8, "review_fix_applied", "info",
                        {"comments_count": 3})
    return store


def test_list_audit_logs_returns_latest_first(store_with_audit_rows):
    """`list_audit_logs(limit=N)` は `ORDER BY id DESC` で最新を先頭に返す"""
    rows = store_with_audit_rows.list_audit_logs(limit=10)
    assert len(rows) == 5
    # 最新 (id=5) が先頭
    assert rows[0]["id"] == 5
    assert rows[-1]["id"] == 1
    # 各行に必須キーが存在
    for r in rows:
        assert {"id", "workflow_id", "phase", "action", "status",
                "details", "created_at"} <= set(r.keys())


def test_list_audit_logs_filters_by_workflow_id(store_with_audit_rows):
    """workflow_id で絞り込むと一致行のみ返る"""
    rows = store_with_audit_rows.list_audit_logs(
        workflow_id="wf-test-001", limit=10,
    )
    assert len(rows) == 3
    assert all(r["workflow_id"] == "wf-test-001" for r in rows)


def test_list_audit_logs_filters_by_phase(store_with_audit_rows):
    """phase で絞り込むと一致行のみ返る"""
    rows = store_with_audit_rows.list_audit_logs(phase=2, limit=10)
    assert len(rows) == 2
    assert all(r["phase"] == 2 for r in rows)


def test_list_audit_logs_filters_by_status(store_with_audit_rows):
    """status で絞り込むと一致行のみ返る"""
    rows = store_with_audit_rows.list_audit_logs(status="block", limit=10)
    assert len(rows) == 1
    assert rows[0]["status"] == "block"
    assert rows[0]["details"]["policy_hits"] == ["unknown_provider"]


def test_list_audit_logs_filters_by_action(store_with_audit_rows):
    """action で絞り込むと一致行のみ返る"""
    rows = store_with_audit_rows.list_audit_logs(
        action="llm_gateway_decision", limit=10,
    )
    assert len(rows) == 3
    assert all(r["action"] == "llm_gateway_decision" for r in rows)


def test_list_audit_logs_combines_filters(store_with_audit_rows):
    """複数フィルタは AND で適用される"""
    rows = store_with_audit_rows.list_audit_logs(
        workflow_id="wf-test-001",
        action="llm_gateway_decision",
        limit=10,
    )
    assert len(rows) == 2
    assert all(r["workflow_id"] == "wf-test-001"
               and r["action"] == "llm_gateway_decision" for r in rows)


def test_list_audit_logs_respects_limit(store_with_audit_rows):
    """`limit` 引数が SQL の LIMIT に渡る"""
    rows = store_with_audit_rows.list_audit_logs(limit=2)
    assert len(rows) == 2


def test_get_audit_log_returns_single_row(store_with_audit_rows):
    """`get_audit_log(id)` で id 指定の単一行が返る"""
    row = store_with_audit_rows.get_audit_log(2)
    assert row is not None
    assert row["id"] == 2
    assert row["status"] == "block"
    assert row["details"]["policy_hits"] == ["unknown_provider"]


def test_get_audit_log_returns_none_for_missing_id(store_with_audit_rows):
    """存在しない id では None を返す"""
    row = store_with_audit_rows.get_audit_log(999)
    assert row is None


# ---------------------------------------------------------------------------
# CLI: _handle_audit (list / show)
# ---------------------------------------------------------------------------


def _run_audit(args_list: list[str], database_path: Path) -> tuple[int, str]:
    """`hokusai audit ...` を internal handler 経由で実行し (rc, stdout) を返す"""
    from hokusai.cli_main import _handle_audit

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.database_path = database_path

    # argparse でパース
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="audit_subcommand")
    p_list = sub.add_parser("list")
    p_list.add_argument("--workflow-id", default=None)
    p_list.add_argument("--phase", type=int, default=None)
    p_list.add_argument("--action", default=None)
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--output", choices=("table", "json"), default="table")
    p_show = sub.add_parser("show")
    p_show.add_argument("audit_id", type=int)
    args = parser.parse_args(args_list)

    buf = StringIO()
    with patch.object(sys, "stdout", buf):
        rc = _handle_audit(args, cfg)
    return rc, buf.getvalue()


def test_cli_audit_list_table_outputs_header_and_rows(
    store_with_audit_rows, tmp_path,
):
    """`audit list` (table) でヘッダーと audit_logs 行が stdout に出る"""
    db_path = Path(store_with_audit_rows.db_path)
    rc, output = _run_audit(["list", "--limit", "10"], db_path)
    assert rc == 0
    assert "id" in output  # ヘッダ
    assert "wf-test-001" in output
    assert "llm_gateway_decision" in output


def test_cli_audit_list_json_outputs_valid_json(
    store_with_audit_rows, tmp_path,
):
    """`audit list --output json` が valid JSON を出す"""
    db_path = Path(store_with_audit_rows.db_path)
    rc, output = _run_audit(
        ["list", "--output", "json", "--limit", "10"], db_path,
    )
    assert rc == 0
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    assert len(parsed) == 5
    # details はネスト dict としてパースされる
    assert isinstance(parsed[0]["details"], (dict, type(None)))


def test_cli_audit_list_filter_by_status(store_with_audit_rows, tmp_path):
    """`audit list --status block` で block 行のみ出る"""
    db_path = Path(store_with_audit_rows.db_path)
    rc, output = _run_audit(
        ["list", "--output", "json", "--status", "block"], db_path,
    )
    assert rc == 0
    parsed = json.loads(output)
    assert len(parsed) == 1
    assert parsed[0]["status"] == "block"


def test_cli_audit_list_empty_result(tmp_path):
    """audit_logs が空のとき (table) は明示メッセージが出る"""
    db_path = tmp_path / "empty.db"
    SQLiteStore(str(db_path))  # init only
    rc, output = _run_audit(["list", "--limit", "10"], db_path)
    assert rc == 0
    assert "該当行はありません" in output


def test_cli_audit_show_outputs_json_details(store_with_audit_rows, tmp_path):
    """`audit show <id>` で details_json を含む単一行 JSON が出る"""
    db_path = Path(store_with_audit_rows.db_path)
    rc, output = _run_audit(["show", "2"], db_path)
    assert rc == 0
    parsed = json.loads(output)
    assert parsed["id"] == 2
    assert parsed["status"] == "block"
    assert parsed["details"]["policy_hits"] == ["unknown_provider"]


def test_cli_audit_show_missing_id_returns_error(
    store_with_audit_rows, tmp_path,
):
    """存在しない id では rc=1 を返す"""
    db_path = Path(store_with_audit_rows.db_path)
    rc, _ = _run_audit(["show", "999"], db_path)
    assert rc == 1
