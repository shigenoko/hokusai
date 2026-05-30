"""Local Workgraph Edges の決定的 extractor (Step 5 第1スライス)

HOKUSAI の Workgraph は Notion 上の human governance view としては強いが、
ローカルに graph query できる形が薄い。GBrain の typed edge 発想を借りて、
SQLite に軽量な edge table を持ち、workflow state / PR metadata から
**決定的に** (LLM なし) edge を抽出する
(docs/roadmap-gbrain-inspirations.md §P1 / Step 5)。

第1スライスのスコープ:
- ローカル state のみから抽出できる 2 種の edge type:
  - workflow -> supersedes -> workflow (state["supersedes_workflow_id"])
  - workflow -> has_pr -> pull_request  (state["pull_requests"][].url)
- Notion DB relation / review comment 由来の edge (resolved_by / duplicates /
  touches_file 等) や recurring review issue 検出は後続スライス。

抽出は純関数 `extract_edges_from_state(state)` で行い、I/O は持たない
(SQLite への upsert は CLI / 呼び出し側の責務)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Edge:
    """1 本の typed edge (有向)。

    Attributes:
        src_type / src_id: 起点ノードの種別と ID
        edge_type: 関係の種別 (supersedes / has_pr / ...)
        dst_type / dst_id: 終点ノードの種別と ID
        metadata: 付随情報 (PR 番号 / repo 名など、任意)
    """

    src_type: str
    src_id: str
    edge_type: str
    dst_type: str
    dst_id: str
    metadata: dict[str, Any] | None = field(default=None)

    # frozen=True は __hash__ を生成するが metadata(dict) は hash 不可能なので
    # 明示的に unhashable 化して set/dict key 投入時の latent TypeError を防ぐ。
    __hash__ = None


def extract_edges_from_state(state: dict[str, Any]) -> list[Edge]:
    """workflow state からローカル決定的に edge を抽出する。

    Notion / LLM / network には一切触れない。state に必要キーが無い場合は
    その edge をスキップする (best-effort、欠損は単に「その関係なし」とみなす)。

    Returns:
        抽出した Edge のリスト (重複なし、安定順序)。
    """
    edges: list[Edge] = []
    seen: set[tuple[str, ...]] = set()

    workflow_id = state.get("workflow_id")
    if not workflow_id:
        # 起点 workflow が無ければ何も抽出できない
        return edges

    def _add(edge: Edge) -> None:
        key = (
            edge.src_type, edge.src_id, edge.edge_type,
            edge.dst_type, edge.dst_id,
        )
        if key not in seen:
            seen.add(key)
            edges.append(edge)

    # workflow -> supersedes -> workflow
    superseded = state.get("supersedes_workflow_id")
    if superseded:
        _add(Edge(
            "workflow", workflow_id, "supersedes", "workflow", str(superseded),
        ))

    # workflow -> has_pr -> pull_request
    for pr in state.get("pull_requests", []) or []:
        if not isinstance(pr, dict):
            continue
        url = pr.get("url")
        if not url:
            continue
        metadata = {
            k: pr.get(k)
            for k in ("number", "repo_name", "github_status")
            if pr.get(k) is not None
        }
        _add(Edge(
            "workflow", workflow_id, "has_pr", "pull_request", str(url),
            metadata or None,
        ))

    return edges
