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


# ---------------------------------------------------------------------------
# Active lease チェック（Workgraph Phase 3 / Issue #42 / 要件 §4.5）
# ---------------------------------------------------------------------------


def test_active_unexpired_lease_returns_in_progress():
    """lease_status=active かつ Expires At > now → in_progress 相当"""
    from datetime import datetime, timedelta

    future = (datetime.now() + timedelta(hours=1)).isoformat()
    wi = {
        "status": "pending",
        "lease_status": "active",
        "lease_expires_at": future,
        # 依存も blocker も無い（通常なら pending のまま）が、active lease で in_progress
        "dependency_page_ids": [],
        "blocking_review_issue_page_ids": [],
    }
    assert compute_ready_state(wi) == "in_progress"


def test_expired_active_lease_treated_as_no_lease():
    """lease_status=active でも Expires At < now なら期限切れ扱い → 通常判定にフォール
    バック（依存無 blocker 無 → pending）"""
    from datetime import datetime, timedelta

    past = (datetime.now() - timedelta(hours=1)).isoformat()
    wi = {
        "status": "pending",
        "lease_status": "active",
        "lease_expires_at": past,
        "dependency_page_ids": [],
        "blocking_review_issue_page_ids": [],
    }
    # 期限切れなので lease は無視、通常判定 → pending
    assert compute_ready_state(wi) == "pending"


def test_released_lease_does_not_block_ready_judgement():
    """lease_status=released は active ではないので通常判定に進む"""
    from datetime import datetime, timedelta

    future = (datetime.now() + timedelta(hours=1)).isoformat()
    wi = {
        "status": "pending",
        "lease_status": "released",
        "lease_expires_at": future,
    }
    # released は active 判定対象外 → 通常判定（依存も blocker も無い → pending）
    assert compute_ready_state(wi) == "pending"


def test_expired_lease_status_does_not_block():
    """lease_status=expired（明示的に expired マークされた）も active ではない"""
    from datetime import datetime, timedelta

    future = (datetime.now() + timedelta(hours=1)).isoformat()
    wi = {
        "status": "pending",
        "lease_status": "expired",
        "lease_expires_at": future,
    }
    assert compute_ready_state(wi) == "pending"


def test_active_lease_with_invalid_expires_at_falls_back_to_normal_judgement():
    """lease_expires_at が parse できない → lease なし扱いで通常判定"""
    wi = {
        "status": "pending",
        "lease_status": "active",
        "lease_expires_at": "not-an-iso-date",
    }
    assert compute_ready_state(wi) == "pending"


def test_active_lease_with_missing_expires_at_falls_back_to_normal_judgement():
    """lease_status=active でも lease_expires_at が無ければ lease なし扱い"""
    wi = {
        "status": "pending",
        "lease_status": "active",
        # lease_expires_at 欠落
    }
    assert compute_ready_state(wi) == "pending"


def test_active_lease_overrides_dependency_ready():
    """active lease がある場合は、依存が全 done でも in_progress を返す
    （別 Agent が claim 済みなので新たに ready に再昇格させない）"""
    from datetime import datetime, timedelta

    future = (datetime.now() + timedelta(hours=1)).isoformat()
    wi = {
        "status": "pending",
        "lease_status": "active",
        "lease_expires_at": future,
        "dependency_page_ids": ["dep-1"],
    }
    lookup = {"dep-1": {"status": "done"}}
    # 依存全 done でも active lease 優先 → in_progress
    assert compute_ready_state(wi, work_items_by_page_id=lookup) == "in_progress"


def test_active_lease_with_tz_aware_expires_at_does_not_raise():
    """lease_expires_at が tz-aware ISO（`...+00:00`）でも tz mismatch で
    TypeError にならない（PR #43 Copilot 1 回目指摘）"""
    from datetime import datetime, timedelta, timezone

    future_utc = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    wi = {
        "status": "pending",
        "lease_status": "active",
        "lease_expires_at": future_utc,
    }
    # 未期限なので in_progress を返す（TypeError で落ちないことが主目的）
    assert compute_ready_state(wi) == "in_progress"


def test_active_lease_with_tz_aware_expired_returns_pending():
    """tz-aware で past の Expires At も期限切れ扱い → 通常判定"""
    from datetime import datetime, timedelta, timezone

    past_utc = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    wi = {
        "status": "pending",
        "lease_status": "active",
        "lease_expires_at": past_utc,
    }
    assert compute_ready_state(wi) == "pending"
