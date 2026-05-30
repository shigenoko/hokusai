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
