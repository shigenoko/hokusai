"""
Integration tests for multi-repository workflow

マルチリポジトリフロー全体の統合テスト
"""

import pytest
from datetime import datetime

from hokusai.state import (
    WorkflowState,
    PhaseStatus,
    VerificationResult,
    VerificationErrorEntry,
    PhaseState,
    RepositoryPhaseStatus,
    init_repository_state,
    get_repository_state,
    update_repository_phase_status,
    get_pending_repositories,
    all_repositories_completed,
)


@pytest.fixture
def multi_repo_state() -> WorkflowState:
    """マルチリポジトリ用のワークフロー状態"""
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

    # 2つのリポジトリを持つ状態を作成
    backend = init_repository_state("Backend", "/path/backend", "feature/test", "main")
    api = init_repository_state("API", "/path/api", "feature/test", "main")

    return WorkflowState(
        workflow_id="wf-integration-001",
        task_url="https://notion.so/test-task",
        task_title="Integration Test Task",
        branch_name="feature/test",
        base_branch="main",
        current_phase=1,
        phases=phases,
        schema_change_required=False,
        schema_pr_url=None,
        schema_pr_merged=False,
        pull_requests=[],
        current_pr_index=0,
        verification={
            "build": VerificationResult.NOT_RUN.value,
            "test": VerificationResult.NOT_RUN.value,
            "lint": VerificationResult.NOT_RUN.value,
        },
        verification_errors=[],
        repository_status={},
        repositories=[backend, api],
        final_review_passed=False,
        final_review_issues=[],
        final_review_rules={},
        final_review_by_repo={},
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


class TestMultiRepoStateManagement:
    """マルチリポジトリの状態管理テスト"""

    def test_initial_state_has_two_repos(self, multi_repo_state: WorkflowState):
        """初期状態で2つのリポジトリがある"""
        assert len(multi_repo_state["repositories"]) == 2
        assert multi_repo_state["repositories"][0]["name"] == "Backend"
        assert multi_repo_state["repositories"][1]["name"] == "API"

    def test_update_phase_status_per_repo(self, multi_repo_state: WorkflowState):
        """リポジトリごとにフェーズ状態を更新できる"""
        state = multi_repo_state

        # Backend は Phase 6 完了
        state = update_repository_phase_status(
            state, "Backend", 6, RepositoryPhaseStatus.COMPLETED
        )
        # API は Phase 6 失敗
        state = update_repository_phase_status(
            state, "API", 6, RepositoryPhaseStatus.FAILED
        )

        backend = get_repository_state(state, "Backend")
        api = get_repository_state(state, "API")

        assert backend["phase_status"][6] == "completed"
        assert api["phase_status"][6] == "failed"

    def test_repositories_is_single_source(self, multi_repo_state: WorkflowState):
        """repositories が単一情報源として使用される"""
        state = multi_repo_state

        state = update_repository_phase_status(
            state, "Backend", 6, RepositoryPhaseStatus.COMPLETED
        )

        # repositories.phase_status のみが更新される
        backend = get_repository_state(state, "Backend")
        assert backend["phase_status"][6] == RepositoryPhaseStatus.COMPLETED.value

    def test_get_pending_repositories(self, multi_repo_state: WorkflowState):
        """未完了リポジトリを取得できる"""
        state = multi_repo_state

        # Backend は Phase 6 完了
        state = update_repository_phase_status(
            state, "Backend", 6, RepositoryPhaseStatus.COMPLETED
        )
        # API は Phase 6 失敗
        state = update_repository_phase_status(
            state, "API", 6, RepositoryPhaseStatus.FAILED
        )

        pending = get_pending_repositories(state, 6)

        assert len(pending) == 1
        assert pending[0]["name"] == "API"

    def test_all_repositories_completed(self, multi_repo_state: WorkflowState):
        """全リポジトリの完了チェック"""
        state = multi_repo_state

        # 最初は未完了
        assert all_repositories_completed(state, 6) is False

        # Backend のみ完了
        state = update_repository_phase_status(
            state, "Backend", 6, RepositoryPhaseStatus.COMPLETED
        )
        assert all_repositories_completed(state, 6) is False

        # 両方完了
        state = update_repository_phase_status(
            state, "API", 6, RepositoryPhaseStatus.COMPLETED
        )
        assert all_repositories_completed(state, 6) is True


class TestMultiRepoVerificationFlow:
    """マルチリポジトリの検証フローテスト"""

    def test_verification_errors_per_repo(self, multi_repo_state: WorkflowState):
        """リポジトリごとに検証エラーを記録できる"""
        state = multi_repo_state

        # 検証エラーを追加
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
                success=False,
                error_output="TypeScript compilation failed",
            ),
        ]

        # Backend のエラーを取得
        backend_errors = [
            e for e in state["verification_errors"]
            if e["repository"] == "Backend"
        ]
        assert len(backend_errors) == 2
        assert all(e["success"] for e in backend_errors)

        # API のエラーを取得
        api_errors = [
            e for e in state["verification_errors"]
            if e["repository"] == "API"
        ]
        assert len(api_errors) == 1
        assert api_errors[0]["success"] is False

    def test_partial_verification_success(self, multi_repo_state: WorkflowState):
        """一部のリポジトリのみ検証成功のシナリオ"""
        state = multi_repo_state

        # Backend は成功
        state = update_repository_phase_status(
            state, "Backend", 6, RepositoryPhaseStatus.COMPLETED
        )
        # API は失敗
        state = update_repository_phase_status(
            state, "API", 6, RepositoryPhaseStatus.FAILED
        )

        # Phase 6 は全体として失敗
        assert not all_repositories_completed(state, 6)

        # 再実行時は API のみが対象
        pending = get_pending_repositories(state, 6)
        assert len(pending) == 1
        assert pending[0]["name"] == "API"


class TestMultiRepoReviewFlow:
    """マルチリポジトリのレビューフローテスト"""

    def test_review_results_per_repo(self, multi_repo_state: WorkflowState):
        """リポジトリごとにレビュー結果を記録できる"""
        state = multi_repo_state

        state["final_review_by_repo"] = {
            "Backend": {
                "passed": True,
                "issues": [],
                "rules": {"CQ01": {"result": "OK"}},
            },
            "API": {
                "passed": False,
                "issues": ["[API] console.log found"],
                "rules": {"CQ01": {"result": "NG"}},
            },
        }

        # Backend はレビュー合格
        assert state["final_review_by_repo"]["Backend"]["passed"]
        # API はレビュー不合格
        assert not state["final_review_by_repo"]["API"]["passed"]

    def test_partial_review_success(self, multi_repo_state: WorkflowState):
        """一部のリポジトリのみレビュー成功のシナリオ"""
        state = multi_repo_state

        # Backend は Phase 7 完了
        state = update_repository_phase_status(
            state, "Backend", 7, RepositoryPhaseStatus.COMPLETED
        )
        # API は Phase 7 失敗
        state = update_repository_phase_status(
            state, "API", 7, RepositoryPhaseStatus.FAILED
        )

        # Phase 7 は全体として失敗
        assert not all_repositories_completed(state, 7)

        # 再実行時は API のみが対象
        pending = get_pending_repositories(state, 7)
        assert len(pending) == 1
        assert pending[0]["name"] == "API"


class TestMultiRepoPRFlow:
    """マルチリポジトリのPRフローテスト"""

    def test_success_check_for_pr_ready(self, multi_repo_state: WorkflowState):
        """PRをReady for Reviewにする判定"""
        state = multi_repo_state

        # Backend は Phase 6, 7 両方完了
        state = update_repository_phase_status(
            state, "Backend", 6, RepositoryPhaseStatus.COMPLETED
        )
        state = update_repository_phase_status(
            state, "Backend", 7, RepositoryPhaseStatus.COMPLETED
        )

        # API は Phase 6 のみ完了
        state = update_repository_phase_status(
            state, "API", 6, RepositoryPhaseStatus.COMPLETED
        )
        state = update_repository_phase_status(
            state, "API", 7, RepositoryPhaseStatus.FAILED
        )

        # Backend のステータス確認
        backend = get_repository_state(state, "Backend")
        backend_p6 = backend["phase_status"].get(6) == RepositoryPhaseStatus.COMPLETED.value
        backend_p7 = backend["phase_status"].get(7) == RepositoryPhaseStatus.COMPLETED.value
        assert backend_p6 and backend_p7  # Ready for Review 可能

        # API のステータス確認
        api = get_repository_state(state, "API")
        api_p6 = api["phase_status"].get(6) == RepositoryPhaseStatus.COMPLETED.value
        api_p7 = api["phase_status"].get(7) == RepositoryPhaseStatus.COMPLETED.value
        assert api_p6 and not api_p7  # Ready for Review 不可
