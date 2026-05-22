"""Project Memory DB ドメインクライアント（Workgraph Phase 5 / Issue #46）

案件固有のルール / 設計判断 / 避けるべき実装 / 運用注意点 / handover note を
Notion に保存し、後段で Agent prompt に要約注入する基盤。本 module はストレージ
層のみを提供し、Agent prompt 注入機構（`hokusai prime` 等）は別 Issue で扱う。

設計方針（workflow_gates_db.py を踏襲）:
- dedupe_key（workflow_id + type + name の sha256 hex 先頭 16 文字）で重複を
  抑止し、既存レコードがあれば **Status / Created At を除く全プロパティ** を
  上書き更新する upsert を提供する。
    - Created At: create 時のみ書き込み、Notion 側で初回作成時刻を温存
    - Status: create 時のみ初期値（既定 `draft`）を書き込み、update 時は
      payload に含めない。人間が Notion 上で `active` に承認した状態を、
      後発 upsert で `draft` に巻き戻さないため（要件 §8.5）。状態遷移は
      専用 API `update_status` で扱う。
- Notion DB にプロパティが存在しない環境でも壊れないよう、共通の
  `_property_pruning.submit_with_property_pruning` を経由する。
- Type / Status の enum は schema（setup.py）と本ファイルの定数で完全一致
  させる。Agent prompt 注入対象は ACTIVE_MEMORY_STATUSES のみ。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable

from ...logging_config import get_logger
from ._property_pruning import submit_with_property_pruning
from .client import NotionAPIClient

logger = get_logger("integrations.notion_dashboard.project_memory_db")


# Memory Type enum（要件 §8.2 と完全一致）。
MEMORY_TYPE_PROJECT_RULE = "project_rule"
MEMORY_TYPE_ARCHITECTURE_DECISION = "architecture_decision"
MEMORY_TYPE_AVOIDANCE = "avoidance"
MEMORY_TYPE_DOMAIN_KNOWLEDGE = "domain_knowledge"
MEMORY_TYPE_OPERATIONS_NOTE = "operations_note"
MEMORY_TYPE_POLICY_NOTE = "policy_note"
MEMORY_TYPE_HANDOVER_NOTE = "handover_note"

ALL_MEMORY_TYPES = frozenset({
    MEMORY_TYPE_PROJECT_RULE,
    MEMORY_TYPE_ARCHITECTURE_DECISION,
    MEMORY_TYPE_AVOIDANCE,
    MEMORY_TYPE_DOMAIN_KNOWLEDGE,
    MEMORY_TYPE_OPERATIONS_NOTE,
    MEMORY_TYPE_POLICY_NOTE,
    MEMORY_TYPE_HANDOVER_NOTE,
})


def is_valid_memory_type(value: object) -> bool:
    """Memory Type が許容 enum 値かを判定する public helper。"""
    return isinstance(value, str) and value in ALL_MEMORY_TYPES


# Memory Status enum（要件 §8.3）。状態遷移の典型フロー:
# draft（Agent 自動生成 / 人間入力）→ active（人間承認）→ deprecated（廃止）
# rejected は draft からの却下経路。
MEMORY_STATUS_DRAFT = "draft"
MEMORY_STATUS_ACTIVE = "active"
MEMORY_STATUS_DEPRECATED = "deprecated"
MEMORY_STATUS_REJECTED = "rejected"

ALL_MEMORY_STATUSES = frozenset({
    MEMORY_STATUS_DRAFT,
    MEMORY_STATUS_ACTIVE,
    MEMORY_STATUS_DEPRECATED,
    MEMORY_STATUS_REJECTED,
})


def is_valid_memory_status(value: object) -> bool:
    """Memory Status が許容 enum 値かを判定する public helper。"""
    return isinstance(value, str) and value in ALL_MEMORY_STATUSES


# Agent prompt 注入対象になる Status（要件 §8.5: draft / deprecated / rejected は
# Agent に渡さない）。`hokusai prime` 等の future CLI 実装はこの定数を見て
# active なものだけを抽出する。
ACTIVE_MEMORY_STATUSES = frozenset({MEMORY_STATUS_ACTIVE})

# Agent / 人間が新規 memory を起こす際のデフォルト status。要件 §8.5 で
# 「Agent が自動生成した memory は必ず draft から開始する」とあり、Agent
# 入力経路では draft 固定にする運用を想定（人間明示時のみ override 可能）。
DEFAULT_MEMORY_STATUS = MEMORY_STATUS_DRAFT


def build_dedupe_key(
    *,
    workflow_id: str | None,
    memory_type: str,
    name: str,
) -> str:
    """workflow_id + memory_type + name から決定的な dedupe_key を生成する。

    sha256 の hex digest 先頭 16 文字を返す。

    各フィールドを hash 入力に含める根拠:
    - workflow_id: 同 type / name が **別 workflow** で発生した場合、別レコード
      として残すため（review_issues_db / work_items_db と同じ理由）
    - memory_type: 同 workflow 内でも種別違いは別 memory
    - name: 同 workflow / 同 type 内の異なる memory を識別

    `None` / 空文字は空文字に正規化、name は前後空白を取り除く（先頭だけだと
    別記述の同種 memory が衝突する）。
    """
    parts = "\x1f".join(
        (
            workflow_id or "",
            (memory_type or "").strip(),
            (name or "").strip(),
        )
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


class ProjectMemoryDBClient:
    """Notion Project Memory DB へのレコード作成・更新・状態遷移を担当する。"""

    def __init__(self, api: NotionAPIClient, database_id: str):
        if not database_id:
            raise ValueError("Project Memory DB の database_id は必須です")
        self._api = api
        self._database_id = database_id

    def upsert_memory(
        self,
        *,
        name: str,
        memory_type: str,
        content: str,
        summary: str | None = None,
        status: str = DEFAULT_MEMORY_STATUS,
        profile: str | None = None,
        applies_to: Iterable[str] | None = None,
        workflow_id: str | None = None,
        workflow_page_id: str | None = None,
        pull_request_page_id: str | None = None,
        approved_by: str | None = None,
        approved_at: str | None = None,
        expires_at: str | None = None,
        dedupe_key: str | None = None,
    ) -> dict:
        """Project Memory を upsert する（status は新規作成時のみ書き込み、
        update 時は温存）。"""
        if not is_valid_memory_type(memory_type):
            raise ValueError(f"Memory Type の値が不正です: {memory_type!r}")
        if not is_valid_memory_status(status):
            raise ValueError(f"Memory Status の値が不正です: {status!r}")
        if not name:
            raise ValueError("name は必須です")
        if not content:
            raise ValueError("content は必須です")

        if not dedupe_key:
            dedupe_key = build_dedupe_key(
                workflow_id=workflow_id,
                memory_type=memory_type,
                name=name,
            )

        existing_page_id = self.find_by_dedupe_key(dedupe_key)
        properties = self._build_properties(
            name=name,
            memory_type=memory_type,
            status=status,
            profile=profile,
            content=content,
            summary=summary,
            applies_to=list(applies_to or []),
            workflow_page_id=workflow_page_id,
            pull_request_page_id=pull_request_page_id,
            approved_by=approved_by,
            approved_at=approved_at,
            expires_at=expires_at,
            dedupe_key=dedupe_key,
            is_new=existing_page_id is None,
        )
        return self._submit_with_property_pruning(existing_page_id, properties)

    def update_status(
        self,
        page_id: str,
        status: str,
        *,
        approved_by: str | None = None,
        approved_at: str | None = None,
    ) -> dict:
        """Memory Status を明示的に上書きする（状態遷移専用 API）。

        upsert_memory は再 dispatch で Status を巻き戻さないため温存するが、
        実際の状態遷移（draft → active / deprecated / rejected）には本 API
        を使う。Approved By / Approved At を同時に上書きできる（要件 §8.5:
        memory の編集・承認・廃止は audit log に残す）。
        """
        if not is_valid_memory_status(status):
            raise ValueError(f"Memory Status の値が不正です: {status!r}")
        now_iso = datetime.now().isoformat()
        properties: dict[str, Any] = {
            "Status": {"select": {"name": status}},
            "Last Updated": _date(now_iso),
        }
        if approved_by:
            properties["Approved By"] = _rich_text(approved_by)
        if approved_at:
            properties["Approved At"] = _date(approved_at)
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
                f"Project Memory DB 検索失敗: dedupe_key={dedupe_key[:8]}..., error={e}"
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
        """共通の property_not_found pruning へ委譲（_property_pruning helper）。"""
        return submit_with_property_pruning(
            api=self._api,
            database_id=self._database_id,
            existing_page_id=existing_page_id,
            properties=properties,
            db_label="Project Memory DB",
            max_attempts=max_attempts,
        )

    @staticmethod
    def _build_properties(
        *,
        name: str,
        memory_type: str,
        status: str,
        profile: str | None,
        content: str,
        summary: str | None,
        applies_to: list[str],
        workflow_page_id: str | None,
        pull_request_page_id: str | None,
        approved_by: str | None,
        approved_at: str | None,
        expires_at: str | None,
        dedupe_key: str,
        is_new: bool,
    ) -> dict:
        now_iso = datetime.now().isoformat()
        props: dict[str, Any] = {
            "Name": _title(name),
            "Type": {"select": {"name": memory_type}},
            "Content": _rich_text(content),
            "Dedupe Key": _rich_text(dedupe_key),
            "Last Updated": _date(now_iso),
        }
        # Status は新規作成時のみ書き込む。人間が active / deprecated / rejected に
        # 書き換えた状態を後発 upsert で巻き戻さないため（要件 §8.5）。
        if is_new:
            props["Status"] = {"select": {"name": status}}
            props["Created At"] = _date(now_iso)
        if profile:
            props["Profile"] = _rich_text(profile)
        if summary:
            props["Summary"] = _rich_text(summary)
        if applies_to:
            props["Applies To"] = {
                "multi_select": [{"name": item} for item in applies_to]
            }
        if workflow_page_id:
            props["Workflow"] = {"relation": [{"id": workflow_page_id}]}
        if pull_request_page_id:
            props["Pull Request"] = {
                "relation": [{"id": pull_request_page_id}]
            }
        if approved_by:
            props["Approved By"] = _rich_text(approved_by)
        if approved_at:
            props["Approved At"] = _date(approved_at)
        if expires_at:
            props["Expires At"] = _date(expires_at)
        return props


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _rich_text(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def _date(iso_string: str) -> dict:
    return {"date": {"start": iso_string}}
