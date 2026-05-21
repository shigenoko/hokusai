"""ready_judgment.compute_ready_state の単体テスト（Issue #38 / Workgraph Phase 2）"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.integrations.notion_dashboard.ready_judgment import compute_ready_state

# ---------------------------------------------------------------------------
# Terminal / active status は再判定しない
# ---------------------------------------------------------------------------


def test_in_progress_is_preserved():
    assert (
        compute_ready_state({"status": "in_progress", "dependency_page_ids": []})
        == "in_progress"
    )


def test_done_is_preserved():
    assert compute_ready_state({"status": "done"}) == "done"


def test_skipped_is_preserved():
    assert compute_ready_state({"status": "skipped"}) == "skipped"


def test_canceled_is_preserved():
    assert compute_ready_state({"status": "canceled"}) == "canceled"


# ---------------------------------------------------------------------------
# 依存も blocker も無い → pending を温存
# ---------------------------------------------------------------------------


def test_no_deps_no_blockers_stays_pending():
    """依存も blocker も無い Work Item は pending を温存（自動 ready 化しない）"""
    wi = {"status": "pending", "dependency_page_ids": [], "blocking_review_issue_page_ids": []}
    assert compute_ready_state(wi) == "pending"


# ---------------------------------------------------------------------------
# 依存が全て done → ready
# ---------------------------------------------------------------------------


def test_all_deps_done_and_no_blockers_becomes_ready():
    wi = {"status": "pending", "dependency_page_ids": ["dep-1"]}
    lookup = {"dep-1": {"status": "done"}}
    assert compute_ready_state(
        wi, work_items_by_page_id=lookup
    ) == "ready"


def test_multiple_deps_all_done_becomes_ready():
    wi = {"status": "pending", "dependency_page_ids": ["dep-1", "dep-2", "dep-3"]}
    lookup = {
        "dep-1": {"status": "done"},
        "dep-2": {"status": "done"},
        "dep-3": {"status": "done"},
    }
    assert compute_ready_state(wi, work_items_by_page_id=lookup) == "ready"


# ---------------------------------------------------------------------------
# 依存が未 done → blocked
# ---------------------------------------------------------------------------


def test_dep_not_done_becomes_blocked():
    wi = {"status": "pending", "dependency_page_ids": ["dep-1"]}
    lookup = {"dep-1": {"status": "in_progress"}}
    assert compute_ready_state(wi, work_items_by_page_id=lookup) == "blocked"


def test_dep_pending_becomes_blocked():
    wi = {"status": "pending", "dependency_page_ids": ["dep-1"]}
    lookup = {"dep-1": {"status": "pending"}}
    assert compute_ready_state(wi, work_items_by_page_id=lookup) == "blocked"


def test_one_done_one_pending_becomes_blocked():
    wi = {"status": "pending", "dependency_page_ids": ["dep-1", "dep-2"]}
    lookup = {
        "dep-1": {"status": "done"},
        "dep-2": {"status": "pending"},
    }
    assert compute_ready_state(wi, work_items_by_page_id=lookup) == "blocked"


def test_dep_missing_from_lookup_is_blocked():
    """lookup に該当依存が無い場合は保守的に blocked（誤 ready 化を避ける）"""
    wi = {"status": "pending", "dependency_page_ids": ["dep-1"]}
    assert compute_ready_state(wi, work_items_by_page_id={}) == "blocked"


def test_no_lookup_provided_treats_deps_as_blocking():
    """lookup を渡さなければ依存解決不能 → blocked"""
    wi = {"status": "pending", "dependency_page_ids": ["dep-1"]}
    assert compute_ready_state(wi) == "blocked"


# ---------------------------------------------------------------------------
# blocker（Review Issue）の影響
# ---------------------------------------------------------------------------


def test_open_blocker_keeps_blocked_even_with_done_deps():
    wi = {
        "status": "pending",
        "dependency_page_ids": ["dep-1"],
        "blocking_review_issue_page_ids": ["ri-1"],
    }
    wi_lookup = {"dep-1": {"status": "done"}}
    ri_lookup = {"ri-1": {"status": "open"}}
    assert (
        compute_ready_state(
            wi,
            work_items_by_page_id=wi_lookup,
            review_issues_by_page_id=ri_lookup,
        )
        == "blocked"
    )


def test_resolved_blocker_does_not_block():
    wi = {
        "status": "pending",
        "dependency_page_ids": [],
        "blocking_review_issue_page_ids": ["ri-1"],
    }
    ri_lookup = {"ri-1": {"status": "resolved"}}
    # 依存無し + blocker は resolved → ready（blocker が一切 open でない）
    assert (
        compute_ready_state(wi, review_issues_by_page_id=ri_lookup) == "ready"
    )


def test_waived_blocker_does_not_block():
    wi = {
        "status": "pending",
        "blocking_review_issue_page_ids": ["ri-1"],
    }
    ri_lookup = {"ri-1": {"status": "waived"}}
    assert (
        compute_ready_state(wi, review_issues_by_page_id=ri_lookup) == "ready"
    )


def test_blocker_missing_from_lookup_treated_as_blocking():
    """blocker が lookup に無い → 状態不明 → 保守的に blocked"""
    wi = {
        "status": "pending",
        "blocking_review_issue_page_ids": ["ri-1"],
    }
    # blocker lookup なし
    assert compute_ready_state(wi, review_issues_by_page_id={}) == "blocked"


def test_no_review_lookup_provided_treats_blockers_as_blocking():
    """blocker があるのに lookup 未指定 → 解決不能 → blocked"""
    wi = {
        "status": "pending",
        "blocking_review_issue_page_ids": ["ri-1"],
    }
    assert compute_ready_state(wi) == "blocked"


# ---------------------------------------------------------------------------
# 入力 normalization
# ---------------------------------------------------------------------------


def test_status_missing_defaults_to_pending():
    """status キー欠落時は pending として再判定の対象になる"""
    wi = {"dependency_page_ids": [], "blocking_review_issue_page_ids": []}
    assert compute_ready_state(wi) == "pending"


def test_dependency_page_ids_with_invalid_entries_are_filtered():
    """非 str / 空文字 / None の依存は無視される"""
    wi = {
        "status": "pending",
        "dependency_page_ids": [None, "", "dep-1", 42],
    }
    lookup = {"dep-1": {"status": "done"}}
    # 実 dep は 1 件だけ → 全 done → ready
    assert compute_ready_state(wi, work_items_by_page_id=lookup) == "ready"


def test_dependency_page_ids_dict_input_is_ignored():
    """dict / Mapping を dependency_page_ids に渡しても、キーを page_id と
    誤解釈せず空 list 扱いになる（PR #41 Copilot 7 回目指摘）"""
    wi = {
        "status": "pending",
        # 誤って dict を渡してしまったケース
        "dependency_page_ids": {"dep-1": "done", "dep-2": "pending"},
    }
    # dict は無視されるので「依存無し / blocker 無し」→ pending 温存
    assert compute_ready_state(wi) == "pending"


def test_blocking_review_issue_page_ids_dict_input_is_ignored():
    """blocking_review_issue_page_ids も dict は無視（同上）"""
    wi = {
        "status": "pending",
        "blocking_review_issue_page_ids": {"ri-1": {"status": "open"}},
    }
    assert compute_ready_state(wi) == "pending"
