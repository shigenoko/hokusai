"""Prime v2 MVP-4 / MVP-5: gap analysis detector の単体テスト

docs/design-prime-v2.md §6.1 / §8.1 の決定的 gap detector を検証する:
- MVP-4: `unresolved_review_issue_open` / `notion_outbox_pending` /
  `audit_log_silence`
- MVP-5 (Notion 非依存、state/SQLite-backed): `phase4_plan_missing` /
  `supersedes_chain_broken`

CLI handler との結合テストは test_prime_v2_query.py に集約。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hokusai.persistence.sqlite_store import SQLiteStore
from hokusai.prime_gaps import (
    Gap,
    collect_gaps,
    detect_audit_log_silence,
    detect_notion_outbox_pending,
    detect_phase4_plan_missing,
    detect_supersedes_chain_broken,
    detect_unresolved_review_issues,
)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "wf.db")


def _review_issue_page(title: str, severity: str = "blocker") -> dict:
    return {
        "id": f"page-{title}",
        "properties": {
            "Title": {"title": [{"plain_text": title}]},
            "Severity": {"select": {"name": severity}},
        },
    }


# ---------------------------------------------------------------------------
# detect_unresolved_review_issues
# ---------------------------------------------------------------------------


def test_review_issue_none_returns_no_gaps():
    assert detect_unresolved_review_issues(None) == []


def test_review_issue_empty_returns_no_gaps():
    assert detect_unresolved_review_issues([]) == []


def test_review_issue_each_becomes_one_gap():
    issues = [
        _review_issue_page("bug A", severity="blocker"),
        _review_issue_page("bug B", severity="warning"),
    ]
    gaps = detect_unresolved_review_issues(issues)
    assert len(gaps) == 2
    assert all(g.kind == "unresolved_review_issue_open" for g in gaps)
    assert "bug A" in gaps[0].detail
    assert "blocker" in gaps[0].detail
    assert "bug B" in gaps[1].detail


def test_review_issue_handles_missing_title_and_severity():
    issues = [{"id": "x", "properties": {}}]  # no Title / Severity
    gaps = detect_unresolved_review_issues(issues)
    assert len(gaps) == 1
    assert "(untitled review issue)" in gaps[0].detail
    assert "severity=?" in gaps[0].detail


# ---------------------------------------------------------------------------
# detect_notion_outbox_pending
# ---------------------------------------------------------------------------


def test_outbox_empty_returns_no_gaps(store: SQLiteStore):
    assert detect_notion_outbox_pending(store) == []


def test_outbox_pending_returns_one_gap(store: SQLiteStore):
    # outbox に 1 件 enqueue
    store.enqueue_notion_sync(
        idempotency_key="key1", workflow_id="wf-1",
        event_type="workflow_started", payload={},
    )
    gaps = detect_notion_outbox_pending(store)
    assert len(gaps) == 1
    assert gaps[0].kind == "notion_outbox_pending"
    assert "1 件" in gaps[0].detail
    assert "Operations Console" in gaps[0].detail


def test_outbox_store_exception_returns_no_gaps():
    """store API 失敗時は best-effort で空を返す"""

    class _BrokenStore:
        def count_notion_sync_pending(self):
            raise RuntimeError("simulated outage")

    assert detect_notion_outbox_pending(_BrokenStore()) == []


# ---------------------------------------------------------------------------
# detect_audit_log_silence
# ---------------------------------------------------------------------------


def test_audit_silence_skipped_when_gateway_disabled(store: SQLiteStore):
    assert detect_audit_log_silence(store, llm_gateway_enabled=False) == []


def test_audit_silence_zero_rows_returns_gap(store: SQLiteStore):
    """gateway enabled で audit_logs が空なら gap が出る"""
    gaps = detect_audit_log_silence(store, llm_gateway_enabled=True)
    assert len(gaps) == 1
    assert gaps[0].kind == "audit_log_silence"
    assert "全 workflow" in gaps[0].detail


def test_audit_silence_with_workflow_id_scope_message(store: SQLiteStore):
    gaps = detect_audit_log_silence(
        store, llm_gateway_enabled=True, workflow_id="wf-1"
    )
    assert len(gaps) == 1
    assert "workflow wf-1" in gaps[0].detail


def test_audit_silence_non_empty_returns_no_gap(store: SQLiteStore):
    """audit_logs に 1 件でもあれば silence ではない"""
    store.add_audit_log(
        workflow_id="wf-1", phase=2, action="llm_gateway_decision",
        status="log", details={},
    )
    assert detect_audit_log_silence(store, llm_gateway_enabled=True) == []


def test_audit_silence_store_exception_returns_no_gaps():
    """store 例外時は best-effort で空"""

    class _BrokenStore:
        def list_audit_logs(self, **kwargs):
            raise RuntimeError("simulated outage")

    assert detect_audit_log_silence(
        _BrokenStore(), llm_gateway_enabled=True
    ) == []


# ---------------------------------------------------------------------------
# detect_phase4_plan_missing (MVP-5, state-backed)
# ---------------------------------------------------------------------------


def test_phase4_plan_none_state_returns_no_gaps():
    assert detect_phase4_plan_missing(None) == []


def test_phase4_plan_non_int_phase_returns_no_gaps():
    # current_phase が文字列等で int でない場合は skip
    assert detect_phase4_plan_missing({"current_phase": "phase5"}) == []


def test_phase4_plan_below_threshold_returns_no_gaps():
    # Phase 4 以前は計画生成中とみなして検出しない (>= 5 が閾値)
    assert detect_phase4_plan_missing(
        {"current_phase": 4, "work_plan": None}
    ) == []
    assert detect_phase4_plan_missing(
        {"current_phase": 2, "work_plan": None}
    ) == []


def test_phase4_plan_missing_at_phase5_returns_gap():
    gaps = detect_phase4_plan_missing(
        {"current_phase": 5, "work_plan": None}
    )
    assert len(gaps) == 1
    assert gaps[0].kind == "phase4_plan_missing"
    assert gaps[0].phase == 4
    assert "current_phase=5" in gaps[0].detail


def test_phase4_plan_blank_string_is_missing():
    gaps = detect_phase4_plan_missing(
        {"current_phase": 7, "work_plan": "   "}
    )
    assert len(gaps) == 1
    assert gaps[0].kind == "phase4_plan_missing"


def test_phase4_plan_present_returns_no_gap():
    # work_plan が非空なら gap なし (実 workflow wf-dbe7b6cd 相当)
    assert detect_phase4_plan_missing(
        {"current_phase": 7, "work_plan": "## 開発計画\n- step 1"}
    ) == []


# ---------------------------------------------------------------------------
# detect_supersedes_chain_broken (MVP-5, state + SQLite-backed)
# ---------------------------------------------------------------------------


def test_supersedes_none_state_returns_no_gaps(store: SQLiteStore):
    assert detect_supersedes_chain_broken(store, None) == []


def test_supersedes_no_supersedes_id_returns_no_gaps(store: SQLiteStore):
    assert detect_supersedes_chain_broken(
        store, {"supersedes_workflow_id": None}
    ) == []


def test_supersedes_healthy_chain_returns_no_gap(store: SQLiteStore):
    # supersedes 元 wf が notion_sync_errors に無ければ chain は健全
    assert detect_supersedes_chain_broken(
        store, {"supersedes_workflow_id": "wf-old"}
    ) == []


def test_supersedes_broken_chain_returns_gap(store: SQLiteStore):
    # supersedes 元 wf の workflow_started が errors に flush 済み → chain 切れ
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-old:workflow_started",
        workflow_id="wf-old",
        event_type="workflow_started",
        payload={},
        error="404 Not Found",
    )
    gaps = detect_supersedes_chain_broken(
        store, {"supersedes_workflow_id": "wf-old"}
    )
    assert len(gaps) == 1
    assert gaps[0].kind == "supersedes_chain_broken"
    assert "wf-old" in gaps[0].detail


def test_supersedes_store_exception_returns_no_gaps():
    class _BrokenStore:
        def has_failed_workflow_started(self, wf):
            raise RuntimeError("simulated outage")

    assert detect_supersedes_chain_broken(
        _BrokenStore(), {"supersedes_workflow_id": "wf-old"}
    ) == []


# ---------------------------------------------------------------------------
# collect_gaps + Gap.to_dict
# ---------------------------------------------------------------------------


def test_gap_to_dict_shape():
    g = Gap(kind="x", detail="y", phase=3)
    assert g.to_dict() == {"kind": "x", "phase": 3, "detail": "y"}


def test_collect_gaps_combines_all_three(store: SQLiteStore):
    """MVP-4 の 3 detector すべてが gap を出すケースで合計件数を確認
    (state 未指定なら MVP-5 detector は発火しない)"""
    store.enqueue_notion_sync(
        idempotency_key="k1", workflow_id="wf-1",
        event_type="workflow_started", payload={},
    )
    gaps = collect_gaps(
        store=store,
        review_issues=[_review_issue_page("bug A")],
        llm_gateway_enabled=True,
        workflow_id="wf-1",
    )
    kinds = sorted(g.kind for g in gaps)
    assert kinds == [
        "audit_log_silence",
        "notion_outbox_pending",
        "unresolved_review_issue_open",
    ]


def test_collect_gaps_includes_mvp5_when_state_given(store: SQLiteStore):
    """state を渡すと MVP-5 の 2 detector も合流する"""
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-old:workflow_started",
        workflow_id="wf-old",
        event_type="workflow_started",
        payload={},
        error="404",
    )
    gaps = collect_gaps(
        store=store,
        review_issues=None,
        llm_gateway_enabled=False,
        workflow_id="wf-1",
        state={
            "current_phase": 6,
            "work_plan": None,
            "supersedes_workflow_id": "wf-old",
        },
    )
    kinds = sorted(g.kind for g in gaps)
    assert kinds == ["phase4_plan_missing", "supersedes_chain_broken"]


def test_collect_gaps_empty_environment_returns_empty(store: SQLiteStore):
    gaps = collect_gaps(
        store=store,
        review_issues=None,
        llm_gateway_enabled=False,
        workflow_id=None,
    )
    assert gaps == []
