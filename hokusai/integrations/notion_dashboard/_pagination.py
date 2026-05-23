"""Notion DB ページネーション共通 helper（Issue #54 / Workgraph 完成）

`list_X_for_workflow` 系の prime context fetcher で `has_more` / `next_cursor`
ループ + truncation warning + 部分結果保持を 4 つの client にそれぞれ実装すると
SonarCloud duplication で重複行が検出されたため、本 module に集約する
（`_property_pruning.submit_with_property_pruning` と同じく共通 helper 方針）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

# 1 ページの最大サイズ（Notion API v1 仕様で 100 が上限）
_PAGE_SIZE = 100


def paginate_query(
    *,
    api: Any,
    database_id: str,
    filter_: dict,
    label: str,
    max_pages: int,
    logger: logging.Logger,
    page_filter: Callable[[list[dict]], list[dict]] | None = None,
) -> list[dict]:
    """Notion DB query を `has_more` / `next_cursor` でページめくりする共通 loop。

    Args:
        api: NotionAPIClient 互換オブジェクト（`query_database` メソッドを持つ）。
        database_id: 対象 DB の ID。
        filter_: Notion filter dict（呼び出し側で組み立て済み）。
        label: ログ表示用のメソッド名（例: `"list_open_review_issues_for_workflow"`）。
        max_pages: 安全上限。上限到達時は warning ログを出す。
        logger: 呼び出し元 client の logger（メソッド固有の logger 名で出る）。
        page_filter: 各ページの results に client-side filter をかける callback。
            None なら page の results をそのまま結果に append。指定時は
            `filtered = page_filter(page_results)` の返り値を append する。

    Returns:
        収集された Notion page dict のリスト。API 失敗時は取得済み部分結果を
        保持して返す（prime 注入で全消失より部分提供を優先する設計）。
    """
    results: list[dict] = []
    start_cursor: str | None = None
    truncated = False
    for page_idx in range(max_pages):
        try:
            response = api.query_database(
                database_id,
                filter_=filter_,
                start_cursor=start_cursor,
                page_size=_PAGE_SIZE,
            )
        except Exception as e:
            logger.warning(
                "%s 失敗（部分結果 %d 件で続行）: %s",
                label, len(results), e,
            )
            return results
        page_results = response.get("results") or []
        if page_filter is not None:
            page_results = page_filter(page_results)
        results.extend(page_results)
        if not response.get("has_more"):
            break
        start_cursor = response.get("next_cursor")
        if not start_cursor:
            break
        if page_idx + 1 >= max_pages:
            truncated = True
            break

    if truncated:
        logger.warning(
            "%s が max_pages=%d で打ち切られました（取得済み %d 件で返却）",
            label, max_pages, len(results),
        )
    return results
