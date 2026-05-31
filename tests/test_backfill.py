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
