"""Workflows DB ドメインクライアント

HOKUSAI ワークフローの実行状態を Notion Workflows DB のページとして同期する。

設計方針:
- workflow_id → Notion page_id のマッピングは、Workflow ID プロパティで Notion 側を検索して取得する
- 新規ワークフロー: ページを作成
- 既存ワークフロー: ページを更新（プロパティのみ）
- 子ページ（Phase 2/3/4）の URL は別途 update で書き戻す
- イベント単位で payload を受け、内部で Notion プロパティへマッピング

Phase 2/3/4 の子ページ自体は既存の Notion MCP 経由（save_to_subpage_or_create）が正本。
本クライアントは DB 上の URL プロパティだけを更新する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient

logger = get_logger("integrations.notion_dashboard.workflows_db")


# ワークフローイベントの種別。同期 dispatcher が発行するイベント名と対応する。
EVENT_WORKFLOW_STARTED = "workflow_started"
EVENT_PHASE_CHANGED = "phase_changed"
EVENT_PHASE_ARTIFACT_LINKED = "phase_artifact_linked"
EVENT_PR_CREATED = "pr_created"
EVENT_TERMINAL_STATUS_CHANGED = "terminal_status_changed"


# 内部 status を Notion select 値にマッピング
_STATUS_LABELS: dict[str, str] = {
    "ready": "Ready",
    "running": "Running",
    "waiting_for_human": "Waiting for Human",
    "failed": "Failed",
    "done": "Done",
    "canceled": "Canceled",
}


class WorkflowsDBClient:
    """Notion Workflows DB へのページ作成・更新を担当する。"""

    def __init__(self, api: NotionAPIClient, database_id: str):
        if not database_id:
            raise ValueError("Workflows DB の database_id は必須です")
        self._api = api
        self._database_id = database_id

    def apply_event(self, event_type: str, payload: dict[str, Any]) -> dict:
        """同期イベントを受け、Notion DB に反映する。

        Args:
            event_type: イベント名
            payload: state 由来の辞書（workflow_id を含むこと）

        Returns:
            Notion から返された page オブジェクト

        Raises:
            ValueError: workflow_id が含まれない場合
            NotionAPIError / NotionRateLimitError: API 呼び出し失敗
        """
        workflow_id = payload.get("workflow_id")
        if not workflow_id:
            raise ValueError("payload に workflow_id が必要です")

        existing_page_id = self._find_page_id(workflow_id)
        properties = self._build_properties(event_type, payload)

        if existing_page_id is None:
            # 新規作成: Name / Workflow ID / Started At を最低限必須として埋める
            return self._api.create_page({
                "parent": {"database_id": self._database_id},
                "properties": properties,
            })

        return self._api.update_page(existing_page_id, {"properties": properties})

    def get_workflow_page_url(self, workflow_id: str) -> str | None:
        """workflow_id に対応する Notion ページ URL を返す。

        Slack 通知のディープリンク生成等で使用する。Notion API が返すページ URL を
        そのまま返すため、ワークスペースのドメインや ID 構造に依存しない。
        """
        try:
            response = self._api.query_database(
                self._database_id,
                filter_={
                    "property": "Workflow ID",
                    "rich_text": {"equals": workflow_id},
                },
            )
        except Exception as e:
            logger.debug(f"page URL 解決失敗: workflow_id={workflow_id}, error={e}")
            return None

        results = response.get("results") or []
        if not results:
            return None
        return results[0].get("url")

    def _find_page_id(self, workflow_id: str) -> str | None:
        """Workflow ID プロパティで Notion DB を検索し、page_id を返す。"""
        try:
            response = self._api.query_database(
                self._database_id,
                filter_={
                    "property": "Workflow ID",
                    "rich_text": {"equals": workflow_id},
                },
            )
        except Exception as e:
            logger.debug(f"Workflows DB 検索失敗: workflow_id={workflow_id}, error={e}")
            raise

        results = response.get("results") or []
        if not results:
            return None
        return results[0].get("id")

    @staticmethod
    def _build_properties(event_type: str, payload: dict[str, Any]) -> dict:
        """payload から Notion プロパティ辞書を構築する。

        必須/任意フィールドはイベントに応じて変える。未指定のフィールドはそもそも
        プロパティ辞書に入れず、Notion 側の既存値を温存する。
        """
        props: dict[str, Any] = {}

        if "task_title" in payload and payload["task_title"]:
            props["Name"] = _title(str(payload["task_title"]))

        if "workflow_id" in payload and payload["workflow_id"]:
            props["Workflow ID"] = _rich_text(str(payload["workflow_id"]))

        if "status" in payload and payload["status"]:
            label = _STATUS_LABELS.get(str(payload["status"]).lower(), str(payload["status"]))
            props["Status"] = {"select": {"name": label}}

        if "current_phase" in payload and payload["current_phase"] is not None:
            props["Current Phase"] = {"number": int(payload["current_phase"])}

        if "current_phase_name" in payload and payload["current_phase_name"]:
            props["Current Phase Name"] = _rich_text(str(payload["current_phase_name"]))

        if "waiting_reason" in payload and payload["waiting_reason"]:
            props["Waiting Reason"] = {"select": {"name": str(payload["waiting_reason"])}}

        if "next_action" in payload and payload["next_action"]:
            props["Next Action"] = _rich_text(str(payload["next_action"]))

        if "task_url" in payload and payload["task_url"]:
            # Workflows DB スキーマでは Name を title にしているため、task_url は
            # 別 url プロパティ（運用ルールで Task URL プロパティを設けるなら拡張）
            # 当面は Next Action / Name に含める形に留める
            pass

        if "gitlab_mr_url" in payload and payload["gitlab_mr_url"]:
            props["GitLab MR"] = {"url": str(payload["gitlab_mr_url"])}

        if "research_page_url" in payload and payload["research_page_url"]:
            props["Research Page"] = {"url": str(payload["research_page_url"])}

        if "design_page_url" in payload and payload["design_page_url"]:
            props["Design Page"] = {"url": str(payload["design_page_url"])}

        if "plan_page_url" in payload and payload["plan_page_url"]:
            props["Plan Page"] = {"url": str(payload["plan_page_url"])}

        if "started_at" in payload and payload["started_at"]:
            props["Started At"] = _date(str(payload["started_at"]))

        if "completed_at" in payload and payload["completed_at"]:
            props["Completed At"] = _date(str(payload["completed_at"]))

        if "error_summary" in payload and payload["error_summary"]:
            props["Error Summary"] = _rich_text(str(payload["error_summary"]))

        # Last Updated は常に書き戻す
        props["Last Updated"] = _date(payload.get("last_updated") or datetime.now().isoformat())

        # Last Sync は同期成功時に書き戻す（dispatcher が成功時のみ含めて渡す）
        if "last_sync" in payload and payload["last_sync"]:
            props["Last Sync"] = _date(str(payload["last_sync"]))

        # Sync Errors: 失敗滞留があればサマリ文字列を、なければ空文字でクリア
        if "sync_errors" in payload:
            summary = str(payload["sync_errors"] or "")
            props["Sync Errors"] = _rich_text(summary)

        return props


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _rich_text(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def _date(iso_string: str) -> dict:
    return {"date": {"start": iso_string}}
