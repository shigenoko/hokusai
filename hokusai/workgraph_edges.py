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

    # workflow -> has_work_item -> work_item
    # work item の node identity は title (Phase 4 plan と Phase 5 implement が
    # 同じ workflow_id + title を同一 identity として扱うのと整合。phase を
    # またいでも同じ work item を指す)。phase / status は metadata に持ち、
    # 再抽出時は最新値で上書きされる。
    #
    # ⚠ 既知の制約 (PR #146 Copilot Round 1): `pending_work_items` は Notion
    # dispatch 後に drain layer (workflow.py) が `[]` に clear して永続化する
    # ため、drain 済みの work item は state に残らない。よって `graph build` は
    # 「未 drain の work item のみ」を edge 化し、再 build 時 (clear→再抽出) は
    # 既存 has_work_item edge を失い得る。has_work_item edge は transient で
    # work item の完全な履歴を表さない。完全な履歴が要るなら、durable な
    # work-item イベント源 (SQLite への永続化) を前提とする後続スライスが必要。
    for wi in state.get("pending_work_items", []) or []:
        if not isinstance(wi, dict):
            continue
        title = wi.get("title")
        if not title:
            continue
        metadata = {
            k: wi.get(k)
            for k in ("phase", "status")
            if wi.get(k) is not None
        }
        _add(Edge(
            "workflow", workflow_id, "has_work_item", "work_item", str(title),
            metadata or None,
        ))

    return edges


def extract_durable_edges(
    workflow_id: str,
    *,
    work_items: list[dict[str, Any]],
    review_issues: list[dict[str, Any]],
    pr_urls: list[str],
) -> list[Edge]:
    """durable な SQLite table（work_items / review_issues）から edge を抽出する。

    第3スライスで永続化した durable データを使うため、drain 後も失われない
    （state-based の `has_work_item` が transient だった問題を解消する。Step 5
    第5スライス）。決定的・I/O なし（store から取得済みの list を受け取る）。

    抽出する edge:
    - `workflow -> has_work_item -> work_item` (dst_id=title。durable 版)
    - `workflow -> has_review_issue -> review_issue` (dst_id=dedupe_key)
    - `review_issue -> resolved_by -> pull_request`: status="resolved" の
      review issue を、その workflow が産んだ各 PR に結ぶ。これは
      **workflow 単位の関連付け**（特定 issue ↔ 特定 PR の厳密な対応では
      ない）で、解決済み指摘がどの PR を伴う workflow で片付いたかを辿る用途。

    Args:
        workflow_id: 抽出元 workflow。
        work_items: `store.list_work_items(workflow_id=...)` の結果。
        review_issues: `store.list_review_issues(workflow_id=...)` の結果。
        pr_urls: 当該 workflow の PR URL 群（state の pull_requests 由来）。

    Returns:
        Edge のリスト（重複なし・安定順序）。
    """
    edges: list[Edge] = []
    seen: set[tuple[str, ...]] = set()

    def _add(edge: Edge) -> None:
        key = (edge.src_type, edge.src_id, edge.edge_type,
               edge.dst_type, edge.dst_id)
        if key not in seen:
            seen.add(key)
            edges.append(edge)

    for wi in work_items or []:
        title = wi.get("title")
        if not title:
            continue
        meta = {
            k: wi.get(k) for k in ("phase", "status") if wi.get(k) is not None
        }
        _add(Edge(
            "workflow", workflow_id, "has_work_item", "work_item", str(title),
            meta or None,
        ))

    for ri in review_issues or []:
        dedupe_key = ri.get("dedupe_key")
        if not dedupe_key:
            continue
        meta = {
            k: ri.get(k)
            for k in ("source", "rule", "status")
            if ri.get(k) is not None
        }
        _add(Edge(
            "workflow", workflow_id, "has_review_issue", "review_issue",
            str(dedupe_key), meta or None,
        ))
        # 解決済み review issue → workflow の各 PR (resolved_by)
        if ri.get("status") == "resolved":
            for url in pr_urls or []:
                if not url:
                    continue
                _add(Edge(
                    "review_issue", str(dedupe_key), "resolved_by",
                    "pull_request", str(url), None,
                ))

    return edges


def collect_all_workflow_edges(
    workflow_id: str,
    state: dict[str, Any],
    *,
    work_items: list[dict[str, Any]],
    review_issues: list[dict[str, Any]],
) -> list[Edge]:
    """state-based + durable な edge を合流し 5-tuple で dedup した list を返す。

    `extract_edges_from_state(state)`（state 由来: supersedes / has_pr /
    has_work_item）と `extract_durable_edges(...)`（durable table 由来:
    has_work_item / has_review_issue / resolved_by）を合流する。`graph build`
    と `backfill` の双方が使う共通ロジック（dedup は先勝ち・安定順序）。
    """
    edges = list(extract_edges_from_state(state))
    pr_urls = [
        pr.get("url")
        for pr in (state.get("pull_requests") or [])
        if isinstance(pr, dict) and pr.get("url")
    ]
    edges.extend(extract_durable_edges(
        workflow_id,
        work_items=work_items,
        review_issues=review_issues,
        pr_urls=pr_urls,
    ))
    seen: set[tuple[str, ...]] = set()
    out: list[Edge] = []
    for e in edges:
        key = (e.src_type, e.src_id, e.edge_type, e.dst_type, e.dst_id)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def edge_to_replace_dict(edge: Edge) -> dict[str, Any]:
    """Edge を `replace_workgraph_edges_for_workflow` の入力 dict に変換する。"""
    return {
        "src_type": edge.src_type,
        "src_id": edge.src_id,
        "edge_type": edge.edge_type,
        "dst_type": edge.dst_type,
        "dst_id": edge.dst_id,
        "metadata": edge.metadata,
    }
