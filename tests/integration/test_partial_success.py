"""
Integration tests for partial success scenarios

部分的成功シナリオの統合テスト
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, patch

from hokusai.state import (
    WorkflowState,
    PhaseStatus,
    VerificationResult,
    VerificationErrorEntry,
    PhaseState,
    PullRequestInfo,
    PRStatus,
    RepositoryPhaseStatus,
    init_repository_state,
    get_repository_state,
    update_repository_phase_status,
    get_pending_repositories,
)
from hokusai.nodes.phase8 import (
    _is_repository_successful,
    _mark_successful_prs_ready,
)


@pytest.fixture
def partial_success_state() -> WorkflowState:
    """部分的成功シナリオ用のワークフロー状態

    Backend: Phase 6, 7 完了（成功）
    API: Phase 6 完了、Phase 7 失敗（部分的成功）
    """
    now = datetime.now().isoformat()
    phases = {}
    for i in range(1, 11):
        phases[i] = PhaseState(
            status=PhaseStatus.PENDING.value,
            started_at=None,
            completed_at=None,
            error_message=None,
            retry_count=0,
        )

    # Backend: 成功
    backend = init_repository_state("Backend", "/path/backend", "feature/test", "main")
    backend["phase_status"] = {
        6: RepositoryPhaseStatus.COMPLETED.value,
        7: RepositoryPhaseStatus.COMPLETED.value,
    }

    # API: 部分的成功（Phase 7 失敗）
    api = init_repository_state("API", "/path/api", "feature/test", "main")
    api["phase_status"] = {
        6: RepositoryPhaseStatus.COMPLETED.value,
        7: RepositoryPhaseStatus.FAILED.value,
    }

    return WorkflowState(
        workflow_id="wf-partial-001",
        task_url="https://notion.so/test-task",
        task_title="Partial Success Test",
        branch_name="feature/test",
        base_branch="main",
        current_phase=8,
        phases=phases,
        schema_change_required=False,
        schema_pr_url=None,
        schema_pr_merged=False,
        pull_requests=[
            PullRequestInfo(
                repo_name="Backend",
                title="Backend PR",
                url="https://github.com/test/backend/pull/1",
                number=1,
                owner="test",
                repo="backend",
                status=PRStatus.DRAFT.value,
                github_status="draft",
            ),
            PullRequestInfo(
                repo_name="API",
                title="API PR",
                url="https://github.com/test/api/pull/2",
                number=2,
                owner="test",
                repo="api",
                status=PRStatus.DRAFT.value,
                github_status="draft",
            ),
        ],
        current_pr_index=0,
        verification={
            "build": VerificationResult.PASS.value,
            "test": VerificationResult.PASS.value,
            "lint": VerificationResult.PASS.value,
        },
        verification_errors=[],
        repository_status={},
        repositories=[backend, api],
        final_review_passed=False,
        final_review_issues=["[API] console.log found"],
        final_review_rules={},
        final_review_by_repo={
            "Backend": {"passed": True, "issues": []},
            "API": {"passed": False, "issues": ["console.log found"]},
        },
        work_plan=None,
        implementation_result=None,
        expected_changed_files=[],
        branch_hygiene_issues=[],
        cherry_picked_from=None,
        cherry_picked_commits=[],
        created_at=now,
        updated_at=now,
        total_retry_count=0,
        audit_log=[],
        waiting_for_human=False,
        human_input_request=None,
        last_environment_error=None,
        waiting_for_pr_approval=False,
        pr_ready_for_review=False,
        waiting_for_copilot_review=False,
        copilot_review_passed=False,
        copilot_review_comments=[],
        copilot_fix_requested=False,
        waiting_for_human_review=False,
        human_review_passed=False,
        human_review_comments=[],
        human_fix_requested=False,
    )


class TestPartialSuccessScenario:
    """部分的成功シナリオのテスト"""

    def test_identify_successful_repository(self, partial_success_state: WorkflowState):
        """成功したリポジトリを識別できる"""
        state = partial_success_state

        # Backend は成功
        assert _is_repository_successful(state, "Backend") is True
        # API は失敗
        assert _is_repository_successful(state, "API") is False

    def test_mark_only_successful_prs_ready(self, partial_success_state: WorkflowState):
        """全PRがDraftのまま維持される（Ready for Reviewは人間が手動で行う）"""
        state = partial_success_state

        ready_prs, draft_prs = _mark_successful_prs_ready(state)

        # 全PRがDraftのまま
        assert len(ready_prs) == 0
        assert len(draft_prs) == 2

    def test_pending_repositories_for_retry(self, partial_success_state: WorkflowState):
        """リトライ時に未完了リポジトリのみが対象になる"""
        state = partial_success_state

        # Phase 7 で未完了のリポジトリを取得
        pending = get_pending_repositories(state, 7)

        assert len(pending) == 1
        assert pending[0]["name"] == "API"


class TestRetryAfterPartialSuccess:
    """部分的成功後のリトライシナリオ"""

    def test_retry_updates_only_failed_repo(self, partial_success_state: WorkflowState):
        """リトライで失敗したリポジトリのみ状態が更新される"""
        state = partial_success_state

        # API を修正して Phase 7 を再実行
        state = update_repository_phase_status(
            state, "API", 7, RepositoryPhaseStatus.COMPLETED
        )

        # 両方のリポジトリが成功状態
        backend = get_repository_state(state, "Backend")
        api = get_repository_state(state, "API")

        assert backend["phase_status"][7] == RepositoryPhaseStatus.COMPLETED.value
        assert api["phase_status"][7] == RepositoryPhaseStatus.COMPLETED.value

    def test_all_repos_successful_after_retry(self, partial_success_state: WorkflowState):
        """リトライ後に全リポジトリが成功"""
        state = partial_success_state

        # API を修正
        state = update_repository_phase_status(
            state, "API", 7, RepositoryPhaseStatus.COMPLETED
        )

        # 両方成功判定
        assert _is_repository_successful(state, "Backend") is True
        assert _is_repository_successful(state, "API") is True

    def test_all_prs_draft_after_retry(self, partial_success_state: WorkflowState):
        """リトライ後も全PRがDraftのまま維持される（Ready for Reviewは人間が手動で行う）"""
        state = partial_success_state

        # API を修正
        state = update_repository_phase_status(
            state, "API", 7, RepositoryPhaseStatus.COMPLETED
        )

        ready_prs, draft_prs = _mark_successful_prs_ready(state)

        # 全PRがDraftのまま
        assert len(ready_prs) == 0
        assert len(draft_prs) == 2


class TestVerificationErrorIsolation:
    """検証エラーの分離テスト"""

    def test_verification_errors_per_repository(self, partial_success_state: WorkflowState):
        """リポジトリごとに検証エラーが分離される"""
        state = partial_success_state

        # 検証エラーを追加（API のみ失敗）
        state["verification_errors"] = [
            VerificationErrorEntry(
                repository="Backend",
                command="build",
                success=True,
                error_output=None,
            ),
            VerificationErrorEntry(
                repository="Backend",
                command="test",
                success=True,
                error_output=None,
            ),
            VerificationErrorEntry(
                repository="API",
                command="build",
                success=True,
                error_output=None,
            ),
            VerificationErrorEntry(
                repository="API",
                command="test",
                success=False,
                error_output="Test failed: expected 1 but got 2",
            ),
        ]

        # Backend のエラーを抽出
        backend_errors = [
            e for e in state["verification_errors"]
            if e["repository"] == "Backend" and not e["success"]
        ]
        assert len(backend_errors) == 0

        # API のエラーを抽出
        api_errors = [
            e for e in state["verification_errors"]
            if e["repository"] == "API" and not e["success"]
        ]
        assert len(api_errors) == 1
        assert "Test failed" in api_errors[0]["error_output"]


class TestReviewIssueIsolation:
    """レビューissueの分離テスト"""

    def test_review_issues_per_repository(self, partial_success_state: WorkflowState):
        """リポジトリごとにレビューissueが分離される"""
        state = partial_success_state

        # Backend のレビュー結果
        backend_result = state["final_review_by_repo"]["Backend"]
        assert backend_result["passed"] is True
        assert len(backend_result["issues"]) == 0

        # API のレビュー結果
        api_result = state["final_review_by_repo"]["API"]
        assert api_result["passed"] is False
        assert len(api_result["issues"]) == 1

    def test_aggregate_issues_have_repo_prefix(self, partial_success_state: WorkflowState):
        """集約されたissueにはリポジトリ名のプレフィックスがある"""
        state = partial_success_state

        # final_review_issues には "[API]" プレフィックス付き
        assert any("[API]" in issue for issue in state["final_review_issues"])
