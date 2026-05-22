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
#   "Could not find property with name or id: '<NAME>'. ..." (single quote)
# プロパティ名は空白を含み得る（例: "Design Status"）ため、prefix パターンは最短一致で
# "is not a property" の直前まで全部キャプチャする。クォートは double / single の
# 両方を許容（Notion のメッセージ表記ゆれに対応）。
_PROPERTY_NAME_PATTERN_QUOTED = re.compile(r"""(?:"([^"]+)"|'([^']+)')""")
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

    def set_supersedes(
        self, page_id: str, prior_workflow_page_id: str
    ) -> dict:
        """`Supersedes`（self-link relation）を設定する（Workgraph Phase 7
        / Issue #50 / 要件 §9.3.3）。

        新 workflow（wf-B）から旧 workflow（wf-A）への引き継ぎリレーション。
        `single_property` 採用のため Notion 側 `Superseded By` の synced
        backref は表示されない。

        本メソッドは単一プロパティ（`Supersedes` のみ）を書き込むため、Notion
        側に `Supersedes` が未追加（migrate 未実施環境）だと `_submit_with_property_pruning`
        が該当プロパティを除外した結果 payload が空になり `NotionAPIError`
        を raise する。呼び出し側は「`hokusai notion-migrate-schema` 実施
        必要」のシグナルとして扱う想定（silent no-op で同期破壊が見えなく
        なるのを避ける）。複数プロパティ書き込みの apply_event 経路とは
        挙動が異なる点に注意。
        """
        if not page_id:
            raise ValueError("page_id は必須です")
        if not prior_workflow_page_id:
            raise ValueError("prior_workflow_page_id は必須です")
        properties = {
            "Supersedes": {"relation": [{"id": prior_workflow_page_id}]}
        }
        return self._submit_with_property_pruning(page_id, properties)

    def get_supersedes(self, page_id: str) -> list[str]:
        """`Supersedes` リレーション値（旧 workflow の page_id リスト）を取得する。

        次 PR（handover_note 世代遡及）で使用予定。Notion から返る
        `relation` プロパティを抜き出して `[{"id": "..."}]` の id を平坦化する。
        プロパティが存在しない / 失敗時は空リストを返す（部分結果保持の方針）。
        """
        if not page_id:
            return []
        try:
            page = self._api.retrieve_page(page_id)
        except Exception as e:
            logger.debug(
                f"Workflows DB retrieve_page 失敗: page_id={page_id[:8]}..., error={e}"
            )
            return []
        prop = (page.get("properties") or {}).get("Supersedes") or {}
        relations = prop.get("relation") or []
        return [r.get("id") for r in relations if r.get("id")]

    def set_cancel_reason(self, page_id: str, reason: str) -> dict:
        """`Cancel Reason`（rich_text）を設定する（Workgraph Phase 7 / Issue #50）。

        Status=Canceled 時の理由。引き継ぎ運用（要件 §9.3.2）では推奨。
        引数 `page_id` / `reason` が空 / None なら `ValueError` を raise する
        （silent no-op は呼び出し側のバグを隠すため、明示的に拒否する方針）。

        単一プロパティ書き込みのため、Notion 側に `Cancel Reason` が未追加
        （migrate 未実施環境）だと `_submit_with_property_pruning` が pruning
        した結果 payload が空になり `NotionAPIError` を raise する。呼び出し
        側は「`hokusai notion-migrate-schema` 実施必要」のシグナルとして扱う
        想定（`set_supersedes` と同じ方針）。
        """
        if not page_id:
            raise ValueError("page_id は必須です")
        if not reason:
            raise ValueError("reason は必須です")
        properties = {"Cancel Reason": _rich_text(str(reason))}
        return self._submit_with_property_pruning(page_id, properties)

    def find_workflow_page_id(self, workflow_id: str) -> str | None:
        """Workflow ID プロパティで Notion DB を検索し、page_id を返す
        （Workgraph Phase 7 / Issue #52: handover_note 世代遡及で使用）。

        `_find_page_id` と挙動は同じだが、本メソッドは外部呼び出し向けに
        例外を抑制して None を返す（API 失敗 / 検索 miss いずれも None）。
        prime CLI 等の read-only 経路は障害でフローを止めず memory 取得を
        skip させる方が UX として望ましいため。書き込み経路（apply_event）
        は内部 `_find_page_id` を引き続き使い、API 失敗を raise する。
        """
        if not workflow_id:
            return None
        try:
            return self._find_page_id(workflow_id)
        except Exception:
            # 失敗内容は `_find_page_id` 側 logger.debug で既に出力済み。
            # ここで再度 log を出すとログノイズになるので raise を握り潰すだけ
            # （wrapper の責務は「例外を抑制して None を返す」のみ）。
            return None

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

        # Operator: workflow_started event でのみ書き込む（Issue #21 / v0.4.8）。
        # 以降の event で誤って payload に operator が混入しても Notion 側の既存値を
        # 温存できるよう、event_type を明示的にガードする（invariant の強制）。
        # DB に Operator プロパティが無い環境では property_not_found pruning で
        # 自動的に除外される（後方互換）。
        if (
            event_type == EVENT_WORKFLOW_STARTED
            and "operator" in payload
            and payload["operator"]
        ):
            props["Operator"] = _rich_text(str(payload["operator"]))

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

    1. クォート（double / single）で囲まれた名前 — current_props と一致時のみ
    2. `<name> is not a property` の prefix パターン（最短一致、空白含む名前を許容）
    3. 現在送ろうとしているプロパティ名のいずれかがメッセージに含まれているか
       （大小文字非依存。**長い名前を優先**して評価することで、`Status` が
       `Design Status` を先取りして誤削除する事故を防ぐ）
    """
    msg_lower = message.lower()

    # 1. クォート抽出（double / single 両対応）
    m = _PROPERTY_NAME_PATTERN_QUOTED.search(message)
    if m:
        # group(1)=double quote 内、group(2)=single quote 内（どちらかは None）
        candidate = m.group(1) or m.group(2)
        if candidate:
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

    # 3. 含有チェック。長い名前を優先することで、"Status" が "Design Status" より
    #    先に一致して payload から先取りされる誤削除を避ける。
    for name in sorted(current_props, key=len, reverse=True):
        if name.lower() in msg_lower:
            return name

    return None



# ----- Public API: property error helpers（PR #45 Copilot 3 回目対応） -----
# `_is_property_not_found` / `_extract_missing_property` は当初本 module の
# 内部実装として private 命名で導入したが、後段で他 client（review_issues_db
# / work_items_db / workflow_gates_db / _property_pruning helper）が同 helper
# を必要とすることが判明し、現状 4 module から参照される共有実装になっている。
# 名前の private prefix を外して公開 API 化することで、依存方向の不安定さ
# （private 名のリファクタで他 module が壊れる）を解消する。
# 既存 private 名は本 module の内部利用のため残し、新規参照側は public 名を
# 使う方針。
is_property_not_found = _is_property_not_found
extract_missing_property = _extract_missing_property
