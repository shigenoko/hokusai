"""既存 workflow からの durable テーブル backfill のテスト。

backfill_workflow / preview_workflow（state → durable 再構築）と CLI
`hokusai backfill` を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.backfill import backfill_workflow, preview_workflow
from hokusai.persistence import SQLiteStore


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / "wf.db")


def _state_with_pending():
    return {
        "workflow_id": "wf-1",
        "pull_requests": [{"url": "https://gh/o/r/pull/9", "number": 9}],
        "supersedes_workflow_id": "wf-0",
        "pending_review_issues": [
            {"dedupe_key": "k1", "workflow_id": "wf-1",
             "source": "verification_failure", "message": "boom",
             "rule": "npm test", "status": "open"},
        ],
        "pending_work_items": [
            {"workflow_id": "wf-1", "title": "認証実装", "phase": 4,
             "status": "pending"},
        ],
    }


def test_backfill_workflow_reconstructs_durable_tables(store):
    state = _state_with_pending()
    counts = backfill_workflow(store, "wf-1", state)
    assert counts["review_issues"] == 1
    assert counts["work_items"] == 1
    # supersedes + has_pr + has_work_item(state & durable dedup) +
    # has_review_issue
    assert counts["edges"] >= 4

    # durable テーブルに永続化された
    assert len(store.list_review_issues(workflow_id="wf-1")) == 1
    assert len(store.list_work_items(workflow_id="wf-1")) == 1
    types = {e["edge_type"] for e in store.list_workgraph_edges()}
    assert {"supersedes", "has_pr", "has_work_item",
            "has_review_issue"} <= types


def test_backfill_workflow_idempotent(store):
    state = _state_with_pending()
    backfill_workflow(store, "wf-1", state)
    backfill_workflow(store, "wf-1", state)  # 2 回目でも重複しない
    assert len(store.list_review_issues(workflow_id="wf-1")) == 1
    assert len(store.list_work_items(workflow_id="wf-1")) == 1


def test_preview_workflow_does_not_mutate(store):
    state = _state_with_pending()
    counts = preview_workflow(store, "wf-1", state)
    assert counts["review_issues"] == 1
    assert counts["work_items"] == 1
    # preview は書き込まない
    assert store.list_review_issues(workflow_id="wf-1") == []
    assert store.list_work_items(workflow_id="wf-1") == []
    assert store.list_workgraph_edges() == []


def test_backfill_empty_state(store):
    counts = backfill_workflow(store, "wf-1", {"workflow_id": "wf-1"})
    assert counts == {"review_issues": 0, "work_items": 0, "edges": 0}


def test_backfill_counts_unique_rows_not_payloads(store):
    """件数は処理 payload 数でなく実 row 増分（重複 identity は 1 行）。

    dogfooding §12: pending_work_items に同一 (workflow_id, phase, title) の
    イベントが ~10倍重複して積まれるが、durable table は dedupe_key で正規化
    するので、backfill 件数は unique row 数を返すべき。"""
    # 同一 identity の work item を 5 payload（status だけ違う lifecycle）
    state = {
        "workflow_id": "wf-1",
        "pending_work_items": [
            {"workflow_id": "wf-1", "title": "認証", "phase": 4,
             "status": s}
            for s in ("pending", "in_progress", "done", "done", "done")
        ],
        "pending_review_issues": [],
    }
    counts = backfill_workflow(store, "wf-1", state)
    # 5 payload → 実 row 1（dedupe_key 同一）
    assert counts["work_items"] == 1
    assert len(store.list_work_items(workflow_id="wf-1")) == 1

    # preview も同じ semantics（unique row 数）
    counts2 = preview_workflow(store, "wf-2", {
        "workflow_id": "wf-2",
        "pending_work_items": [
            {"workflow_id": "wf-2", "title": "x", "phase": 4, "status": s}
            for s in ("a", "b", "c")
        ],
    })
    assert counts2["work_items"] == 1  # 3 payload → 1 unique


def test_list_limit_none_returns_all_rows(store):
    """limit=None で LIMIT 句なしの全件取得（hard cap 回避、PR #159 Round 1）。"""
    for i in range(250):  # default limit(200) を超える件数
        store.upsert_work_item(dedupe_key=f"d{i}", workflow_id="wf-1",
                               title=f"t{i}", phase=4)
    assert len(store.list_work_items(workflow_id="wf-1")) == 200  # default cap
    assert len(store.list_work_items(workflow_id="wf-1", limit=None)) == 250


def test_count_scoped_by_workflow(store):
    """backfill の件数は workflow_id で絞られ、他 workflow の row を含まない
    (PR #159 Copilot Round 1)。"""
    # 別 workflow に既存 row があっても、対象 workflow の増分のみ数える
    store.upsert_review_issue(dedupe_key="other", workflow_id="wf-other",
                              source="x", message="m", status="open")
    state = {
        "workflow_id": "wf-1",
        "pending_review_issues": [
            {"dedupe_key": "k1", "workflow_id": "wf-1", "source": "x",
             "message": "m"},
        ],
    }
    counts = backfill_workflow(store, "wf-1", state)
    assert counts["review_issues"] == 1  # wf-other は数えない


def test_backfill_idempotent_rerun_adds_zero(store):
    """冪等な再実行は実 row 増分 0 を返す（§12 件数=増分の意味）。"""
    state = _state_with_pending()
    first = backfill_workflow(store, "wf-1", state)
    assert first["review_issues"] == 1
    second = backfill_workflow(store, "wf-1", state)
    assert second["review_issues"] == 0  # 既存なので増分なし
    assert second["work_items"] == 0


# --- CLI ---------------------------------------------------------------


def test_handle_backfill_all_workflows(store, capsys):
    import argparse
    import json

    from hokusai.cli_main import _handle_backfill

    store.save_workflow("wf-1", _state_with_pending())

    class _Cfg:
        database_path = store.db_path

    rc = _handle_backfill(
        argparse.Namespace(workflow_id=None, dry_run=False, output="json"),
        _Cfg(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["dry_run"] is False
    assert payload["totals"]["review_issues"] == 1
    assert payload["totals"]["work_items"] == 1
    # 実際に永続化された
    assert len(store.list_review_issues()) == 1


def test_handle_backfill_covers_completed_workflows(store, capsys):
    """id 省略時、completed (phase>=10) の workflow も対象になる
    (PR #157 Copilot Round 1: list_active_workflows は phase<10 のみで漏れる)。"""
    import argparse
    import json

    from hokusai.cli_main import _handle_backfill

    # completed workflow (phase=10) に pending データを持たせる
    # (state 内部の workflow_id と保存 key を "wf-done" で一致させる)
    st = {
        "workflow_id": "wf-done", "current_phase": 10,
        "pending_review_issues": [
            {"dedupe_key": "kd", "workflow_id": "wf-done",
             "source": "verification_failure", "message": "boom"},
        ],
        "pending_work_items": [
            {"workflow_id": "wf-done", "title": "x", "phase": 4},
        ],
    }
    store.save_workflow("wf-done", st)

    class _Cfg:
        database_path = store.db_path

    rc = _handle_backfill(
        argparse.Namespace(workflow_id=None, dry_run=False, output="json"),
        _Cfg(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    # completed workflow が対象に含まれ backfill された
    assert payload["workflows"] == 1
    assert payload["totals"]["review_issues"] == 1
    assert len(store.list_review_issues(workflow_id="wf-done")) == 1


def test_list_all_workflow_ids_includes_completed(store):
    st = _state_with_pending()
    st["current_phase"] = 10
    store.save_workflow("wf-done", st)
    st2 = _state_with_pending()
    st2["workflow_id"] = "wf-active"
    st2["current_phase"] = 3
    store.save_workflow("wf-active", st2)
    ids = set(store.list_all_workflow_ids())
    assert ids == {"wf-done", "wf-active"}
    # list_active_workflows は active のみ（対比）
    active = {w["workflow_id"] for w in store.list_active_workflows()}
    assert active == {"wf-active"}


def test_handle_backfill_dry_run(store, capsys):
    import argparse
    import json

    from hokusai.cli_main import _handle_backfill

    store.save_workflow("wf-1", _state_with_pending())

    class _Cfg:
        database_path = store.db_path

    rc = _handle_backfill(
        argparse.Namespace(workflow_id="wf-1", dry_run=True, output="json"),
        _Cfg(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["dry_run"] is True
    assert payload["totals"]["review_issues"] == 1
    # dry-run は書き込まない
    assert store.list_review_issues() == []


def test_handle_backfill_missing_workflow(store, capsys):
    import argparse

    from hokusai.cli_main import _handle_backfill

    class _Cfg:
        database_path = store.db_path

    rc = _handle_backfill(
        argparse.Namespace(workflow_id="nope", dry_run=False, output="text"),
        _Cfg(),
    )
    assert rc == 1
    assert "見つかりません" in capsys.readouterr().err
