"""
Tests for hokusai.nodes.phase8_pr module

C-2-4: 部分的なPR完了フローのテスト
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from hokusai.state import (
    WorkflowState,
    PullRequestInfo,
    PRStatus,
    init_repository_state,
    RepositoryPhaseStatus,
)
from hokusai.nodes.phase8 import (
    _is_repository_successful,
    _mark_successful_prs_ready,
    phase8e_ready_for_review_node,
)


class TestIsRepositorySuccessful:
    """_is_repository_successful 関数のテスト"""

    def test_success_with_repositories_completed(self, minimal_state: WorkflowState):
        """repositories の phase_status で Phase 6, 7 が completed の場合は True"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {
            6: RepositoryPhaseStatus.COMPLETED.value,
            7: RepositoryPhaseStatus.COMPLETED.value,
        }
        minimal_state["repositories"] = [repo]

        result = _is_repository_successful(minimal_state, "Backend")

        assert result is True

    def test_failure_with_repositories_failed(self, minimal_state: WorkflowState):
        """repositories の phase_status が failed の場合は False"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {6: RepositoryPhaseStatus.FAILED.value}
        minimal_state["repositories"] = [repo]

        result = _is_repository_successful(minimal_state, "Backend")

        assert result is False

    def test_failure_with_phase6_failed(self, minimal_state: WorkflowState):
        """Phase 6 が failed の場合は False"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {
            6: RepositoryPhaseStatus.FAILED.value,
            7: RepositoryPhaseStatus.COMPLETED.value,
        }
        minimal_state["repositories"] = [repo]

        result = _is_repository_successful(minimal_state, "Backend")

        assert result is False

    def test_failure_with_phase7_failed(self, minimal_state: WorkflowState):
        """Phase 7 が failed の場合は False"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {
            6: RepositoryPhaseStatus.COMPLETED.value,
            7: RepositoryPhaseStatus.FAILED.value,
        }
        minimal_state["repositories"] = [repo]

        result = _is_repository_successful(minimal_state, "Backend")

        assert result is False

    def test_failure_with_phase_pending(self, minimal_state: WorkflowState):
        """Phase 6, 7 が pending の場合は False"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {}  # pending
        minimal_state["repositories"] = [repo]

        result = _is_repository_successful(minimal_state, "Backend")

        assert result is False

    def test_failure_with_unknown_repository(self, minimal_state: WorkflowState):
        """未知のリポジトリの場合は False"""
        result = _is_repository_successful(minimal_state, "UnknownRepo")

        assert result is False

    def test_repositories_is_single_source(
        self, minimal_state: WorkflowState
    ):
        """repositories のみが参照される（repository_status は無視）"""
        # repository_status は completed (deprecated - 無視される)
        minimal_state["repository_status"] = {"Backend": "completed"}
        # repositories は failed
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {
            6: RepositoryPhaseStatus.FAILED.value,
        }
        minimal_state["repositories"] = [repo]

        result = _is_repository_successful(minimal_state, "Backend")

        # repositories が単一情報源なので、failed と判定される
        assert result is False


class TestMarkSuccessfulPrsReady:
    """_mark_successful_prs_ready 関数のテスト"""

    def test_all_prs_stay_draft(self, minimal_state: WorkflowState):
        """全PRがDraftのまま維持される（Ready for Reviewは人間が手動で行う）"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {
            6: RepositoryPhaseStatus.COMPLETED.value,
            7: RepositoryPhaseStatus.COMPLETED.value,
        }
        minimal_state["repositories"] = [repo]
        minimal_state["pull_requests"] = [
            PullRequestInfo(
                repo_name="Backend",
                title="Test PR",
                url="https://github.com/test/repo/pull/1",
                number=1,
                owner="test",
                repo="repo",
                status=PRStatus.DRAFT.value,
                github_status="draft",
            )
        ]

        ready_prs, draft_prs = _mark_successful_prs_ready(minimal_state)

        assert len(ready_prs) == 0
        assert len(draft_prs) == 1
        assert draft_prs[0]["repo_name"] == "Backend"

    def test_keep_failed_pr_as_draft(self, minimal_state: WorkflowState):
        """失敗したリポジトリのPRはDraftのまま"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {6: RepositoryPhaseStatus.FAILED.value}
        minimal_state["repositories"] = [repo]
        minimal_state["pull_requests"] = [
            PullRequestInfo(
                repo_name="Backend",
                title="Test PR",
                url="https://github.com/test/repo/pull/1",
                number=1,
                owner="test",
                repo="repo",
                status=PRStatus.DRAFT.value,
                github_status="draft",
            )
        ]

        ready_prs, draft_prs = _mark_successful_prs_ready(minimal_state)

        assert len(ready_prs) == 0
        assert len(draft_prs) == 1
        assert draft_prs[0]["repo_name"] == "Backend"

    def test_mixed_repository_status(self, minimal_state: WorkflowState):
        """成功と失敗が混在する場合"""
        backend_repo = init_repository_state("Backend", "/path/backend", "feature/test", "main")
        backend_repo["phase_status"] = {
            6: RepositoryPhaseStatus.COMPLETED.value,
            7: RepositoryPhaseStatus.COMPLETED.value,
        }
        api_repo = init_repository_state("API", "/path/api", "feature/test", "main")
        api_repo["phase_status"] = {6: RepositoryPhaseStatus.FAILED.value}
        minimal_state["repositories"] = [backend_repo, api_repo]
        minimal_state["pull_requests"] = [
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
        ]

        ready_prs, draft_prs = _mark_successful_prs_ready(minimal_state)

        # 全PRがDraftのまま維持
        assert len(ready_prs) == 0
        assert len(draft_prs) == 2

    def test_api_failure_keeps_pr_as_draft(self, minimal_state: WorkflowState):
        """API呼び出し失敗時もDraftのまま"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {
            6: RepositoryPhaseStatus.COMPLETED.value,
            7: RepositoryPhaseStatus.COMPLETED.value,
        }
        minimal_state["repositories"] = [repo]
        minimal_state["pull_requests"] = [
            PullRequestInfo(
                repo_name="Backend",
                title="Test PR",
                url="https://github.com/test/repo/pull/1",
                number=1,
                owner="test",
                repo="repo",
                status=PRStatus.DRAFT.value,
                github_status="draft",
            )
        ]

        with patch(
            "hokusai.nodes.phase8.ready_for_review._get_git_client_for_pr"
        ) as mock_get_client:
            mock_client = Mock()
            mock_client.mark_ready_for_review.side_effect = Exception("API Error")
            mock_get_client.return_value = mock_client

            ready_prs, draft_prs = _mark_successful_prs_ready(minimal_state)

        # API失敗でもdraftとしてカウント
        assert len(ready_prs) == 0
        assert len(draft_prs) == 1

    def test_empty_pull_requests(self, minimal_state: WorkflowState):
        """PRがない場合"""
        minimal_state["pull_requests"] = []

        ready_prs, draft_prs = _mark_successful_prs_ready(minimal_state)

        assert len(ready_prs) == 0
        assert len(draft_prs) == 0

    def test_skip_pr_without_number(self, minimal_state: WorkflowState):
        """PR番号がないPRはスキップ"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {
            6: RepositoryPhaseStatus.COMPLETED.value,
            7: RepositoryPhaseStatus.COMPLETED.value,
        }
        minimal_state["repositories"] = [repo]
        minimal_state["pull_requests"] = [
            PullRequestInfo(
                repo_name="Backend",
                title="Test PR",
                url="https://github.com/test/repo/pull/1",
                number=None,  # 番号なし
                owner="test",
                repo="repo",
                status=PRStatus.DRAFT.value,
                github_status="draft",
            )
        ]

        ready_prs, draft_prs = _mark_successful_prs_ready(minimal_state)

        assert len(ready_prs) == 0
        assert len(draft_prs) == 0


class TestPhase8eReadyForReviewNode:
    """phase8e_ready_for_review_node 関数のテスト"""

    def test_ready_for_review_keeps_draft_and_waits(self, minimal_state: WorkflowState):
        """PRはDraftのまま維持し、人間レビュー待ちに移行"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {
            6: RepositoryPhaseStatus.COMPLETED.value,
            7: RepositoryPhaseStatus.COMPLETED.value,
        }
        minimal_state["repositories"] = [repo]
        minimal_state["pull_requests"] = [
            PullRequestInfo(
                repo_name="Backend",
                title="Test PR",
                url="https://github.com/test/repo/pull/1",
                number=1,
                owner="test",
                repo="repo",
                status=PRStatus.DRAFT.value,
                github_status="draft",
            )
        ]

        state = phase8e_ready_for_review_node(minimal_state)

        assert state["waiting_for_human"] is True
        assert state["human_input_request"] == "human_review"

    def test_ready_for_review_with_failed_prs(self, minimal_state: WorkflowState):
        """失敗したPRでもDraftのまま人間レビュー待ちに移行"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        repo["phase_status"] = {6: RepositoryPhaseStatus.FAILED.value}
        minimal_state["repositories"] = [repo]
        minimal_state["pull_requests"] = [
            PullRequestInfo(
                repo_name="Backend",
                title="Test PR",
                url="https://github.com/test/repo/pull/1",
                number=1,
                owner="test",
                repo="repo",
                status=PRStatus.DRAFT.value,
                github_status="draft",
            )
        ]

        state = phase8e_ready_for_review_node(minimal_state)

        assert state["waiting_for_human"] is True
        assert state["human_input_request"] == "human_review"

    def test_audit_log_recorded(self, minimal_state: WorkflowState):
        """監査ログが記録される"""
        backend_repo = init_repository_state("Backend", "/path/backend", "feature/test", "main")
        backend_repo["phase_status"] = {
            6: RepositoryPhaseStatus.COMPLETED.value,
            7: RepositoryPhaseStatus.COMPLETED.value,
        }
        api_repo = init_repository_state("API", "/path/api", "feature/test", "main")
        api_repo["phase_status"] = {6: RepositoryPhaseStatus.FAILED.value}
        minimal_state["repositories"] = [backend_repo, api_repo]
        minimal_state["pull_requests"] = [
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
        ]

        state = phase8e_ready_for_review_node(minimal_state)

        # 監査ログを確認
        assert len(state["audit_log"]) > 0
        log = state["audit_log"][-1]
        assert log["action"] == "ready_for_review_processed"
        assert "ready_prs" in log["details"]
        assert "draft_prs" in log["details"]
