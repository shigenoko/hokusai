"""Review Issues DB ドメインクライアント

Phase 6 verification failure / Phase 7 final review の指摘を Notion Review Issues
DB に構造化レコードとして同期する。後続の Policy Governance 違反 / LLM Gateway
ブロック / Copilot 指摘 / CI failure の共通受け皿となるよう、Source enum に将来
枠を最初から確保する。

設計方針:
- dedupe_key（rule + file + message の hash）で重複を抑止し、既存レコードがあれば
  Status / Last Updated のみ更新する upsert を提供する。
- Notion DB に該当プロパティが存在しない環境（schema 未追加など）でも壊れない
  よう、workflows_db.py と同じ property_not_found pruning を採用する。
- 重要度・状態の select option は schema 定義（setup.py）と本ファイルの定数で
  一致させる。enum 値の追加は両方で同期する。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient, NotionAPIError
from .workflows_db import (
    _extract_missing_property,
    _is_property_not_found,
)

logger = get_logger("integrations.notion_dashboard.review_issues_db")


# Source enum。前 4 つが MVP で発行する種別、後 3 つは後続機能用の先行確保枠。
# schema (setup.py:_review_issues_db_properties) と完全一致させる。
SOURCE_FINAL_REVIEW = "final_review"
SOURCE_VERIFICATION_FAILURE = "verification_failure"
SOURCE_COPILOT_REVIEW = "copilot_review"
SOURCE_CI_FAILURE = "ci_failure"
SOURCE_POLICY_VIOLATION = "policy_violation"
SOURCE_LLM_GATEWAY_BLOCK = "llm_gateway_block"
SOURCE_DEPENDENCY_VULN = "dependency_vuln"

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_WAIVED = "waived"
STATUS_DUPLICATE = "duplicate"

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITY_INFO = "info"


def build_dedupe_key(
    *, source: str, rule: str | None, file: str | None, message: str
) -> str:
    """rule + file + message から決定的な dedupe_key を生成する。

    source も hash 入力に含めることで、別 source が偶然同じ rule/file/message を
    生成しても別レコードとして扱う。`None` は空文字に正規化し、message は
    前後空白を取り除いた上で全長を使う（先頭だけだと別箇所の同種指摘が衝突する）。
    """
    parts = "\x1f".join(
        (source or "", rule or "", file or "", (message or "").strip())
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


class ReviewIssuesDBClient:
    """Notion Review Issues DB へのレコード作成・更新を担当する。"""

    def __init__(self, api: NotionAPIClient, database_id: str):
        if not database_id:
            raise ValueError("Review Issues DB の database_id は必須です")
        self._api = api
        self._database_id = database_id

    def upsert_record(
        self,
        *,
        source: str,
        message: str,
        severity: str = SEVERITY_MEDIUM,
        status: str = STATUS_OPEN,
        rule: str | None = None,
        file: str | None = None,
        repository: str | None = None,
        workflow_page_id: str | None = None,
        operator: str | None = None,
        dedupe_key: str | None = None,
        title: str | None = None,
    ) -> dict:
        """Review Issue を upsert する。

        Args:
            source: SOURCE_* のいずれか
            message: 指摘本文
            severity: SEVERITY_* のいずれか
            status: STATUS_* のいずれか
            rule: linter rule / レビュー観点（任意）
            file: 該当ファイルパス（任意）
            repository: リポジトリ表示名（PR DB と揃える: Backend / Frontend 等）
            workflow_page_id: 関連 workflow の Notion page id（relation 用）
            operator: workflow を起動した実行者
            dedupe_key: 重複判定キー。省略時は build_dedupe_key で生成
            title: Notion 表示用タイトル。省略時は `[source] file — summary` で生成

        Returns:
            Notion から返された page オブジェクト
        """
        if dedupe_key is None:
            dedupe_key = build_dedupe_key(
                source=source, rule=rule, file=file, message=message
            )
        if title is None:
            title = _build_title(source=source, file=file, message=message)

        existing_page_id = self.find_by_dedupe_key(dedupe_key)
        properties = self._build_properties(
            source=source,
            status=status,
            severity=severity,
            message=message,
            rule=rule,
            file=file,
            repository=repository,
            workflow_page_id=workflow_page_id,
            operator=operator,
            dedupe_key=dedupe_key,
            title=title,
            is_new=existing_page_id is None,
        )
        return self._submit_with_property_pruning(existing_page_id, properties)

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
                f"Review Issues DB 検索失敗: dedupe_key={dedupe_key[:8]}..., error={e}"
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

        workflows_db._submit_with_property_pruning と同じ仕組み。Review Issues DB
        の schema が古い環境（後続機能用の Source enum が未追加など）でも、
        存在するプロパティだけで同期が進む。
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
                    logger.warning(
                        "property_not_found 検知だが対象プロパティを特定できず: %s",
                        exc.message[:200],
                    )
                    raise
                logger.info(
                    "Review Issues DB に '%s' プロパティが存在しないため除外して再試行",
                    missing,
                )
                current_props.pop(missing, None)
                if not current_props:
                    logger.warning("除外後にプロパティが空になったため処理を中断")
                    raise

    @staticmethod
    def _build_properties(
        *,
        source: str,
        status: str,
        severity: str,
        message: str,
        rule: str | None,
        file: str | None,
        repository: str | None,
        workflow_page_id: str | None,
        operator: str | None,
        dedupe_key: str,
        title: str,
        is_new: bool,
    ) -> dict:
        props: dict[str, Any] = {
            "Title": _title(title),
            "Source": {"select": {"name": source}},
            "Status": {"select": {"name": status}},
            "Severity": {"select": {"name": severity}},
            "Dedupe Key": _rich_text(dedupe_key),
            "Message": _rich_text(message),
            "Last Updated": _date(datetime.now().isoformat()),
        }
        if rule:
            props["Rule ID"] = _rich_text(rule)
        if file:
            props["File Path"] = _rich_text(file)
        if repository:
            props["Repository"] = {"select": {"name": repository}}
        if workflow_page_id:
            props["Workflow"] = {"relation": [{"id": workflow_page_id}]}
        if operator:
            props["Operator"] = _rich_text(operator)
        if is_new:
            props["Created At"] = _date(datetime.now().isoformat())
        return props


def _build_title(*, source: str, file: str | None, message: str) -> str:
    """`[source] file — first-line-of-message` 形式の表示タイトルを生成する。"""
    summary = (message or "").strip().splitlines()[0] if message else ""
    if len(summary) > 120:
        summary = summary[:117] + "..."
    if file:
        return f"[{source}] {file} — {summary}"
    return f"[{source}] {summary}"


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _rich_text(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def _date(iso_string: str) -> dict:
    return {"date": {"start": iso_string}}
