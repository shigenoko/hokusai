from hokusai.state import create_initial_state
from hokusai.utils.phase_page_templates import (
    build_phase_page_content,
    _derive_display_status,
)


class TestBuildPhasePageContent:
    def test_builds_uniform_phase2_page(self):
        state = create_initial_state(
            task_url="https://notion.so/workspace/task-aabbccdd11223344aabbccdd11223344",
            task_title="テストタスク",
            branch_name="feature/test",
        )
        state["phases"][2]["status"] = "in_progress"

        content = build_phase_page_content(
            state=state,
            phase=2,
            latest_document="## 事前調査結果\n\nテスト本文",
            source_phase="phase2_research",
        )

        assert "# Phase 2: 事前調査" in content
        assert "## フェーズ概要" in content
        assert "## 現在の判断" in content
        assert "## 進捗チェックリスト" in content
        assert "## 最新版ドキュメント" in content
        assert "## 最新レビュー結果" in content
        assert "## 次アクション" in content
        assert "## 事前調査結果" in content
        assert "調査漏れがないか" in content
        assert "phase2_research" in content
        assert "Display Status: `drafting`" in content
        assert "Recommended Action: `none`" in content

    def test_derives_review_and_revision_sections_from_state(self):
        state = create_initial_state(
            task_url="https://notion.so/workspace/task-aabbccdd11223344aabbccdd11223344",
            task_title="テストタスク",
            branch_name="feature/test",
        )
        state["phases"][3]["status"] = "failed"
        state["phases"][3]["error_message"] = "cross_review_blocked"
        state["current_phase"] = 3
        state["waiting_for_human"] = True
        state["cross_review_results"][3] = {
            "overall_assessment": "request_changes",
            "summary": "指摘あり",
            "findings": [{"severity": "major", "title": "設計見直し"}],
        }
        state["audit_log"].append(
            {
                "timestamp": "2026-03-08T10:00:00+09:00",
                "phase": 3,
                "action": "cross_review_completed",
                "result": "success",
                "details": {"assessment": "request_changes"},
                "error": None,
            }
        )

        content = build_phase_page_content(
            state=state,
            phase=3,
            latest_document="## 設計チェック\n\nテスト本文",
            source_phase="phase3_design",
        )

        assert "Display Status: `needs_human_check`" in content
        assert "Recommended Action: `request_changes`" in content
        assert "- Reviewer: `codex`" in content
        assert "[major] 設計見直し" in content
        assert "cross_review_completed" in content


class TestDeriveDisplayStatus:
    """_derive_display_status のフェーズ単位判定テスト"""

    def _make_state(self, **overrides):
        state = create_initial_state(
            task_url="https://notion.so/workspace/task-aabbccdd11223344aabbccdd11223344",
            task_title="テスト",
            branch_name="feature/test",
        )
        state.update(overrides)
        return state

    def test_other_phase_waiting_does_not_affect_failed_phase(self):
        """Phase 5 で待機中でも Phase 4 は failed のまま"""
        state = self._make_state(
            current_phase=5,
            waiting_for_human=True,
            human_input_request="作業計画が取得できません",
        )
        state["phases"][4]["status"] = "failed"
        state["phases"][4]["error_message"] = "work_plan_extraction_failed"
        state["phases"][5]["status"] = "in_progress"

        assert _derive_display_status(state, 4) == "failed"

    def test_cross_review_blocked_is_needs_human_check(self):
        """Phase 4 が cross_review_blocked のときだけ needs_human_check"""
        state = self._make_state(
            current_phase=4,
            waiting_for_human=True,
        )
        state["phases"][4]["status"] = "failed"
        state["phases"][4]["error_message"] = "cross_review_blocked"

        assert _derive_display_status(state, 4) == "needs_human_check"

    def test_failed_phase_with_review_only_stays_failed(self):
        """レビュー結果が残っていても cross_review_blocked でなければ failed"""
        state = self._make_state(
            current_phase=6,
            waiting_for_human=False,
        )
        state["phases"][4]["status"] = "failed"
        state["phases"][4]["error_message"] = "work_plan_extraction_failed"
        state["cross_review_results"][4] = {
            "overall_assessment": "request_changes",
            "findings": [{"severity": "major", "title": "old review"}],
        }

        assert _derive_display_status(state, 4) == "failed"

    def test_current_phase_human_wait_is_needs_human_check(self):
        """current_phase 自身が human wait のときは needs_human_check"""
        state = self._make_state(
            current_phase=3,
            waiting_for_human=True,
            human_input_request="確認が必要です",
        )
        state["phases"][3]["status"] = "failed"
        state["phases"][3]["error_message"] = "some_other_reason"

        assert _derive_display_status(state, 3) == "needs_human_check"
