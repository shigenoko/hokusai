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

import re
from datetime import datetime
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient, NotionAPIError

logger = get_logger("integrations.notion_dashboard.workflows_db")

# Notion API が property_not_found エラーを返す際のメッセージから、対象プロパティ名を
# 抽出するための正規表現。Notion のメッセージ例:
#   "<NAME> is not a property that exists. ..."
#   "Could not find property with name or id: \"<NAME>\". ..."
# プロパティ名は空白を含み得る（例: "Design Status"）ため、prefix パターンは最短一致で
# "is not a property" の直前まで全部キャプチャする。
_PROPERTY_NAME_PATTERN_QUOTED = re.compile(r'"([^"]+)"')
_PROPERTY_NAME_PATTERN_PREFIX = re.compile(r"^(.+?)\s+is not a property", re.IGNORECASE)


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

        Note:
            Notion DB 側に該当プロパティが存在しない場合 (property_not_found) は、
            該当プロパティを除去して同期を再試行する。最大 6 回まで試行
            （= 初回 1 + リトライ 5）。これにより、Workflows DB スキーマが古い環境
            （Figma/Miro 系プロパティ未追加など）でも、存在するプロパティのみで
            同期が進む。
        """
        workflow_id = payload.get("workflow_id")
        if not workflow_id:
            raise ValueError("payload に workflow_id が必要です")

        existing_page_id = self._find_page_id(workflow_id)
        properties = self._build_properties(event_type, payload)
        return self._submit_with_property_pruning(existing_page_id, properties)

    def _submit_with_property_pruning(
        self,
        existing_page_id: str | None,
        properties: dict,
        max_attempts: int = 6,
    ) -> dict:
        """create / update を試行し、property_not_found なら原因プロパティを除去して再試行。

        Notion DB スキーマの差異（プロパティが追加されていない環境）を吸収するため、
        エラーから推定される原因プロパティをペイロードから外して同期を継続させる。
        無限ループ回避のために最大試行回数を持つ。
        """
        attempts = 0
        current_props = dict(properties)
        while True:
            attempts += 1
            try:
                if existing_page_id is None:
                    return self._api.create_page({
                        "parent": {"database_id": self._database_id},
                        "properties": current_props,
                    })
                return self._api.update_page(
                    existing_page_id, {"properties": current_props}
                )
            except NotionAPIError as exc:
                if not _is_property_not_found(exc):
                    raise
                if attempts >= max_attempts:
                    logger.warning(
                        "property_not_found リトライ上限に到達: 残プロパティ数=%d",
                        len(current_props),
                    )
                    raise
                missing = _extract_missing_property(exc.message, current_props)
                if missing is None:
                    # メッセージから推定できなかった場合は安全のため打ち切る
                    logger.warning(
                        "property_not_found 検知だが対象プロパティを特定できず: %s",
                        exc.message[:200],
                    )
                    raise
                logger.info(
                    "Workflows DB に '%s' プロパティが存在しないため除外して再試行",
                    missing,
                )
                current_props.pop(missing, None)
                if not current_props:
                    logger.warning("除外後にプロパティが空になったため処理を中断")
                    raise

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

        # Figma / Miro 連携プロパティ。DB 側に存在しない場合は Notion 側で
        # property_not_found となるため、空値はスキップして送らない。
        if "miro_url" in payload and payload["miro_url"]:
            props["Miro URL"] = {"url": str(payload["miro_url"])}
        if "figma_url" in payload and payload["figma_url"]:
            props["Figma URL"] = {"url": str(payload["figma_url"])}
        if "design_integration_status" in payload and payload["design_integration_status"]:
            props["Design Status"] = {
                "select": {"name": str(payload["design_integration_status"])}
            }
        if "design_review_required" in payload and isinstance(
            payload["design_review_required"], bool
        ):
            props["Design Review Required"] = {
                "checkbox": bool(payload["design_review_required"])
            }
        if "design_review_result" in payload and payload["design_review_result"]:
            props["Design Review Result"] = {
                "select": {"name": str(payload["design_review_result"])}
            }
        if "miro_last_synced_at" in payload and payload["miro_last_synced_at"]:
            props["Miro Last Synced At"] = _date(str(payload["miro_last_synced_at"]))
        if "figma_last_synced_at" in payload and payload["figma_last_synced_at"]:
            props["Figma Last Synced At"] = _date(str(payload["figma_last_synced_at"]))
        if "design_notes" in payload and payload["design_notes"]:
            props["Design Notes"] = _rich_text(str(payload["design_notes"])[:2000])

        return props


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _rich_text(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def _date(iso_string: str) -> dict:
    return {"date": {"start": iso_string}}


def _is_property_not_found(exc: NotionAPIError) -> bool:
    """Notion API エラーが property_not_found（プロパティ欠落）由来か判定する。

    判定条件は AND で 3 つ:
    1. HTTP status が 400（Bad Request）
    2. error code が "validation_error"
    3. メッセージに欠落を示す文言（"not a property" / "could not find property"）

    文字列マッチだけだと、別 code の 4xx で文言が偶然含まれた場合に誤判定する。
    また `validation_error` 全般を property_not_found 扱いすると、型不一致や
    不正な値（例: `body.properties.X.url` が壊れている等）まで pruning 対象に
    なり、実在するプロパティが除去されて誤って同期成功扱いされるリスクがある。
    そのため status + code + 文言の 3 段で絞り込む。
    """
    if exc.status != 400 or exc.code != "validation_error":
        return False
    msg = exc.message.lower()
    return ("not a property" in msg) or ("could not find property" in msg)


def _extract_missing_property(message: str, current_props: dict) -> str | None:
    """エラーメッセージから対象プロパティ名を抽出する。

    Notion のメッセージは表記ゆれ（quote 有無、空白を含む名前、大小文字差）が
    あり得るため、以下の順で頑健に試行する:

    1. ダブルクォートで囲まれた名前（"Design Status" 等）— current_props と一致した時のみ
    2. `<name> is not a property` の prefix パターン（最短一致、空白含む名前を許容）
    3. 現在送ろうとしているプロパティ名のいずれかがメッセージに含まれているか
       （大小文字非依存。Notion のエラー文が小文字化される実装に備える）
    """
    msg_lower = message.lower()

    # 1. クォート抽出
    m = _PROPERTY_NAME_PATTERN_QUOTED.search(message)
    if m:
        candidate = m.group(1)
        if candidate in current_props:
            return candidate
        # 大小文字差を吸収
        for name in current_props:
            if name.lower() == candidate.lower():
                return name

    # 2. 先頭パターン（"Design Status is not a property..." → "Design Status"）
    m = _PROPERTY_NAME_PATTERN_PREFIX.match(message)
    if m:
        candidate = m.group(1).strip()
        if candidate in current_props:
            return candidate
        for name in current_props:
            if name.lower() == candidate.lower():
                return name

    # 3. 現在送るプロパティのうち、メッセージに含まれるもの（大小文字非依存）
    for name in current_props:
        if name.lower() in msg_lower:
            return name

    return None
