"""Work Items DB の ready 判定エンジン（Issue #38 / Workgraph Phase 2）

要件: 依存 Work Item がすべて `done` かつ blocking_review_issues に `open` の
ものがなければ `ready`。それ以外は `blocked`（依存が積まれているが何かが阻害
している状態）または `pending`（依存も blocker も無い初期状態を温存）。

設計判断:
- 本モジュールは **pure 関数** のみで Notion API を直接呼ばない。Work Item /
  Review Issue の現状は呼び出し側が pre-fetch した dict 構造で渡す前提とする。
  Notion API 呼び出し層は dispatcher / Operations Console 側で持つ。
- 状態は work_items_db.py の STATUS_* と完全一致させる。値の更新は同時に
  両ファイルで揃える。
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping

from .review_issues_db import STATUS_OPEN as REVIEW_ISSUE_STATUS_OPEN
from .work_items_db import (
    LEASE_STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SKIPPED,
)
from .workflow_gates_db import BLOCKING_GATE_STATUSES

# Review Issue の status enum。review_issues_db.STATUS_OPEN を直接参照する
# ことで、Review Issues DB 側で enum 値が変わったときに自動追従する
# （PR #41 Copilot 1 回目指摘で drift 防止のため string literal を撤廃）。
_BLOCKING_REVIEW_ISSUE_STATUSES = frozenset({REVIEW_ISSUE_STATUS_OPEN})

# Work Item が「進行中／完了側」と見なされる status。これらは ready 判定の
# 対象から外す（既に in_progress 以降は ready / blocked を再評価しない）。
# work_items_db の STATUS_* 定数を直接参照し、enum 値変更時に自動追従する。
_TERMINAL_OR_ACTIVE_STATUSES = frozenset({
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_SKIPPED,
    STATUS_CANCELED,
})


def compute_ready_state(
    work_item: Mapping[str, object],
    *,
    work_items_by_page_id: Mapping[str, Mapping[str, object]] | None = None,
    review_issues_by_page_id: Mapping[str, Mapping[str, object]] | None = None,
    gates_by_page_id: Mapping[str, Mapping[str, object]] | None = None,
) -> str:
    """1 件の Work Item に対する判定 status を返す。

    Args:
        work_item: 判定対象 Work Item。dict 形式で以下を持つ想定:
            {
                "status": str,
                "dependency_page_ids": list[str],
                "blocking_review_issue_page_ids": list[str],
                "gate_page_ids": list[str],  # 関連 Workflow Gate page_ids（Phase 4 / #44）
            }
        work_items_by_page_id: 依存 Work Item を page_id で引ける lookup
        review_issues_by_page_id: blocking Review Issue を page_id で引ける
            lookup
        gates_by_page_id: 関連 Workflow Gate を page_id で引ける lookup。
            **work_item に `gate_page_ids` が無い場合は不要**（gate 不要扱い、
            既存呼び出しの後方互換）。`gate_page_ids` がある場合に lookup が
            未指定 / 空 / 該当 page id が無いなら、状態不明として保守的に
            blocked を返す（誤 ready 化を防ぐため）。指定 lookup の gate の
            `status` が `pending` / `blocked`（BLOCKING_GATE_STATUSES）の
            いずれかなら blocked を返す（要件 §7.5）。

    Returns:
        ready / blocked / pending / (元の status をそのまま) のいずれか。
        - 現 status が in_progress / done / skipped / canceled なら元のまま
          を返す（再判定しない）
        - active 未期限 lease があれば in_progress 相当（Phase 3 / #42）
        - 関連 gate が pending/blocked なら blocked（Phase 4 / #44 / §7.5）
        - 依存に「done でないもの」または blocker に「open のもの」があれば
          blocked
        - 依存・blocker が一切無ければ pending を温存（依存追加前の初期状態
          を変更しない）
        - 依存・blocker があり、すべて done / non-open なら ready
    """
    current_status = str(work_item.get("status") or STATUS_PENDING)
    if current_status in _TERMINAL_OR_ACTIVE_STATUSES:
        # 既に進行中／完了側は再判定対象外（Phase 5 が決めた状態を温存）
        return current_status

    # **Active lease check（Workgraph Phase 3 / Issue #42）**:
    # 要件 §4.5 で「active な lease が存在しない」を ready 条件に含む。
    # Lease Status=active かつ Lease Expires At > now の Work Item は
    # 別 Agent が処理中とみなし、in_progress 相当として扱う（再 claim を
    # 防ぐ）。期限切れの active lease は無視（再割当可能な状態）。
    if _has_active_unexpired_lease(work_item):
        return STATUS_IN_PROGRESS

    # **Workflow Gate check（Workgraph Phase 4 / Issue #44 / 要件 §7.5）**:
    # 関連 gate のいずれかが `pending` または `blocked` なら、対象 Work Item は
    # 先に進めない。`open` / `not_required` / `expired` / `canceled` は阻害
    # しない（expired は再 claim 可能、canceled は不要扱い）。
    gate_ids = _as_list(work_item.get("gate_page_ids"))
    if gate_ids and _has_blocking_gate(gate_ids, gates_by_page_id):
        return STATUS_BLOCKED

    dep_ids = _as_list(work_item.get("dependency_page_ids"))
    block_ids = _as_list(work_item.get("blocking_review_issue_page_ids"))

    # 依存・blocker が一切無ければ pending のまま（明示的に何かを待っている
    # わけではないので ready 昇格はしない。Operations Console が手動で
    # ready 化するか、Phase 5 開始時に in_progress に遷移する想定）。
    if not dep_ids and not block_ids:
        return STATUS_PENDING

    if _has_unblocking_dependency(dep_ids, work_items_by_page_id):
        return STATUS_READY if not _has_open_blocker(
            block_ids, review_issues_by_page_id
        ) else STATUS_BLOCKED
    return STATUS_BLOCKED


def _has_active_unexpired_lease(work_item: Mapping[str, object]) -> bool:
    """Work Item に active な未期限 lease があるかを返す（Workgraph Phase 3）。

    判定:
    - `lease_status` キーが `active`（LEASE_STATUS_ACTIVE）でない → False
    - `lease_expires_at` が None / 空 / 不正 ISO → 「期限不明 = 期限切れ扱い」で False
      （安全側に倒すと「lease あり扱い」だが、不明な lease で永久に in_progress
      に固着するリスクを避けるため expire 側に倒す）
    - `lease_expires_at` を ISO 8601 で parse、now より過去 → False
    - now より未来 → True

    呼び出し側（compute_ready_state）は work_item dict から直接渡される想定。
    Notion 側プロパティは `Lease Status` / `Lease Expires At` だが、呼び出し
    側が事前に `lease_status` / `lease_expires_at` の lowercase キーで dict 化
    して渡す前提（Notion API レスポンスのパース層が責任を持つ）。
    """
    lease_status = str(work_item.get("lease_status") or "").lower()
    if lease_status != LEASE_STATUS_ACTIVE:
        return False
    expires_raw = work_item.get("lease_expires_at")
    if not expires_raw or not isinstance(expires_raw, str):
        return False
    try:
        expires = datetime.fromisoformat(expires_raw)
    except (ValueError, TypeError):
        return False
    # Notion から返る ISO 文字列はタイムゾーン付き（`+00:00` 等）になり
    # 得るため、tz-aware と tz-naive の混在比較で TypeError にならないよう、
    # 比較時の `now` は `expires` の tzinfo に揃える（PR #43 Copilot 1
    # 回目指摘）。`expires.tzinfo is None` のときは従来通り tz-naive
    # `datetime.now()` で比較する。
    now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
    return expires > now


def _has_blocking_gate(
    gate_ids: list[str],
    gates_by_page_id: Mapping[str, Mapping[str, object]] | None,
) -> bool:
    """関連 gate のいずれかが pending / blocked なら True を返す
    （Workgraph Phase 4 / Issue #44 / 要件 §7.5）。

    判定:
    - gate_ids が空 → 常に False（gate 不要）
    - gates_by_page_id 未指定 → 保守的に True を返す（gate 状態が分から
      ない場合、進めるのは危険なので blocked 側に倒す）
    - gate が lookup に無い → 状態不明 → True（blocked 扱い）
    - lookup の gate の `status` が pending / blocked なら True
    - その他（open / not_required / expired / canceled）は False
    """
    if not gate_ids:
        return False
    if gates_by_page_id is None:
        # gate lookup 未提供 = ready 判定エンジンに gate 情報を渡していない
        # 場合の保守的扱い: gate が紐付いているのに状態が分からないなら
        # 「先に進めない」側に倒す（誤って ready に昇格させない）。
        return True
    for pid in gate_ids:
        gate = gates_by_page_id.get(pid)
        if gate is None:
            # lookup に無い → 状態不明 → 阻害扱い
            return True
        status = str(gate.get("status") or "").lower()
        if status in BLOCKING_GATE_STATUSES:
            return True
    return False


def _has_unblocking_dependency(
    dep_ids: list[str],
    work_items_by_page_id: Mapping[str, Mapping[str, object]] | None,
) -> bool:
    """全依存 Work Item が done なら True。1 つでも未 done があれば False。

    lookup に該当 page_id が無い場合は「状態不明 = まだ done と確定できない」
    と保守的に解釈して False を返す（Notion 同期遅延などで lookup が miss
    するケースで、誤って ready に昇格させない）。
    """
    if not dep_ids:
        return True
    if work_items_by_page_id is None:
        return False
    for pid in dep_ids:
        dep = work_items_by_page_id.get(pid)
        if dep is None:
            return False
        if str(dep.get("status") or "") != STATUS_DONE:
            return False
    return True


def _has_open_blocker(
    block_ids: list[str],
    review_issues_by_page_id: Mapping[str, Mapping[str, object]] | None,
) -> bool:
    """blocking_review_issue に 1 つでも open があれば True。"""
    if not block_ids:
        return False
    if review_issues_by_page_id is None:
        # lookup 無しなら blocker の状態判定不能 → 保守的に「blocked あり」扱い。
        # 誤って ready 昇格させないため。
        return True
    for pid in block_ids:
        issue = review_issues_by_page_id.get(pid)
        if issue is None:
            # 該当 issue が lookup に無い → 状態不明 → blocker と見なす
            return True
        status = str(issue.get("status") or "").lower()
        if status in _BLOCKING_REVIEW_ISSUE_STATUSES:
            return True
    return False


def _as_list(value: object) -> list[str]:
    """page_id list を安全に取り出す（None / 非 list-like / 非 str 要素を除外）。

    page_id は list[str] / tuple[str, ...] の形でしか受け付けない:
    - `str` / `bytes` は単一文字列として誤入力扱いで無視
    - `Mapping`（dict 等）は iterable だが iteration はキーを返すため、
      dict を渡されると静かにキーを page_id として誤解釈する事故になる
      （PR #41 Copilot 7 回目指摘）。明示的に除外する。
    - その他の Iterable（generator / set 等）は受け入れるが、要素が str
      かつ空でないものだけを残す
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return []
    if isinstance(value, Mapping):
        # dict / Mapping を iterable として消費するとキーが page_id 扱いに
        # なるため、明示的にエラー側に倒す（空 list を返して呼び出し側で
        # 「依存無し / blocker 無し」として判定させる方が安全）。
        return []
    if not isinstance(value, Iterable):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            result.append(item)
    return result
