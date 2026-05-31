"""既存 workflow の state_json から durable テーブルを再構築する backfill。

v0.8–v0.10 で導入した durable テーブル（`review_issues` / `work_items` /
`workgraph_edges`）は drain hook 依存で **forward-only** にしか埋まらない
（dogfooding §11）。本モジュールは既存 workflow の `state_json` を読み、
`persist_*_payloads` と `collect_all_workflow_edges` を**再利用**して durable
テーブルを再構築する。決定的・SQLite-backed・既存純関数の合成のみ。

制約: review_issues / work_items の backfill は state の `pending_review_issues`
/ `pending_work_items` が残っている場合のみ有効（drain で clear 済みの場合は
復元できない）。workgraph_edges は state（supersedes / has_pr / has_work_item）
から常に再構築できる。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def backfill_workflow(
    store: Any, workflow_id: str, state: dict[str, Any]
) -> dict[str, int]:
    """1 workflow の state から durable テーブルを再構築し件数を返す。

    1. `pending_review_issues` → `review_issues`
    2. `pending_work_items` → `work_items`
    3. state + durable から workgraph edge を合流して置換

    review/work を先に永続化してから edge を組むことで、`has_review_issue` /
    `resolved_by` edge が backfill した durable データを参照できる。

    Returns:
        {"review_issues": N, "work_items": N, "edges": N}
    """
    from .local_persistence import (
        persist_review_issue_payloads,
        persist_work_item_payloads,
    )
    from .workgraph_edges import (
        collect_all_workflow_edges,
        edge_to_replace_dict,
    )

    ri = persist_review_issue_payloads(
        store, state.get("pending_review_issues") or []
    )
    wi = persist_work_item_payloads(
        store, state.get("pending_work_items") or []
    )
    edges = collect_all_workflow_edges(
        workflow_id, state,
        work_items=store.list_work_items(workflow_id=workflow_id),
        review_issues=store.list_review_issues(workflow_id=workflow_id),
    )
    store.replace_workgraph_edges_for_workflow(
        workflow_id, [edge_to_replace_dict(e) for e in edges]
    )
    return {"review_issues": ri, "work_items": wi, "edges": len(edges)}


def preview_workflow(
    store: Any, workflow_id: str, state: dict[str, Any]
) -> dict[str, int]:
    """backfill の dry-run。SQLite を mutate せず再構築されるであろう件数を返す。

    edge 件数は durable table の**現状**を読んで算出するため、実 backfill 後の
    has_review_issue / resolved_by を完全には反映しないが、概算 preview として
    十分（書き込みは行わない）。
    """
    from .workgraph_edges import collect_all_workflow_edges

    ri = [
        p for p in (state.get("pending_review_issues") or [])
        if isinstance(p, dict) and p.get("source") and p.get("message")
    ]
    wi = [
        p for p in (state.get("pending_work_items") or [])
        if isinstance(p, dict) and (p.get("dedupe_key") or p.get("title"))
    ]
    edges = collect_all_workflow_edges(
        workflow_id, state,
        work_items=store.list_work_items(workflow_id=workflow_id),
        review_issues=store.list_review_issues(workflow_id=workflow_id),
    )
    return {"review_issues": len(ri), "work_items": len(wi),
            "edges": len(edges)}
