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
from collections.abc import Mapping
from dataclasses import dataclass
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


# Applies To multi_select の許容値（要件 §8.3）。Notion は未知の multi_select
# 値を自動作成してしまうため、ホワイトリストで弾いて DB schema の drift を
# 防ぐ。Phase は HOKUSAI workflow の 10 段階に対応（setup.py と完全一致）。
ALLOWED_APPLIES_TO_VALUES = frozenset(
    {f"phase{i}" for i in range(1, 11)}
)


def _normalize_applies_to(value: object) -> list[str]:
    """`applies_to` を `list[str]` に正規化する。

    - `None` / 空 → `[]`
    - `str` → 単一要素 `list`（`"phase1"` → `["phase1"]`）。素朴に
      `list(applies_to or [])` すると 1 文字ずつの list になり Notion 側で
      `Applies To` に大量の不正値を作ってしまう問題を防ぐ（Copilot 指摘）。
    - その他 iterable → str 要素のみを抜き出し、`ALLOWED_APPLIES_TO_VALUES`
      に含まれないものは除外して DB schema drift を防ぐ。
    """
    if value is None:
        return []
    if isinstance(value, str):
        candidates: list[str] = [value]
    elif isinstance(value, Mapping):
        # dict 等を渡されると iterate でキーが取れて silent に値が通ってしまう
        # ため、Iterable 経路の前で明示的に reject（Copilot 指摘 / ready_judgment
        # `_as_list` と同じ防御策）。
        return []
    elif isinstance(value, Iterable):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return []
    return [v for v in candidates if v in ALLOWED_APPLIES_TO_VALUES]


@dataclass(frozen=True)
class MemoryAudit:
    """Project Memory の audit / lifecycle フィールドをまとめた値オブジェクト。

    `upsert_memory` / `_build_properties` のパラメータ数を抑えるために導入
    （SonarCloud max-param=13 対策）。Approved By / Approved At / Expires At
    の意味的には audit log なので、まとめても可読性は下がらない。
    """

    approved_by: str | None = None
    approved_at: str | None = None
    expires_at: str | None = None


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
        applies_to: Iterable[str] | str | None = None,
        workflow_id: str | None = None,
        workflow_page_id: str | None = None,
        pull_request_page_id: str | None = None,
        audit: MemoryAudit | None = None,
        dedupe_key: str | None = None,
    ) -> dict:
        """Project Memory を upsert する（status は新規作成時のみ書き込み、
        update 時は温存）。

        `applies_to` は `str` / iterable / `None` を受け付け、内部で
        `_normalize_applies_to` により phase1〜phase10 ホワイトリストへ正規化
        される（Copilot 指摘: 単一 str を 1 文字ずつの list 化してしまう問題、
        Notion 側 multi_select 自動作成による schema drift を回避）。
        """
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
            applies_to=_normalize_applies_to(applies_to),
            workflow_page_id=workflow_page_id,
            pull_request_page_id=pull_request_page_id,
            audit=audit or MemoryAudit(),
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

    def list_active_memories(
        self,
        *,
        profile: str | None = None,
        phase: str | None = None,
        types: Iterable[str] | None = None,
        max_pages: int = 10,
    ) -> list[dict]:
        """Status == active な Memory レコードを fetch する（Workgraph Phase 6
        / Issue #48: `hokusai prime` 用）。

        サーバ側 filter は `Status == active` のみで絞り、profile / phase /
        types は client-side で post-process する（Applies To multi_select の
        「空配列 OR phase を含む」OR 条件を Notion filter で組むと複雑で API
        制約に当たりやすいため、シンプルさを優先）。

        Args:
            profile: 一致させる Profile 名。指定時は `Profile == profile`
                **または `Profile` 未設定** な memory を採用（global memory）。
            phase: 一致させる Applies To 値（例 `phase5`）。指定時は
                `Applies To` が空 **または `phase` を含む** memory を採用。
            types: 採用する Memory Type の集合。None なら全 Type を採用。
                ALL_MEMORY_TYPES に含まれない値は黙って除外する。
            max_pages: ページネーション安全上限。各 100 件 × 10 ページで
                通常案件はカバーできる想定（大量 active memory が想定外で
                溜まった場合の暴走防止）。上限到達時は warning ログを出して
                truncation を明示する（silent truncation 防止）。

        Returns:
            Notion page dict のリスト（`id` / `properties` を含む）。
            空ならゼロ件。途中ページで API 失敗した場合は **その時点までに
            取得済みの結果を返す**（部分結果保持: 後段の Agent prompt 注入
            が memory 全消失するより部分的にでも渡せた方が有用なため）。
            API 失敗 / max_pages 上限到達はいずれも warning / debug log で
            通知し、呼び出し側に例外は伝播しない。
        """
        valid_types = None
        if types is not None:
            valid_types = {t for t in types if t in ALL_MEMORY_TYPES}
            if not valid_types:
                # types を渡したのに 1 つも valid でなければ空が期待動作
                return []

        results: list[dict] = []
        start_cursor: str | None = None
        truncated = False
        for page_idx in range(max_pages):
            try:
                response = self._api.query_database(
                    self._database_id,
                    filter_={
                        "property": "Status",
                        "select": {"equals": MEMORY_STATUS_ACTIVE},
                    },
                    start_cursor=start_cursor,
                    page_size=100,
                )
            except Exception as e:
                # 運用で気付けるよう warning に上げる（docstring「warning /
                # debug log で通知」と整合: Copilot 指摘）。部分結果は保持
                # して呼び出し側に返す（後段の Agent prompt 注入で memory
                # 全消失を避けるため）。
                logger.warning(
                    "Project Memory DB list 失敗（部分結果 %d 件で続行）: %s",
                    len(results), e,
                )
                return results
            for page in response.get("results") or []:
                if _matches_memory_filters(
                    page, profile=profile, phase=phase, types=valid_types
                ):
                    results.append(page)
            if not response.get("has_more"):
                break
            start_cursor = response.get("next_cursor")
            if not start_cursor:
                break
            # 次 page を取りに行く直前で安全上限に達するかチェック
            if page_idx + 1 >= max_pages:
                truncated = True
                break

        if truncated:
            # max_pages 上限で打ち切ったことを明示（silent truncation 防止：
            # Copilot 指摘）。呼び出し側が部分結果と認識して上限調整 / DB
            # 整理（古い active を deprecated 化等）の判断材料にできる。
            logger.warning(
                "Project Memory DB list が max_pages=%d で打ち切られました "
                "（has_more=True のまま）。取得済み %d 件で返却します。",
                max_pages, len(results),
            )
        return results

    def find_handover_notes_for_workflow(
        self,
        workflow_page_id: str | None,
        *,
        profile: str | None = None,
        max_pages: int = 5,
    ) -> list[dict]:
        """指定 workflow page に紐づく active な `handover_note` を取得する
        （Workgraph Phase 7 / Issue #52 / 要件 §8.4 lookup rule）。

        サーバ側 filter は AND(Status=active, Type=handover_note, Workflow
        contains workflow_page_id) で絞る（list_active_memories と異なり
        Workflow relation 一致を強制）。profile は `_matches_memory_filters`
        と同じく client-side で「一致 OR Profile 未設定（global）」を採用。

        Args:
            workflow_page_id: Notion 上の旧 workflow ページ id（Supersedes
                経由で取得する）。空 / None なら空リスト即返却。
            profile: 一致させる Profile 名。None なら profile フィルタ無し。
            max_pages: ページネーション安全上限（`list_active_memories` と
                同じく truncation 時は warning）。
        Returns:
            Notion page dict のリスト。API 失敗時は部分結果を保持して返す
            （prime 注入で全消失より部分提供を優先する設計）。
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
                                "property": "Status",
                                "select": {"equals": MEMORY_STATUS_ACTIVE},
                            },
                            {
                                "property": "Type",
                                "select": {
                                    "equals": MEMORY_TYPE_HANDOVER_NOTE
                                },
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
                    "Project Memory DB handover_note list 失敗 "
                    "(部分結果 %d 件で続行): %s",
                    len(results), e,
                )
                return results
            for page in response.get("results") or []:
                if _matches_memory_filters(
                    page,
                    profile=profile,
                    phase=None,
                    types={MEMORY_TYPE_HANDOVER_NOTE},
                ):
                    results.append(page)
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
                "find_handover_notes_for_workflow が max_pages=%d で打ち切られました "
                "（取得済み %d 件で返却）",
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
        audit: MemoryAudit,
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
        if audit.approved_by:
            props["Approved By"] = _rich_text(audit.approved_by)
        if audit.approved_at:
            props["Approved At"] = _date(audit.approved_at)
        if audit.expires_at:
            props["Expires At"] = _date(audit.expires_at)
        return props


def _matches_memory_filters(
    page: dict,
    *,
    profile: str | None,
    phase: str | None,
    types: set[str] | None,
) -> bool:
    """`list_active_memories` の client-side filter helper。

    - `profile` 指定時: Profile == profile **または Profile が空**（global memory）
    - `phase` 指定時: Applies To が空 **または phase を含む**（global memory）
    - `types` 指定時: Type が types に含まれる

    profile / phase が空時の memory は「全範囲適用」とみなして採用する仕様
    （要件 §8.3: Applies To は「省略時 global」の意味付け）。
    """
    props = page.get("properties") or {}

    if types is not None:
        type_value = (
            (props.get("Type") or {}).get("select") or {}
        ).get("name")
        if type_value not in types:
            return False

    if profile is not None:
        profile_rt = (props.get("Profile") or {}).get("rich_text") or []
        # rich_text は装飾やメンションで複数 element に分割され得るため、
        # 先頭要素だけ読むと別 profile を global 扱い（空文字 = passthrough）
        # で誤って通過させる可能性がある。全 element の plain_text / text.content
        # を連結してから比較する（Copilot 指摘）。
        page_profile = _join_rich_text_text(profile_rt).strip()
        if page_profile and page_profile != profile:
            return False

    if phase is not None:
        applies = (
            (props.get("Applies To") or {}).get("multi_select") or []
        )
        if applies:
            names = {opt.get("name") for opt in applies}
            if phase not in names:
                return False
    return True


def _join_rich_text_text(items: list[dict]) -> str:
    """rich_text array の全要素から plain_text / text.content を連結する。

    `_matches_memory_filters` の Profile 比較で使う。複数 element 分割 /
    mention / equation 対応（Copilot 指摘）。prime_renderer 側にも同等の
    `_join_rich_text_items` があるが、依存方向（client → renderer）を逆に
    したくないので本 module 内に独立して持つ。
    """
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        plain = item.get("plain_text")
        if isinstance(plain, str) and plain:
            parts.append(plain)
            continue
        text = item.get("text")
        if isinstance(text, dict):
            content = text.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    return "".join(parts)


def _title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _rich_text(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def _date(iso_string: str) -> dict:
    return {"date": {"start": iso_string}}
