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

from typing import Iterable, Mapping

from .review_issues_db import STATUS_OPEN as REVIEW_ISSUE_STATUS_OPEN
from .work_items_db import (
    STATUS_BLOCKED,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SKIPPED,
)

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
) -> str:
    """1 件の Work Item に対する判定 status を返す。

    Args:
        work_item: 判定対象 Work Item。dict 形式で以下を持つ想定:
            {
                "status": str,
                "dependency_page_ids": list[str],
                "blocking_review_issue_page_ids": list[str],
            }
        work_items_by_page_id: 依存 Work Item を page_id で引ける lookup
        review_issues_by_page_id: blocking Review Issue を page_id で引ける
            lookup

    Returns:
        ready / blocked / pending / (元の status をそのまま) のいずれか。
        - 現 status が in_progress / done / skipped / canceled なら元のまま
          を返す（再判定しない）
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
