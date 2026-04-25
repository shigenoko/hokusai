"""
format_cross_review_for_prompt() のユニットテスト + Phase 3/4 統合テスト
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hokusai.utils.cross_review import format_cross_review_for_prompt


# ---------------------------------------------------------------------------
# Helper: 最小限の WorkflowState を生成
# ---------------------------------------------------------------------------
def _make_state(cross_review_results: dict | None = None):
    """テスト用 state stub"""
    return {"cross_review_results": cross_review_results or {}}


def _make_review(
    assessment: str = "request_changes",
    confidence: int | None = 85,
    summary: str = "テストサマリー",
    findings: list | None = None,
):
    return {
        "overall_assessment": assessment,
        "confidence_score": confidence,
        "summary": summary,
        "findings": findings if findings is not None else [],
    }


# ---------------------------------------------------------------------------
# フォーマッター テスト
# ---------------------------------------------------------------------------
class TestFormatCrossReviewForPrompt:
    def test_no_results_returns_empty(self):
        state = _make_state({})
        assert format_cross_review_for_prompt(state, source_phase=2) == ""

    def test_no_findings_returns_empty(self):
        state = _make_state({2: _make_review(findings=[])})
        assert format_cross_review_for_prompt(state, source_phase=2) == ""

    def test_missing_phase_returns_empty(self):
        state = _make_state({4: _make_review(findings=[{"severity": "minor", "title": "x"}])})
        assert format_cross_review_for_prompt(state, source_phase=2) == ""

    def test_basic_formatting(self):
        findings = [
            {
                "severity": "critical",
                "title": "オフライン対応未考慮",
                "description": "説明テキスト",
                "suggestion": "提案テキスト",
            },
        ]
        state = _make_state({2: _make_review(findings=findings)})
        result = format_cross_review_for_prompt(state, source_phase=2)

        assert "Phase 2: 事前調査" in result
        assert "request_changes" in result
        assert "confidence: 85%" in result
        assert "テストサマリー" in result
        assert "[critical] オフライン対応未考慮" in result
        assert "説明テキスト" in result
        assert "提案: 提案テキスト" in result

    def test_severity_ordering(self):
        findings = [
            {"severity": "info", "title": "info-item"},
            {"severity": "critical", "title": "critical-item"},
            {"severity": "minor", "title": "minor-item"},
            {"severity": "major", "title": "major-item"},
        ]
        state = _make_state({2: _make_review(findings=findings)})
        result = format_cross_review_for_prompt(state, source_phase=2)

        idx_critical = result.index("critical-item")
        idx_major = result.index("major-item")
        idx_minor = result.index("minor-item")
        idx_info = result.index("info-item")
        assert idx_critical < idx_major < idx_minor < idx_info

    def test_no_confidence_score(self):
        findings = [{"severity": "major", "title": "テスト"}]
        state = _make_state({2: _make_review(confidence=None, findings=findings)})
        result = format_cross_review_for_prompt(state, source_phase=2)

        assert "confidence" not in result
        assert "request_changes" in result

    def test_instruction_line_present(self):
        findings = [{"severity": "minor", "title": "テスト"}]
        state = _make_state({2: _make_review(findings=findings)})
        result = format_cross_review_for_prompt(state, source_phase=2)

        assert result.rstrip().endswith(
            "上記のクロスLLMレビュー指摘事項を考慮して開発計画を作成してください。"
        )


# ---------------------------------------------------------------------------
# Phase 4 統合テスト
# ---------------------------------------------------------------------------
class TestPhase4CrossReviewIntegration:
    """Phase 4 の dev-plan 実行時に cross-review コンテキストが付与されることを検証"""

    def _build_state(self, cross_review_results=None):
        """Phase 4 実行に必要な最小限の state"""
        from hokusai.state import create_initial_state

        state = create_initial_state(
            task_url="https://example.com/task/123",
            task_title="テストタスク",
            branch_name="feature/test",
        )
        if cross_review_results:
            state["cross_review_results"] = cross_review_results
        return state

    @patch("hokusai.nodes.phase4_plan.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase4_plan.save_to_subpage_or_create", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase4_plan.ClaudeCodeClient")
    def test_cross_review_context_appended_to_args(
        self, MockClient, mock_save, mock_cross_review,
    ):
        from hokusai.nodes.phase4_plan import phase4_plan_node

        findings = [
            {"severity": "critical", "title": "セキュリティ問題", "description": "XSS脆弱性"},
        ]
        state = self._build_state(
            cross_review_results={2: _make_review(findings=findings)},
        )

        mock_instance = MagicMock()
        mock_instance.execute_skill.return_value = {"output": "## 開発計画\n- [ ] **1.1** API変更\n- [ ] **1.2** 実装\n- [ ] **2.0** テスト"}
        MockClient.return_value = mock_instance

        phase4_plan_node(state)

        call_args = mock_instance.execute_skill.call_args
        args_value = call_args.kwargs.get("args", call_args[1].get("args", ""))
        assert "https://example.com/task/123" in args_value
        assert "[critical] セキュリティ問題" in args_value

    @patch("hokusai.nodes.phase4_plan.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase4_plan.save_to_subpage_or_create", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase4_plan.ClaudeCodeClient")
    def test_no_cross_review_uses_url_only(
        self, MockClient, mock_save, mock_cross_review,
    ):
        from hokusai.nodes.phase4_plan import phase4_plan_node

        state = self._build_state()

        mock_instance = MagicMock()
        mock_instance.execute_skill.return_value = {"output": "## 開発計画\n- [ ] **1.1** API変更\n- [ ] **1.2** 実装\n- [ ] **2.0** テスト"}
        MockClient.return_value = mock_instance

        phase4_plan_node(state)

        call_args = mock_instance.execute_skill.call_args
        args_value = call_args.kwargs.get("args", call_args[1].get("args", ""))
        assert args_value == "https://example.com/task/123"


# ---------------------------------------------------------------------------
# Phase 3 統合テスト
# ---------------------------------------------------------------------------
class TestPhase3CrossReviewIntegration:
    """Phase 3 の design-check 実行時に cross-review コンテキストが付与されることを検証"""

    def _build_state(self, cross_review_results=None, research_result="## 調査結果\nテスト調査"):
        from hokusai.state import create_initial_state

        state = create_initial_state(
            task_url="https://example.com/task/123",
            task_title="テストタスク",
            branch_name="feature/test",
        )
        state["research_result"] = research_result
        if cross_review_results:
            state["cross_review_results"] = cross_review_results
        return state

    @patch("hokusai.nodes.phase3_design._validate_design_output")
    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_cross_review_context_in_prompt(
        self, MockClient, mock_save, mock_cross_review, mock_verify_content,
        mock_validate_output,
    ):
        from hokusai.nodes.phase3_design import phase3_design_node

        findings = [
            {"severity": "major", "title": "パフォーマンス問題", "description": "N+1クエリ"},
        ]
        state = self._build_state(
            cross_review_results={2: _make_review(findings=findings)},
        )

        mock_instance = MagicMock()
        mock_instance.execute_prompt.return_value = "## 設計チェック\nテスト設計"
        MockClient.return_value = mock_instance

        phase3_design_node(state)

        # execute_prompt に渡されたプロンプトにタスクURLとクロスレビュー指摘が含まれる
        prompt_arg = mock_instance.execute_prompt.call_args.kwargs.get("prompt") or mock_instance.execute_prompt.call_args[0][0]
        assert "https://example.com/task/123" in prompt_arg
        assert "[major] パフォーマンス問題" in prompt_arg

    @patch("hokusai.nodes.phase3_design._validate_design_output")
    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_no_cross_review_prompt_has_url(
        self, MockClient, mock_save, mock_cross_review, mock_verify_content,
        mock_validate_output,
    ):
        from hokusai.nodes.phase3_design import phase3_design_node

        state = self._build_state()

        mock_instance = MagicMock()
        mock_instance.execute_prompt.return_value = "## 設計チェック\nテスト設計"
        MockClient.return_value = mock_instance

        phase3_design_node(state)

        # execute_prompt に渡されたプロンプトにタスクURLが含まれる
        prompt_arg = mock_instance.execute_prompt.call_args.kwargs.get("prompt") or mock_instance.execute_prompt.call_args[0][0]
        assert "https://example.com/task/123" in prompt_arg

    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_no_research_result_skips_prompt(self, MockClient, mock_cross_review):
        from hokusai.nodes.phase3_design import phase3_design_node

        state = self._build_state(research_result=None)

        phase3_design_node(state)

        MockClient.assert_not_called()
