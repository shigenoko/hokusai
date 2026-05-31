"""既存 workflow の state_json から durable テーブルを再構築する backfill。

v0.8–v0.10 で導入した durable テーブル（`review_issues` / `work_items` /
`workgraph_edges`）は drain hook 依存で **forward-only** にしか埋まらない
（dogfooding §11）。本モジュールは既存 workflow の `state_json` を読み、
`persist_*_payloads` と `collect_all_workflow_edges` を**再利用**して durable
テーブルを再構築する。決定的・SQLite-backed・既存純関数の合成のみ。

返す件数は **durable table の実 row 増分**（before/after の差）であり、
処理した payload 数ではない。`pending_work_items` には同一 `(workflow_id,
phase, title)` のイベント（upsert / claim / status_change / lease_release ×
retry）が重複して積まれており、durable table は `dedupe_key` で正規化する
ため、payload 数 ≫ 実 row 数 になる（dogfooding §12）。

制約: review_issues / work_items の backfill は state の `pending_review_issues`
/ `pending_work_items` が残っている場合のみ有効（drain で clear 済みの場合は
復元できない）。workgraph_edges は state（supersedes / has_pr / has_work_item）
から常に再構築できる。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 行数カウント用の十分大きな limit（list_* の取得上限を超える DB でも正確に
# unique key を数えるため。count_* は SQL COUNT を使うので影響しない）。
_COUNT_LIMIT = 1_000_000


def backfill_workflow(
    store: Any, workflow_id: str, state: dict[str, Any]
) -> dict[str, int]:
    """1 workflow の state から durable テーブルを再構築し**実 row 増分**を返す。

    1. `pending_review_issues` → `review_issues`
    2. `pending_work_items` → `work_items`
    3. state + durable から workgraph edge を合流して置換

    review/work を先に永続化してから edge を組むことで、`has_review_issue` /
    `resolved_by` edge が backfill した durable データを参照できる。

    Returns:
        {"review_issues": N, "work_items": N, "edges": N}
        review_issues / work_items は **新たに増えた row 数**（冪等な再実行では
        0）。edges は置換後の edge 数。
    """
    from .local_persistence import (
        persist_review_issue_payloads,
        persist_work_item_payloads,
    )
    from .workgraph_edges import (
        collect_all_workflow_edges,
        edge_to_replace_dict,
    )

    ri_before = store.count_review_issues()
    wi_before = store.count_work_items()
    persist_review_issue_payloads(
        store, state.get("pending_review_issues") or []
    )
    persist_work_item_payloads(
        store, state.get("pending_work_items") or []
    )
    ri_added = store.count_review_issues() - ri_before
    wi_added = store.count_work_items() - wi_before

    edges = collect_all_workflow_edges(
        workflow_id, state,
        work_items=store.list_work_items(
            workflow_id=workflow_id, limit=_COUNT_LIMIT
        ),
        review_issues=store.list_review_issues(
            workflow_id=workflow_id, limit=_COUNT_LIMIT
        ),
    )
    store.replace_workgraph_edges_for_workflow(
        workflow_id, [edge_to_replace_dict(e) for e in edges]
    )
    return {"review_issues": ri_added, "work_items": wi_added,
            "edges": len(edges)}


class _DedupeKeyCollector:
    """`persist_*_payloads` の `store` ダブル。upsert される dedupe_key を集める。

    persist 関数の guard / dedupe_key 解決ロジックをそのまま再利用して、
    「永続化されるであろう unique row のキー集合」を副作用なしで得るための
    軽量コレクタ（preview 用、DRY）。
    """

    def __init__(self) -> None:
        self.review_keys: set[str] = set()
        self.work_keys: set[str] = set()

    def upsert_review_issue(self, *, dedupe_key: str, **_: Any) -> None:
        self.review_keys.add(dedupe_key)

    def upsert_work_item(self, *, dedupe_key: str, **_: Any) -> None:
        self.work_keys.add(dedupe_key)


def preview_workflow(
    store: Any, workflow_id: str, state: dict[str, Any]
) -> dict[str, int]:
    """backfill の dry-run。SQLite を mutate せず**実 row 増分の見込み**を返す。

    `persist_*_payloads` を `_DedupeKeyCollector` に対して実行することで、実際
    の guard / dedupe_key 解決を再利用しつつ「永続化される unique key 集合」を
    得て、既存 row の key を引いて新規 row 数を算出する（実 backfill と同じ
    意味。dogfooding §12 の payload 数誤読を解消）。edge 件数は durable table の
    現状から算出する概算（実 backfill 後の has_review_issue / resolved_by を
    完全には反映しないが書き込みは行わない）。
    """
    from .local_persistence import (
        persist_review_issue_payloads,
        persist_work_item_payloads,
    )
    from .workgraph_edges import collect_all_workflow_edges

    collector = _DedupeKeyCollector()
    persist_review_issue_payloads(
        collector, state.get("pending_review_issues") or []
    )
    persist_work_item_payloads(
        collector, state.get("pending_work_items") or []
    )
    existing_ri = {
        r["dedupe_key"]
        for r in store.list_review_issues(limit=_COUNT_LIMIT)
    }
    existing_wi = {
        w["dedupe_key"]
        for w in store.list_work_items(limit=_COUNT_LIMIT)
    }
    edges = collect_all_workflow_edges(
        workflow_id, state,
        work_items=store.list_work_items(
            workflow_id=workflow_id, limit=_COUNT_LIMIT
        ),
        review_issues=store.list_review_issues(
            workflow_id=workflow_id, limit=_COUNT_LIMIT
        ),
    )
    return {
        "review_issues": len(collector.review_keys - existing_ri),
        "work_items": len(collector.work_keys - existing_wi),
        "edges": len(edges),
    }
