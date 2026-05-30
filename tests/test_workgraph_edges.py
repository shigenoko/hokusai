"""Local Workgraph Edges (Step 5 第1スライス) のテスト。

決定的 extractor (extract_edges_from_state) と SQLiteStore の edge 永続化
(upsert / list / clear / 冪等性 / cascade-delete) を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.persistence import SQLiteStore
from hokusai.workgraph_edges import Edge, extract_edges_from_state

# --- extractor (純関数) --------------------------------------------------


def test_extract_supersedes_edge():
    state = {
        "workflow_id": "wf-new",
        "supersedes_workflow_id": "wf-old",
        "pull_requests": [],
    }
    edges = extract_edges_from_state(state)
    assert edges == [
        Edge("workflow", "wf-new", "supersedes", "workflow", "wf-old")
    ]


def test_extract_has_pr_edges_with_metadata():
    state = {
        "workflow_id": "wf-1",
        "pull_requests": [
            {
                "url": "https://github.com/o/r/pull/12",
                "number": 12,
                "repo_name": "Backend",
                "github_status": "open",
            }
        ],
    }
    edges = extract_edges_from_state(state)
    assert len(edges) == 1
    e = edges[0]
    assert e.src_id == "wf-1"
    assert e.edge_type == "has_pr"
    assert e.dst_id == "https://github.com/o/r/pull/12"
    assert e.metadata == {
        "number": 12,
        "repo_name": "Backend",
        "github_status": "open",
    }


def test_extract_skips_pr_without_url():
    state = {
        "workflow_id": "wf-1",
        "pull_requests": [{"number": 5}, {"url": ""}, "not-a-dict"],
    }
    assert extract_edges_from_state(state) == []


def test_extract_no_workflow_id_returns_empty():
    assert extract_edges_from_state({"supersedes_workflow_id": "x"}) == []


def test_extract_is_deterministic_and_dedup():
    state = {
        "workflow_id": "wf-1",
        "supersedes_workflow_id": "wf-0",
        "pull_requests": [
            {"url": "u1", "number": 1},
            {"url": "u1", "number": 1},  # 重複 → 1 本にまとまる
        ],
    }
    first = extract_edges_from_state(state)
    second = extract_edges_from_state(state)
    assert first == second  # 決定的
    # supersedes 1 + has_pr 1 (重複排除)
    assert len(first) == 2


def test_edge_is_unhashable():
    e = Edge("workflow", "a", "has_pr", "pull_request", "u", {"x": 1})
    with pytest.raises(TypeError):
        hash(e)


# --- SQLiteStore 永続化 --------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / "wf.db")


def test_upsert_and_list_edge(store):
    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-1", edge_type="supersedes",
        dst_type="workflow", dst_id="wf-0", workflow_id="wf-1",
    )
    rows = store.list_workgraph_edges()
    assert len(rows) == 1
    r = rows[0]
    assert r["src_id"] == "wf-1"
    assert r["edge_type"] == "supersedes"
    assert r["dst_id"] == "wf-0"
    assert r["workflow_id"] == "wf-1"


def test_upsert_is_idempotent(store):
    for _ in range(3):
        store.upsert_workgraph_edge(
            src_type="workflow", src_id="wf-1", edge_type="has_pr",
            dst_type="pull_request", dst_id="u1", workflow_id="wf-1",
            metadata={"number": 1},
        )
    rows = store.list_workgraph_edges()
    # UNIQUE 制約で 1 本のまま
    assert len(rows) == 1
    assert rows[0]["metadata"] == {"number": 1}


def test_upsert_updates_metadata_on_conflict(store):
    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-1", edge_type="has_pr",
        dst_type="pull_request", dst_id="u1", metadata={"github_status": "open"},
    )
    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-1", edge_type="has_pr",
        dst_type="pull_request", dst_id="u1", metadata={"github_status": "merged"},
    )
    rows = store.list_workgraph_edges()
    assert len(rows) == 1
    assert rows[0]["metadata"] == {"github_status": "merged"}


def test_list_filters(store):
    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-1", edge_type="supersedes",
        dst_type="workflow", dst_id="wf-0", workflow_id="wf-1",
    )
    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-1", edge_type="has_pr",
        dst_type="pull_request", dst_id="u1", workflow_id="wf-1",
    )
    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-2", edge_type="has_pr",
        dst_type="pull_request", dst_id="u2", workflow_id="wf-2",
    )
    assert len(store.list_workgraph_edges(edge_type="has_pr")) == 2
    assert len(store.list_workgraph_edges(workflow_id="wf-1")) == 2
    assert len(store.list_workgraph_edges(
        workflow_id="wf-1", edge_type="has_pr")) == 1


def test_list_rejects_nonpositive_limit(store):
    with pytest.raises(ValueError, match="1 以上"):
        store.list_workgraph_edges(limit=0)


def test_workgraph_edges_indexes_exist(store):
    """公開フィルタ用の index が張られている (PR #144 Copilot Round 2)。

    workflow_id / edge_type での list / clear / GC が full-scan しないよう
    index を確認する。
    """
    with store._connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='workgraph_edges'"
            ).fetchall()
        }
    assert "idx_workgraph_edges_workflow" in names
    assert "idx_workgraph_edges_edge_type" in names


def test_clear_edges_for_workflow(store):
    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-1", edge_type="has_pr",
        dst_type="pull_request", dst_id="u1", workflow_id="wf-1",
    )
    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-2", edge_type="has_pr",
        dst_type="pull_request", dst_id="u2", workflow_id="wf-2",
    )
    deleted = store.clear_workgraph_edges_for_workflow("wf-1")
    assert deleted == 1
    remaining = store.list_workgraph_edges()
    assert [r["workflow_id"] for r in remaining] == ["wf-2"]


def test_workgraph_edges_cascade_on_workflow_gc(tmp_path):
    """delete_old_completed_workflows で workgraph_edges も cascade される。

    `_WORKFLOW_DEPENDENT_TABLES` に workgraph_edges を追加したので、completed
    workflow の cleanup 時に edge が孤児化しない (PR #144 Copilot Round 1)。
    """
    from datetime import datetime, timedelta

    store = SQLiteStore(tmp_path / "workflow.db")

    # 古い completed workflow (current_phase>=10) を直接書き込む
    old_ts = (datetime.now() - timedelta(days=120)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO workflows "
            "(workflow_id, task_url, task_title, branch_name, "
            "current_phase, state_json, created_at, updated_at, profile_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wf-old", "https://example/1", "old task", "feat/old",
             10, "{}", old_ts, old_ts, None),
        )
        conn.commit()

    store.upsert_workgraph_edge(
        src_type="workflow", src_id="wf-old", edge_type="has_pr",
        dst_type="pull_request", dst_id="u-orphan", workflow_id="wf-old",
    )
    assert len(store.list_workgraph_edges(workflow_id="wf-old")) == 1

    counts = store.delete_old_completed_workflows(retention_days=90)

    # cascade で edge も消え、削除件数辞書にも計上される
    assert counts["workflows"] == 1
    assert counts["workgraph_edges"] == 1
    assert store.list_workgraph_edges(workflow_id="wf-old") == []


# --- CLI: graph build の --dry-run 抑止 (PR #144 Copilot Round 1) ---------


def test_graph_build_dry_run_does_not_mutate(tmp_path, capsys):
    """--dry-run 時は preview のみ・SQLite を mutate しない。"""
    import argparse

    from hokusai.cli_main import _handle_graph

    db = tmp_path / "wf.db"
    store = SQLiteStore(db)
    # PR を持つ workflow を保存（has_pr edge が抽出されるはず）
    store.save_workflow(
        "wf-1",
        {
            "workflow_id": "wf-1",
            "task_url": "u",
            "task_title": "t",
            "current_phase": 7,
            "pull_requests": [{"url": "https://gh/o/r/pull/9", "number": 9}],
        },
    )

    class _Cfg:
        database_path = db

    args = argparse.Namespace(
        graph_subcommand="build", workflow_id="wf-1", dry_run=True
    )
    rc = _handle_graph(args, _Cfg())
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out
    assert "has_pr" in out
    # SQLite には書き込まれていない
    assert store.list_workgraph_edges() == []
