"""Service Status ページ ドメインクライアント

connection_status の結果を Notion ページにテーブル形式で書き出す。

設計方針:
- Service Status ページは「最新スナップショットだけを表示する」運用
- 既存の子ブロックを archive して、新しいテーブル＋見出しを append する
- 履歴が必要な場合は Notion 側のページバージョン履歴で追える前提
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient

logger = get_logger("integrations.notion_dashboard.service_status")


# 内部 status → Notion 表示用ラベル
_STATUS_DISPLAY: dict[str, str] = {
    "connected": "✅ Connected",
    "not_installed": "⚠️ Not Installed",
    "not_authenticated": "🔒 Not Authenticated",
    "timeout": "⏱️ Timeout",
    "unsupported": "ℹ️ Unsupported",
    "disabled": "🚫 Disabled",
    "unknown": "❓ Unknown",
}


class ServiceStatusPageClient:
    """Notion Service Status ページの内容を最新スナップショットで置き換える。"""

    def __init__(self, api: NotionAPIClient, page_id: str):
        if not page_id:
            raise ValueError("Service Status page_id は必須です")
        self._api = api
        self._page_id = page_id

    def replace_snapshot(self, services: list[dict[str, Any]]) -> None:
        """既存のブロックを archive し、最新スナップショットを書き込む。"""
        # 1. 既存子ブロックを取得して archive
        try:
            children = self._list_children()
            for block in children:
                self._archive_block(block.get("id"))
        except Exception as e:
            # 既存ブロック取得・削除失敗は append には進む（ページが汚れるが落とさない）
            logger.warning(
                f"Service Status ページの既存ブロック削除に失敗: {type(e).__name__}"
            )

        # 2. 新しいスナップショットブロックを append
        blocks = self._build_snapshot_blocks(services)
        self._api.append_block_children(self._page_id, blocks)

    def _list_children(self) -> list[dict[str, Any]]:
        # Notion API: GET /blocks/{block_id}/children
        try:
            response = self._api._request(
                "GET", f"/blocks/{self._page_id}/children"
            )
        except Exception:
            return []
        return list(response.get("results") or [])

    def _archive_block(self, block_id: str | None) -> None:
        if not block_id:
            return
        # PATCH /blocks/{block_id} { archived: true }
        try:
            self._api._request(
                "PATCH", f"/blocks/{block_id}", body={"archived": True}
            )
        except Exception as e:
            logger.debug(f"ブロック archive 失敗: id={block_id}, error={e}")

    @staticmethod
    def _build_snapshot_blocks(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Notion ブロック配列を組み立てる。

        ヘッダ（H2）+ 最終チェック時刻 + 各サービスのテキスト行（bulleted_list）。
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        blocks: list[dict[str, Any]] = [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "HOKUSAI Service Status"}}
                    ]
                },
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"最終チェック: {timestamp}"}}
                    ]
                },
            },
        ]

        for svc in services:
            label = svc.get("label") or svc.get("id") or "?"
            status = svc.get("status", "unknown")
            display = _STATUS_DISPLAY.get(status, status)
            note = svc.get("message") or ""
            text = f"{label}: {display}"
            if note:
                text += f" — {note}"
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [
                        {"type": "text", "text": {"content": text[:1900]}}
                    ]
                },
            })

        return blocks


def sync_service_status_to_notion(dispatcher) -> bool:
    """connection_status の結果を Notion Service Status ページに反映する。

    Web Dashboard の手動ボタン、または cron / launchd から呼ばれる想定。

    Args:
        dispatcher: NotionSyncDispatcher

    Returns:
        送信成功 True、enabled=False / page_id 未設定 / 送信失敗のいずれかで False
    """
    if not dispatcher.is_configured():
        return False

    from ..connection_status import get_all_statuses

    try:
        bundle = get_all_statuses(refresh=True)
        services = bundle.get("services") or []
    except Exception as e:
        logger.warning(f"connection_status 取得失敗: {type(e).__name__}")
        return False

    return dispatcher.dispatch(
        "service_status_checked",
        {"services": services},
    )
