"""Notion DB client 共通の property_not_found pruning ヘルパー

review_issues_db / work_items_db / workflow_gates_db で同形の retry ループを
持っていたが、PR #45 で Workflow Gates DB を追加した際に新規 duplicated
lines density が 3.3% を超えたため、本モジュールに集約した。

**現状の利用状況**:
- `workflow_gates_db.py`: 移行済（PR #45）
- `work_items_db.py` / `review_issues_db.py`: 従来のクラスメソッド実装のまま。
  既存テストの安定性を優先し、本 PR では新規追加分の Workflow Gates DB のみを
  本 helper 経由に切り替えた（PR #45 Copilot 2 回目指摘）。将来同種の DB
  client を追加する場合は本 helper を使うことで重複検出を回避できる。

Notion 側でプロパティ自体が存在しない（property_not_found）場合のみが対象。
select option が存在しないケースは Notion API が自動 option 作成するので
別ハンドリング不要（PR #37 Copilot 6 回目指摘）。
"""

from __future__ import annotations

from ...logging_config import get_logger
from .client import NotionAPIClient, NotionAPIError
from .workflows_db import (
    extract_missing_property,
    is_property_not_found,
)

logger = get_logger("integrations.notion_dashboard._property_pruning")


def submit_with_property_pruning(
    *,
    api: NotionAPIClient,
    database_id: str,
    existing_page_id: str | None,
    properties: dict,
    db_label: str,
    max_attempts: int = 6,
) -> dict:
    """create / update を試行し、property_not_found なら原因プロパティを除去して再試行。

    Args:
        api: NotionAPIClient
        database_id: 対象 DB の id（新規 create_page の parent に渡す）
        existing_page_id: 既存 page id（None なら create、それ以外は update）
        properties: 書き込む properties（中身は本関数内で mutate して再試行）
        db_label: ログ用の DB ラベル（例: "Work Items DB"）
        max_attempts: 最大リトライ回数（既定 6）

    Returns:
        Notion API レスポンス（create_page / update_page の戻り値）
    """
    attempts = 0
    current_props = dict(properties)
    while True:
        attempts += 1
        try:
            return _create_or_update(
                api, database_id, existing_page_id, current_props
            )
        except NotionAPIError as exc:
            if not is_property_not_found(exc):
                raise
            _prune_missing_or_raise(
                exc, current_props, attempts, max_attempts, db_label=db_label
            )


def _create_or_update(
    api: NotionAPIClient,
    database_id: str,
    existing_page_id: str | None,
    current_props: dict,
) -> dict:
    """既存 page id の有無で create / update を切り替える単純な分岐。"""
    if existing_page_id is None:
        return api.create_page({
            "parent": {"database_id": database_id},
            "properties": current_props,
        })
    return api.update_page(
        existing_page_id, {"properties": current_props}
    )


def _prune_missing_or_raise(
    exc: NotionAPIError,
    current_props: dict,
    attempts: int,
    max_attempts: int,
    *,
    db_label: str,
) -> None:
    """property_not_found エラーに対応して current_props から該当プロパティを除外。

    以下のいずれかで例外を伝播させる:
    - リトライ上限に到達
    - メッセージから対象プロパティを特定できない
    - 除外後に残プロパティが 0 になった

    いずれでもなければ current_props を mutate して return（呼び出し側ループが
    次の attempt を実行する）。
    """
    if attempts >= max_attempts:
        logger.warning(
            "%s: property_not_found リトライ上限に到達: 残プロパティ数=%d",
            db_label, len(current_props),
        )
        raise exc
    missing = extract_missing_property(exc.message, current_props)
    if missing is None:
        logger.warning(
            "%s: property_not_found 検知だが対象プロパティを特定できず: %s",
            db_label, exc.message[:200],
        )
        raise exc
    logger.info(
        "%s に '%s' プロパティが存在しないため除外して再試行",
        db_label, missing,
    )
    current_props.pop(missing, None)
    if not current_props:
        logger.warning(
            "%s: 除外後にプロパティが空になったため処理を中断", db_label,
        )
        raise exc
