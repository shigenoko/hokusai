"""Work Items DB ドメインクライアント（Workgraph Phase 2 / Issue #38）

Phase 4 plan で生成された work_plan を構造化 Work Item として Notion に同期し、
dependencies / blocking_review_issues を踏まえた ready 判定の根拠を Notion 側で
人間が確認できるようにする。

設計方針（review_issues_db.py と同じパターンを踏襲）:
- dedupe_key（workflow_id + phase + title の sha256 hex の先頭 16 文字）で重複を
  抑止し、既存レコードがあれば **Status / Created At を除く全プロパティ** を
  上書き更新する upsert を提供する。
    - Created At: create 時のみ書き込み、Notion 側で初回作成時刻を温存
    - Status: create 時のみ初期値（pending）を書き込み、update 時は payload に
      含めない。人間が Notion 上で in_progress → done に書き換えた、あるいは
      Phase 5 implement の status 遷移で done になった状態を、後発 upsert で
      pending に巻き戻さないため。状態遷移は専用 API `update_status` で扱う。
    - その他（Title / Phase / Workflow / Operator / Description / Dedupe Key /
      Dependencies / Blocking Review Issues / Last Updated）は最新の payload で
      常に上書き
- Notion DB に該当プロパティが存在しない環境（schema 未追加など）でも壊れない
  よう、workflows_db.py と同じ property_not_found pruning を採用する。
- Status / 状態遷移の enum は schema 定義（setup.py）と本ファイルの定数で完全
  一致させる。enum 値の追加は両方で同期する。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable

from ...logging_config import get_logger
from .client import NotionAPIClient, NotionAPIError
from .workflows_db import (
    _extract_missing_property,
    _is_property_not_found,
)

logger = get_logger("integrations.notion_dashboard.work_items_db")


# Status enum。schema (setup.py:_work_items_db_properties) と完全一致させる。
# 状態遷移の標準フロー: pending → ready → in_progress → done。
# 派生分岐: blocked（blocking_review_issues が open / dependencies が未 done）/
# skipped（人手で見送り）/ canceled（workflow 中止）。
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_CANCELED = "canceled"

# Phase 4 plan で自動生成される Work Item のデフォルト status。
# ready 判定エンジンが依存解決済みかつ blocking なしを検出した時点で
# pending → ready に遷移する。
DEFAULT_STATUS = STATUS_PENDING

# Lease lifecycle 状態（Workgraph Phase 3 / Issue #42）。
# - active: Agent が claim 中、Lease Expires At > now の場合のみ有効
# - expired: lease 期限切れ。再 claim 可能（人間または Operations Console から）
# - released: Agent が正常完了して明示的に lease を解放した
LEASE_STATUS_ACTIVE = "active"
LEASE_STATUS_EXPIRED = "expired"
LEASE_STATUS_RELEASED = "released"

# Claim 主体の種別（要件 §6.2）。agent は Claude Code / Codex / Gemini /
# GitHub Copilot / external、human は人間オペレーター。
CLAIM_TYPE_AGENT = "agent"
CLAIM_TYPE_HUMAN = "human"

# Lease 期限のデフォルト（秒）。Phase 5 implement の LLM 駆動実装は数分〜
# 数十分かかるため、1 時間（3600 秒）を default にする。呼び出し側で
# override 可能。
DEFAULT_LEASE_DURATION_SECONDS = 3600


def build_dedupe_key(
    *,
    workflow_id: str | None,
    phase: int | None,
    title: str,
) -> str:
    """workflow_id + phase + title から決定的な dedupe_key を生成する。

    sha256 の hex digest 先頭 16 文字を返す。

    各フィールドを hash 入力に含める根拠:
    - workflow_id: 同じ phase / title が **別 workflow** で発生した場合、別
      レコードとして残すため。workflow_id を含めないと、後発 workflow の
      dispatch で Workflow relation が上書きされ、先発 workflow との関連が
      失われる（Review Issues DB と同じ問題）
    - phase: Phase 4 plan が phase ごとに work_item を分けるケースを別レコード
      化するため
    - title: 同一 workflow / phase 内の異なる Work Item を識別するため

    `None` や空文字は空文字に正規化、title は前後空白を取り除いた上で全長を
    使う（先頭だけだと別 Item の同種タイトル prefix が衝突する）。
    """
    parts = "\x1f".join(
        (
            workflow_id or "",
            "" if phase is None else str(phase),
            (title or "").strip(),
        )
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


class WorkItemsDBClient:
    """Notion Work Items DB へのレコード作成・更新・状態遷移を担当する。"""

    def __init__(self, api: NotionAPIClient, database_id: str):
        if not database_id:
            raise ValueError("Work Items DB の database_id は必須です")
        self._api = api
        self._database_id = database_id

    def upsert_work_item(
        self,
        *,
        title: str,
        phase: int | None = None,
        status: str = DEFAULT_STATUS,
        workflow_id: str | None = None,
        workflow_page_id: str | None = None,
        operator: str | None = None,
        description: str | None = None,
        dependency_page_ids: Iterable[str] | None = None,
        blocking_review_issue_page_ids: Iterable[str] | None = None,
        dedupe_key: str | None = None,
    ) -> dict:
        """Work Item を upsert する。

        Args:
            title: Work Item 表示タイトル
            phase: 紐づく HOKUSAI phase 番号（4 / 5 など）
            status: 新規作成時のみ書き込む初期 status（既定 pending）
            workflow_id: HOKUSAI workflow_id（dedupe_key の hash 入力に含める）
            workflow_page_id: 関連 workflow の Notion page id（relation 用）
            operator: workflow を起動した実行者
            description: Work Item の説明（plan ノードが抽出した本文等）
            dependency_page_ids: 依存する Work Item の Notion page id 集合
                （self-relation `Dependencies` に渡す）
            blocking_review_issue_page_ids: 進行を止めている Review Issues の
                Notion page id 集合（`Blocking Review Issues` relation に渡す）
            dedupe_key: 省略時は build_dedupe_key で生成

        Returns:
            Notion から返された page オブジェクト
        """
        if not dedupe_key:
            dedupe_key = build_dedupe_key(
                workflow_id=workflow_id, phase=phase, title=title
            )

        existing_page_id = self.find_by_dedupe_key(dedupe_key)
        properties = self._build_properties(
            title=title,
            phase=phase,
            status=status,
            workflow_page_id=workflow_page_id,
            operator=operator,
            description=description,
            dependency_page_ids=list(dependency_page_ids or []),
            blocking_review_issue_page_ids=list(
                blocking_review_issue_page_ids or []
            ),
            dedupe_key=dedupe_key,
            is_new=existing_page_id is None,
        )
        return self._submit_with_property_pruning(existing_page_id, properties)

    def update_status(self, page_id: str, status: str) -> dict:
        """Work Item の Status のみを更新する（状態遷移専用 API）。

        upsert_work_item の update 経路では Status を意図的に温存するため、
        Phase 5 implement の done 遷移や ready 判定エンジンの ready 昇格には
        この明示的 API を使う。Notion 側で人間が in_progress → done に手動
        遷移したケースもこちらの方が意図が明確（呼び出し側に「上書きする」と
        明示的に書かせる）。
        """
        if status not in (
            STATUS_PENDING,
            STATUS_READY,
            STATUS_IN_PROGRESS,
            STATUS_BLOCKED,
            STATUS_DONE,
            STATUS_SKIPPED,
            STATUS_CANCELED,
        ):
            raise ValueError(f"Work Item Status の値が不正です: {status!r}")
        properties = {
            "Status": {"select": {"name": status}},
            "Last Updated": _date(datetime.now().isoformat()),
        }
        return self._submit_with_property_pruning(page_id, properties)

    def claim_work_item(
        self,
        page_id: str,
        *,
        claimed_by: str,
        claim_type: str = CLAIM_TYPE_AGENT,
        lease_duration_seconds: int = DEFAULT_LEASE_DURATION_SECONDS,
    ) -> dict:
        """Work Item に Lease を張って Agent / 人間が claim する。

        既存 lease（active）の上書きは **本 API ではチェックしない**。通常
        フローでは ready_judgment.compute_ready_state() が active 未期限
        lease を持つ Work Item を in_progress 相当として扱い、別 Agent が
        新たに claim 候補に拾わないことで衝突を防ぐ（要件 §4.5）。期限切れ
        lease や手動 release 後の Work Item を再 claim するのは正常動作。

        Args:
            page_id: Notion page id
            claimed_by: Claim 主体（"claude_code" / "codex" / "alice@example.com" 等）
            claim_type: CLAIM_TYPE_AGENT または CLAIM_TYPE_HUMAN
            lease_duration_seconds: lease 期限（既定 1 時間）

        Returns:
            Notion から返された更新後 page オブジェクト。`Lease Token` を含む
            properties は呼び出し側で参照可能。token は呼び出し側でも保持して
            `release_lease` 時に整合性確認に使う想定（MVP では確認はしない）。
        """
        if claim_type not in (CLAIM_TYPE_AGENT, CLAIM_TYPE_HUMAN):
            raise ValueError(f"Claim Type の値が不正です: {claim_type!r}")
        if not claimed_by:
            raise ValueError("claimed_by は必須です")
        if lease_duration_seconds <= 0:
            raise ValueError(
                f"lease_duration_seconds は正の整数: {lease_duration_seconds}"
            )
        now = datetime.now()
        expires = now + timedelta(seconds=lease_duration_seconds)
        # Lease Token は uuid4 で生成。HOKUSAI 内部で「この claim を作った
        # 主体」を後段で確認するため。MVP では release_lease は token 確認
        # せず page_id のみで release するが、将来の検証強化に備えて保存する。
        token = uuid.uuid4().hex
        properties = {
            "Claimed By": _rich_text(claimed_by),
            "Claim Type": {"select": {"name": claim_type}},
            "Lease Status": {"select": {"name": LEASE_STATUS_ACTIVE}},
            "Lease Started At": _date(now.isoformat()),
            "Lease Expires At": _date(expires.isoformat()),
            "Lease Token": _rich_text(token),
            "Last Updated": _date(now.isoformat()),
        }
        return self._submit_with_property_pruning(page_id, properties)

    def release_lease(self, page_id: str) -> dict:
        """Agent が正常完了した時点で lease を解放する（要件 §6.6）。

        Lease Status を `released` に変更し、Lease Expires At は **温存** する
        （いつ release されたかは Last Updated で記録される）。Claimed By 等の
        identity は監査用にそのまま残す。Lease Token も温存。
        """
        properties = {
            "Lease Status": {"select": {"name": LEASE_STATUS_RELEASED}},
            "Last Updated": _date(datetime.now().isoformat()),
        }
        return self._submit_with_property_pruning(page_id, properties)

    def expire_lease(self, page_id: str) -> dict:
        """Lease を期限切れマークする（人間 / Operations Console から呼ぶ）。

        通常は Lease Expires At < now を検出した sweep ジョブが呼ぶ想定だが、
        MVP では sweep は実装せず Operations Console からの手動 expire のみ
        を想定する。
        """
        properties = {
            "Lease Status": {"select": {"name": LEASE_STATUS_EXPIRED}},
            "Last Updated": _date(datetime.now().isoformat()),
        }
        return self._submit_with_property_pruning(page_id, properties)

    def list_ready_work_items_for_workflow(
        self,
        workflow_page_id: str | None,
        *,
        max_pages: int = 5,
    ) -> list[dict]:
        """指定 workflow に紐づく ready / in_progress な Work Item を取得する
        （Workgraph 完成 / Issue #54 / 要件 §8.4 `hokusai prime` 統合表示）。

        サーバ側 filter: AND(Status in {ready, in_progress}, Workflow contains
        workflow_page_id)。Phase 5 implement で Agent に渡す候補 + 現在進行中
        の Work Item をまとめて出力する用途。

        Args:
            workflow_page_id: Notion 上の workflow ページ id。空 / None なら
                空リスト即返却。
            max_pages: ページネーション安全上限。

        Returns:
            Notion page dict のリスト。API 失敗時は部分結果を保持して返す
            （prime 注入で全消失より部分提供を優先する設計、
            `find_handover_notes_for_workflow` と同じパターン）。
        """
        if not workflow_page_id:
            return []

        results: list[dict] = []
        start_cursor: str | None = None
        truncated = False
        for page_idx in range(max_pages):
            try:
                response = self._api.query_database(
                    self._database_id,
                    filter_={
                        "and": [
                            {
                                "or": [
                                    {
                                        "property": "Status",
                                        "select": {"equals": STATUS_READY},
                                    },
                                    {
                                        "property": "Status",
                                        "select": {
                                            "equals": STATUS_IN_PROGRESS
                                        },
                                    },
                                ]
                            },
                            {
                                "property": "Workflow",
                                "relation": {"contains": workflow_page_id},
                            },
                        ]
                    },
                    start_cursor=start_cursor,
                    page_size=100,
                )
            except Exception as e:
                logger.warning(
                    "Work Items DB list_ready_work_items_for_workflow 失敗 "
                    "(部分結果 %d 件で続行): %s",
                    len(results), e,
                )
                return results
            results.extend(response.get("results") or [])
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
                "list_ready_work_items_for_workflow が max_pages=%d で打ち切られました "
                "(取得済み %d 件で返却)",
                max_pages, len(results),
            )
        return results

    def find_by_dedupe_key(self, dedupe_key: str) -> str | None:
        """dedupe_key で既存レコードを検索する。"""
        if not dedupe_key:
            return None
        try:
            response = self._api.query_database(
                self._database_id,
                filter_={
                    "property": "Dedupe Key",
                    "rich_text": {"equals": dedupe_key},
                },
            )
        except Exception as e:
            logger.debug(
                f"Work Items DB 検索失敗: dedupe_key={dedupe_key[:8]}..., error={e}"
            )
            raise
        results = response.get("results") or []
        if not results:
            return None
        return results[0].get("id")

    def _submit_with_property_pruning(
        self,
        existing_page_id: str | None,
        properties: dict,
        max_attempts: int = 6,
    ) -> dict:
        """create / update を試行し、property_not_found なら原因プロパティを除去して再試行。

        review_issues_db._submit_with_property_pruning と同じ仕組み。
        Notion 側にプロパティ自体が存在しないケースのみが対象（select option が
        存在しないケースは Notion API が自動で option を作るため除外）。
        """
        attempts = 0
        current_props = dict(properties)
        while True:
            attempts += 1
            try:
                return self._create_or_update(existing_page_id, current_props)
            except NotionAPIError as exc:
                if not _is_property_not_found(exc):
                    raise
                self._prune_missing_or_raise(
                    exc, current_props, attempts, max_attempts
                )

    def _create_or_update(
        self, existing_page_id: str | None, current_props: dict
    ) -> dict:
        if existing_page_id is None:
            return self._api.create_page({
                "parent": {"database_id": self._database_id},
                "properties": current_props,
            })
        return self._api.update_page(
            existing_page_id, {"properties": current_props}
        )

    @staticmethod
    def _prune_missing_or_raise(
        exc: NotionAPIError,
        current_props: dict,
        attempts: int,
        max_attempts: int,
    ) -> None:
        if attempts >= max_attempts:
            logger.warning(
                "property_not_found リトライ上限に到達: 残プロパティ数=%d",
                len(current_props),
            )
            raise exc
        missing = _extract_missing_property(exc.message, current_props)
        if missing is None:
            logger.warning(
                "property_not_found 検知だが対象プロパティを特定できず: %s",
                exc.message[:200],
            )
            raise exc
        logger.info(
            "Work Items DB に '%s' プロパティが存在しないため除外して再試行",
            missing,
        )
        current_props.pop(missing, None)
        if not current_props:
            logger.warning("除外後にプロパティが空になったため処理を中断")
            raise exc

    @staticmethod
    def _build_properties(
        *,
        title: str,
        phase: int | None,
        status: str,
        workflow_page_id: str | None,
        operator: str | None,
        description: str | None,
        dependency_page_ids: list[str],
        blocking_review_issue_page_ids: list[str],
        dedupe_key: str,
        is_new: bool,
    ) -> dict:
        # Created At と Last Updated は同一の datetime.now() を使う（並び順の
        # 整合性確保。review_issues_db と同様）。
        now_iso = datetime.now().isoformat()
        props: dict[str, Any] = {
            "Title": _title(title),
            "Dedupe Key": _rich_text(dedupe_key),
            "Last Updated": _date(now_iso),
        }
        # Status は新規作成時のみ書き込む。再 upsert で Phase 5 implement や
        # 人手の状態遷移を巻き戻さないため。状態遷移は update_status を使う。
        if is_new:
            props["Status"] = {"select": {"name": status}}
            props["Created At"] = _date(now_iso)
        if phase is not None:
            props["Phase"] = {"number": phase}
        if workflow_page_id:
            props["Workflow"] = {"relation": [{"id": workflow_page_id}]}
        if operator:
            props["Operator"] = _rich_text(operator)
        if description:
            props["Description"] = _rich_text(description)
        # relation は空 list を渡しても Notion 側で「relation を空に上書き」と
        # 解釈されるため、ここでは items が空ならキー自体を含めない（誤って
        # 既存依存を消さないため）。明示的に「依存を消す」操作は別 API で扱う。
        if dependency_page_ids:
            props["Dependencies"] = {
                "relation": [{"id": pid} for pid in dependency_page_ids]
            }
        if blocking_review_issue_page_ids:
            props["Blocking Review Issues"] = {
                "relation": [
                    {"id": pid} for pid in blocking_review_issue_page_ids
                ]
            }
        return props


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _rich_text(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def _date(iso_string: str) -> dict:
    return {"date": {"start": iso_string}}
