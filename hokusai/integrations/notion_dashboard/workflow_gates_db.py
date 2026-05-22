"""Workflow Gates DB ドメインクライアント（Workgraph Phase 4 / Issue #44）

Human Approval / CI passed / Design approved / Security approved 等の
workflow 進行条件を Notion 上の明示的 gate として管理する。`pending` /
`blocked` の gate がある間、対象 workflow は先に進まない（要件 §7.5）。

設計方針（review_issues_db.py / work_items_db.py を踏襲）:
- dedupe_key（workflow_id + gate_type + required_by_phase + work_item_dedupe_key
  の sha256 hex 先頭 16 文字）で重複を抑止し、既存レコードがあれば
  **Status / Created At を除く全プロパティ** を上書き更新する upsert を
  提供する。
    - Created At: create 時のみ書き込み、Notion 側で初回作成時刻を温存
    - Status: create 時のみ初期値（既定 `pending`）を書き込み、update 時は
      payload に含めない。人間が Notion 上で `open` に書き換えた承認状態を、
      後発 upsert で `pending` に巻き戻さないため。状態遷移は専用 API
      `update_status` で扱う。
- Notion DB に該当プロパティが存在しない環境（schema 未追加など）でも壊れない
  よう、workflows_db.py と同じ property_not_found pruning を採用する。
- Gate Type / Status の enum は schema 定義（setup.py）と本ファイルの定数で
  完全一致させる。enum 値の追加は両方で同期する。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from ...logging_config import get_logger
from ._property_pruning import submit_with_property_pruning
from .client import NotionAPIClient

logger = get_logger("integrations.notion_dashboard.workflow_gates_db")


# Gate Type enum（要件 §7.2 と完全一致）。schema (setup.py:_workflow_gates_db_properties)
# の select options と完全一致させる。enum 値の追加は両方で同期。
GATE_TYPE_HUMAN_APPROVAL = "human_approval"
GATE_TYPE_CI_PASSED = "ci_passed"
GATE_TYPE_DESIGN_APPROVED = "design_approved"
GATE_TYPE_SECURITY_APPROVED = "security_approved"
GATE_TYPE_POLICY_WAIVER_APPROVED = "policy_waiver_approved"
GATE_TYPE_DEPENDENCY_RISK_ACCEPTED = "dependency_risk_accepted"
GATE_TYPE_TIMER = "timer"
GATE_TYPE_EXTERNAL = "external"

ALL_GATE_TYPES = frozenset({
    GATE_TYPE_HUMAN_APPROVAL,
    GATE_TYPE_CI_PASSED,
    GATE_TYPE_DESIGN_APPROVED,
    GATE_TYPE_SECURITY_APPROVED,
    GATE_TYPE_POLICY_WAIVER_APPROVED,
    GATE_TYPE_DEPENDENCY_RISK_ACCEPTED,
    GATE_TYPE_TIMER,
    GATE_TYPE_EXTERNAL,
})


def is_valid_gate_type(value: object) -> bool:
    """Gate Type が許容 enum 値かを判定する public helper（PR #45 Copilot 1
    回目指摘で private 直参照を撤廃するため公開）。"""
    return isinstance(value, str) and value in ALL_GATE_TYPES

# Status enum（要件 §7.3）。状態遷移の標準フロー:
# pending → open（承認）/ blocked（拒否）/ expired（期限切れ）/ canceled（取り下げ）。
# not_required は gate が不要なケースを明示的に表現するための値。
GATE_STATUS_NOT_REQUIRED = "not_required"
GATE_STATUS_PENDING = "pending"
GATE_STATUS_OPEN = "open"
GATE_STATUS_BLOCKED = "blocked"
GATE_STATUS_EXPIRED = "expired"
GATE_STATUS_CANCELED = "canceled"

ALL_GATE_STATUSES = frozenset({
    GATE_STATUS_NOT_REQUIRED,
    GATE_STATUS_PENDING,
    GATE_STATUS_OPEN,
    GATE_STATUS_BLOCKED,
    GATE_STATUS_EXPIRED,
    GATE_STATUS_CANCELED,
})


def is_valid_gate_status(value: object) -> bool:
    """Gate Status が許容 enum 値かを判定する public helper（PR #45 Copilot
    1 回目指摘で private 直参照を撤廃するため公開）。"""
    return isinstance(value, str) and value in ALL_GATE_STATUSES

# 進行を阻害する Status（ready 判定エンジンが workflow を blocked と扱う条件）。
# pending / blocked のいずれかの gate があれば対象 workflow は先に進めない。
# expired / canceled は「もはや進行条件として有効でない」ので阻害しない。
BLOCKING_GATE_STATUSES = frozenset({GATE_STATUS_PENDING, GATE_STATUS_BLOCKED})

# Phase 4 plan 時点で gate を新規作成する場合のデフォルト status。
DEFAULT_GATE_STATUS = GATE_STATUS_PENDING


def build_dedupe_key(
    *,
    workflow_id: str | None,
    gate_type: str,
    required_by_phase: int | None = None,
    work_item_dedupe_key: str | None = None,
) -> str:
    """workflow_id + gate_type + required_by_phase + work_item_dedupe_key
    から決定的な dedupe_key を生成する。

    sha256 の hex digest 先頭 16 文字を返す。

    各フィールドを hash 入力に含める根拠:
    - workflow_id: 同 gate_type / phase が **別 workflow** で発生した場合、
      別レコードとして残すため
    - gate_type: 同 workflow 内でも種別が違えば別 gate（CI と Human Approval
      は独立判定）
    - required_by_phase: 同 gate_type でも Phase 5 用と Phase 7 用は別 gate
      （例: ci_passed は Phase 5 / Phase 6 で別々に評価され得る）
    - work_item_dedupe_key: gate を Work Item 単位で立てる場合の disambiguation。
      Work Item 横断の phase レベル gate なら省略

    `None` / 空文字は空文字に正規化、gate_type は前後空白を取り除く。
    """
    parts = "\x1f".join(
        (
            workflow_id or "",
            (gate_type or "").strip(),
            "" if required_by_phase is None else str(required_by_phase),
            work_item_dedupe_key or "",
        )
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


class WorkflowGatesDBClient:
    """Notion Workflow Gates DB へのレコード作成・更新・状態遷移を担当する。"""

    def __init__(self, api: NotionAPIClient, database_id: str):
        if not database_id:
            raise ValueError("Workflow Gates DB の database_id は必須です")
        self._api = api
        self._database_id = database_id

    def upsert_gate(
        self,
        *,
        name: str,
        gate_type: str,
        status: str = DEFAULT_GATE_STATUS,
        required_by_phase: int | None = None,
        workflow_id: str | None = None,
        workflow_page_id: str | None = None,
        pull_request_page_id: str | None = None,
        work_item_page_id: str | None = None,
        review_issue_page_id: str | None = None,
        approver: str | None = None,
        decision_reason: str | None = None,
        due_at: str | None = None,
        work_item_dedupe_key: str | None = None,
        dedupe_key: str | None = None,
    ) -> dict:
        """Gate を upsert する（status は新規作成時のみ書き込み、update では温存）。"""
        if gate_type not in ALL_GATE_TYPES:
            raise ValueError(f"Gate Type の値が不正です: {gate_type!r}")
        if status not in ALL_GATE_STATUSES:
            raise ValueError(f"Gate Status の値が不正です: {status!r}")
        if not name:
            raise ValueError("name は必須です")

        if not dedupe_key:
            dedupe_key = build_dedupe_key(
                workflow_id=workflow_id,
                gate_type=gate_type,
                required_by_phase=required_by_phase,
                work_item_dedupe_key=work_item_dedupe_key,
            )

        existing_page_id = self.find_by_dedupe_key(dedupe_key)
        properties = self._build_properties(
            name=name,
            gate_type=gate_type,
            status=status,
            required_by_phase=required_by_phase,
            workflow_page_id=workflow_page_id,
            pull_request_page_id=pull_request_page_id,
            work_item_page_id=work_item_page_id,
            review_issue_page_id=review_issue_page_id,
            approver=approver,
            decision_reason=decision_reason,
            due_at=due_at,
            dedupe_key=dedupe_key,
            is_new=existing_page_id is None,
        )
        return self._submit_with_property_pruning(existing_page_id, properties)

    def update_status(
        self,
        page_id: str,
        status: str,
        *,
        approver: str | None = None,
        decision_reason: str | None = None,
    ) -> dict:
        """Gate Status を明示的に上書きする（状態遷移専用 API）。

        upsert_gate は再 dispatch で Status を巻き戻さないため温存するが、
        実際の状態遷移（pending → open / blocked / expired / canceled）には
        本 API を使う。Approver / Decision Reason を同時に上書きできる
        （要件 §7.5: gate の判断理由と承認者を audit trail として残す）。
        """
        if status not in ALL_GATE_STATUSES:
            raise ValueError(f"Gate Status の値が不正です: {status!r}")
        now_iso = datetime.now().isoformat()
        properties: dict[str, Any] = {
            "Status": {"select": {"name": status}},
            "Last Updated": _date(now_iso),
        }
        if approver:
            properties["Approver"] = _rich_text(approver)
        if decision_reason:
            properties["Decision Reason"] = _rich_text(decision_reason)
        return self._submit_with_property_pruning(page_id, properties)

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
                f"Workflow Gates DB 検索失敗: dedupe_key={dedupe_key[:8]}..., error={e}"
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
        """property_not_found 検出時に該当プロパティを除外して再試行する
        共通機構（_property_pruning.submit_with_property_pruning に集約）。"""
        return submit_with_property_pruning(
            api=self._api,
            database_id=self._database_id,
            existing_page_id=existing_page_id,
            properties=properties,
            db_label="Workflow Gates DB",
            max_attempts=max_attempts,
        )

    @staticmethod
    def _build_properties(
        *,
        name: str,
        gate_type: str,
        status: str,
        required_by_phase: int | None,
        workflow_page_id: str | None,
        pull_request_page_id: str | None,
        work_item_page_id: str | None,
        review_issue_page_id: str | None,
        approver: str | None,
        decision_reason: str | None,
        due_at: str | None,
        dedupe_key: str,
        is_new: bool,
    ) -> dict:
        # Created At / Last Updated は同 timestamp で並びを揃える（PR #37
        # Copilot 2 回目指摘と同じパターン）。
        now_iso = datetime.now().isoformat()
        props: dict[str, Any] = {
            "Name": _title(name),
            "Gate Type": {"select": {"name": gate_type}},
            "Dedupe Key": _rich_text(dedupe_key),
            "Last Updated": _date(now_iso),
        }
        # Status は新規作成時のみ書き込む。人間が Notion で open / blocked /
        # canceled 等に書き換えた状態を後発 upsert で巻き戻さないため。
        if is_new:
            props["Status"] = {"select": {"name": status}}
            props["Created At"] = _date(now_iso)
        if required_by_phase is not None:
            props["Required By Phase"] = {"number": required_by_phase}
        if workflow_page_id:
            props["Workflow"] = {"relation": [{"id": workflow_page_id}]}
        if pull_request_page_id:
            props["Pull Request"] = {
                "relation": [{"id": pull_request_page_id}]
            }
        if work_item_page_id:
            props["Work Item"] = {"relation": [{"id": work_item_page_id}]}
        if review_issue_page_id:
            props["Review Issue"] = {
                "relation": [{"id": review_issue_page_id}]
            }
        if approver:
            props["Approver"] = _rich_text(approver)
        if decision_reason:
            props["Decision Reason"] = _rich_text(decision_reason)
        if due_at:
            props["Due At"] = _date(due_at)
        return props


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _rich_text(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def _date(iso_string: str) -> dict:
    return {"date": {"start": iso_string}}
