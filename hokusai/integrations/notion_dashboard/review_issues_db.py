"""Review Issues DB ドメインクライアント

Phase 6 verification failure / Phase 7 final review の指摘を Notion Review Issues
DB に構造化レコードとして同期する。後続の Policy Governance 違反 / LLM Gateway
ブロック / Copilot 指摘 / CI failure の共通受け皿となるよう、Source enum に将来
枠を最初から確保する。

設計方針:
- dedupe_key（source + repository + rule + file + message の hash）で重複を抑止し、
  既存レコードがあれば **Status / Created At を除く全プロパティ** を上書き更新
  する upsert を提供する。
    - Created At: create 時のみ書き込み、Notion 側で初回作成時刻を温存
    - Status: create 時のみ初期値 open を書き込み、update 時は payload に含めない。
      人間が Notion 上で `waived` / `resolved` に書き換えた状態を、HOKUSAI 側の
      再 dispatch で `open` に巻き戻さないため（PR #37 Copilot レビュー対応）
    - その他（Title / Source / Severity / Message / Repository / Workflow /
      Dedupe Key / Operator / Rule ID / File Path / Last Updated）は最新の
      payload 内容で常に上書き
- Phase 6 verification failure では `Message` は error_output の先頭行のみだが、
  `Dedupe Key` は error_output 全文を hash 入力に使う。共通バナーで先頭行が
  衝突する別ケースを区別するため（Phase 7 final review では Message と dedupe
  入力は一致）。
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
    *,
    source: str,
    rule: str | None,
    file: str | None,
    message: str,
    repository: str | None = None,
) -> str:
    """source + repository + rule + file + message から決定的な dedupe_key を生成する。

    source / repository も hash 入力に含めることで、別 source / 別リポジトリが
    偶然同じ rule/file/message を生成しても別レコードとして扱う。Phase 6/7 の
    payload は file が None になるケースが多いため、repository を含めないと
    同一 rule + 同一 message を出す別リポジトリの指摘が同じ Notion ページに
    集約され、Repository フィールドが上書きで失われる問題が生じる
    （PR #37 Copilot 指摘）。`None` は空文字に正規化し、message は前後空白を
    取り除いた上で全長を使う（先頭だけだと別箇所の同種指摘が衝突する）。
    """
    parts = "\x1f".join(
        (
            source or "",
            repository or "",
            rule or "",
            file or "",
            (message or "").strip(),
        )
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
                source=source,
                rule=rule,
                file=file,
                message=message,
                repository=repository,
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
        """既存ページ ID の有無で create / update を切り替える単純な分岐。"""
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
            "Review Issues DB に '%s' プロパティが存在しないため除外して再試行",
            missing,
        )
        current_props.pop(missing, None)
        if not current_props:
            logger.warning("除外後にプロパティが空になったため処理を中断")
            raise exc

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
        # Created At と Last Updated は同一の datetime.now() を使う。
        # 別々に呼ぶと Created At が Last Updated より遅れて並びが逆転する
        # 可能性があるため（PR #37 Copilot 2 回目指摘）。
        now_iso = datetime.now().isoformat()
        props: dict[str, Any] = {
            "Title": _title(title),
            "Source": {"select": {"name": source}},
            "Severity": {"select": {"name": severity}},
            "Dedupe Key": _rich_text(dedupe_key),
            "Message": _rich_text(message),
            "Last Updated": _date(now_iso),
        }
        # Status は新規作成時のみ書き込む。再 dispatch で人手の waived /
        # resolved を default "open" に巻き戻さないため（PR #37 Copilot 2
        # 回目指摘）。状態遷移を Agent から行う必要が出てきたら、明示的な
        # state-transition API を新設する想定。
        if is_new:
            props["Status"] = {"select": {"name": status}}
            props["Created At"] = _date(now_iso)
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
        return props


def _build_title(*, source: str, file: str | None, message: str) -> str:
    """`[source] file — first-line-of-message` 形式の表示タイトルを生成する。"""
    stripped = (message or "").strip()
    # `"".splitlines()` は [] を返すので空文字判定後にインデックスする
    summary = stripped.splitlines()[0] if stripped else ""
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
