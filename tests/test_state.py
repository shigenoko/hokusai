"""
Tests for hokusai.state module

B-1-3, B-4-4: VerificationErrorEntry と repository_status のテスト
"""

import pytest
from datetime import datetime

from hokusai.state import (
    WorkflowState,
    PhaseStatus,
    VerificationResult,
    VerificationErrorEntry,
    create_initial_state,
    update_phase_status,
    add_audit_log,
    should_skip_phase,
)


class TestVerificationErrorEntry:
    """VerificationErrorEntry TypedDict のテスト"""

    def test_create_success_entry(self):
        """成功エントリの作成"""
        entry = VerificationErrorEntry(
            repository="Backend",
            command="build",
            success=True,
            error_output=None,
        )
        assert entry["repository"] == "Backend"
        assert entry["command"] == "build"
        assert entry["success"] is True
        assert entry["error_output"] is None

    def test_create_failure_entry(self):
        """失敗エントリの作成"""
        error_msg = "Error: Module not found\n  at /path/to/file.ts:10"
        entry = VerificationErrorEntry(
            repository="API",
            command="test",
            success=False,
            error_output=error_msg,
        )
        assert entry["repository"] == "API"
        assert entry["command"] == "test"
        assert entry["success"] is False
        assert entry["error_output"] == error_msg

    def test_entry_as_dict(self):
        """TypedDictとしてdictの機能を持つ"""
        entry = VerificationErrorEntry(
            repository="Backend",
            command="lint",
            success=False,
            error_output="Lint error",
        )
        # dictとして扱える
        assert entry.get("repository") == "Backend"
        assert entry.get("nonexistent", "default") == "default"


class TestCreateInitialState:
    """create_initial_state のテスト"""

    def test_verification_errors_initialized(self):
        """verification_errors が空リストで初期化される"""
        state = create_initial_state(
            task_url="https://notion.so/test",
            task_title="Test",
        )
        assert "verification_errors" in state
        assert state["verification_errors"] == []

    def test_repository_status_initialized(self):
        """repository_status が空dictで初期化される"""
        state = create_initial_state(
            task_url="https://notion.so/test",
            task_title="Test",
        )
        assert "repository_status" in state
        assert state["repository_status"] == {}

    def test_verification_initialized(self):
        """verification が NOT_RUN で初期化される"""
        state = create_initial_state(
            task_url="https://notion.so/test",
            task_title="Test",
        )
        assert state["verification"]["build"] == VerificationResult.NOT_RUN.value
        assert state["verification"]["test"] == VerificationResult.NOT_RUN.value
        assert state["verification"]["lint"] == VerificationResult.NOT_RUN.value

    def test_legacy_phase_page_fields_are_not_created(self):
        """新規 state では phase_page_status / phase_page_last_review_round を生成しない"""
        state = create_initial_state(
            task_url="https://notion.so/test",
            task_title="Test",
        )
        assert "phase_page_status" not in state
        assert "phase_page_last_review_round" not in state


class TestRepositoryStatus:
    """repository_status フィールドのテスト"""

    def test_update_repository_status(self, minimal_state: WorkflowState):
        """リポジトリステータスの更新"""
        minimal_state["repository_status"]["Backend"] = "completed"
        minimal_state["repository_status"]["API"] = "failed"

        assert minimal_state["repository_status"]["Backend"] == "completed"
        assert minimal_state["repository_status"]["API"] == "failed"

    def test_check_existing_status(self, state_with_repository_status: WorkflowState):
        """既存ステータスの確認"""
        status = state_with_repository_status.get("repository_status", {})
        assert status.get("Backend") == "completed"
        assert status.get("API") == "failed"
        assert status.get("Frontend") is None

    def test_skip_completed_repository(self, state_with_repository_status: WorkflowState):
        """完了済みリポジトリのスキップ判定"""
        repo_status = state_with_repository_status.get("repository_status", {})

        # Backend は completed なのでスキップ可能
        assert repo_status.get("Backend") == "completed"

        # API は failed なので再実行が必要
        assert repo_status.get("API") == "failed"


class TestUpdatePhaseStatus:
    """update_phase_status のテスト"""

    def test_update_to_in_progress(self, minimal_state: WorkflowState):
        """IN_PROGRESS への更新"""
        state = update_phase_status(minimal_state, 6, PhaseStatus.IN_PROGRESS)
        assert state["phases"][6]["status"] == PhaseStatus.IN_PROGRESS.value
        assert state["phases"][6]["started_at"] is not None
        assert state["current_phase"] == 6

    def test_update_to_completed(self, minimal_state: WorkflowState):
        """COMPLETED への更新"""
        state = update_phase_status(minimal_state, 6, PhaseStatus.COMPLETED)
        assert state["phases"][6]["status"] == PhaseStatus.COMPLETED.value
        assert state["phases"][6]["completed_at"] is not None


class TestAddAuditLog:
    """add_audit_log のテスト"""

    def test_add_verification_log(self, minimal_state: WorkflowState):
        """検証結果のログ追加"""
        state = add_audit_log(
            minimal_state, 6, "verification_completed", "success",
            details={"build": "pass", "test": "fail"}
        )
        assert len(state["audit_log"]) == 1
        log = state["audit_log"][0]
        assert log["phase"] == 6
        assert log["action"] == "verification_completed"
        assert log["result"] == "success"
        assert log["details"]["build"] == "pass"


class TestShouldSkipPhase:
    """should_skip_phase のテスト"""

    def test_skip_skipped_phase(self, minimal_state: WorkflowState):
        """SKIPPED フェーズはスキップ"""
        minimal_state["phases"][4]["status"] = PhaseStatus.SKIPPED.value
        assert should_skip_phase(minimal_state, 4) is True

    def test_dont_skip_completed_phase(self, minimal_state: WorkflowState):
        """COMPLETED フェーズはスキップしない（リトライ可能）"""
        minimal_state["phases"][5]["status"] = PhaseStatus.COMPLETED.value
        assert should_skip_phase(minimal_state, 5) is False

    def test_dont_skip_pending_phase(self, minimal_state: WorkflowState):
        """PENDING フェーズはスキップしない"""
        assert should_skip_phase(minimal_state, 6) is False


# === C-1: リポジトリ別状態管理のテスト ===

from hokusai.state import (
    RepositoryState,
    RepositoryPhaseStatus,
    init_repository_state,
    get_repository_state,
    update_repository_state,
    update_repository_phase_status,
    get_pending_repositories,
    get_completed_repositories,
    all_repositories_completed,
)


class TestRepositoryState:
    """RepositoryState TypedDict のテスト"""

    def test_create_repository_state(self):
        """リポジトリ状態の作成"""
        repo = init_repository_state(
            name="Backend",
            path="/path/to/backend",
            branch="feature/test",
            base_branch="main",
        )
        assert repo["name"] == "Backend"
        assert repo["path"] == "/path/to/backend"
        assert repo["branch"] == "feature/test"
        assert repo["base_branch"] == "main"
        assert repo["phase_status"] == {}
        assert repo["pr_url"] is None
        assert repo["verification_results"] == []

    def test_repositories_in_workflow_state(self, minimal_state: WorkflowState):
        """WorkflowState に repositories が含まれる"""
        assert "repositories" in minimal_state
        assert minimal_state["repositories"] == []


class TestGetRepositoryState:
    """get_repository_state のテスト"""

    def test_get_existing_repository(self, minimal_state: WorkflowState):
        """既存のリポジトリを取得"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        minimal_state["repositories"] = [repo]

        result = get_repository_state(minimal_state, "Backend")
        assert result is not None
        assert result["name"] == "Backend"

    def test_get_nonexistent_repository(self, minimal_state: WorkflowState):
        """存在しないリポジトリを取得"""
        result = get_repository_state(minimal_state, "NonExistent")
        assert result is None


class TestUpdateRepositoryState:
    """update_repository_state のテスト"""

    def test_update_repository(self, minimal_state: WorkflowState):
        """リポジトリ状態を更新"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        minimal_state["repositories"] = [repo]

        updated = update_repository_state(
            minimal_state, "Backend", {"pr_url": "https://github.com/test/pr/123"}
        )

        result = get_repository_state(updated, "Backend")
        assert result["pr_url"] == "https://github.com/test/pr/123"


class TestUpdateRepositoryPhaseStatus:
    """update_repository_phase_status のテスト"""

    def test_update_phase_status(self, minimal_state: WorkflowState):
        """フェーズ状態を更新"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        minimal_state["repositories"] = [repo]

        updated = update_repository_phase_status(
            minimal_state, "Backend", 6, RepositoryPhaseStatus.COMPLETED
        )

        result = get_repository_state(updated, "Backend")
        assert result["phase_status"][6] == "completed"

    def test_repositories_is_single_source(self, minimal_state: WorkflowState):
        """repositories.phase_status が単一情報源として使用される"""
        repo = init_repository_state("Backend", "/path", "feature/test", "main")
        minimal_state["repositories"] = [repo]

        updated = update_repository_phase_status(
            minimal_state, "Backend", 6, RepositoryPhaseStatus.COMPLETED
        )

        # repositories.phase_status のみが更新される
        repo_state = get_repository_state(updated, "Backend")
        assert repo_state["phase_status"][6] == RepositoryPhaseStatus.COMPLETED.value


class TestGetPendingRepositories:
    """get_pending_repositories のテスト"""

    def test_get_pending(self, minimal_state: WorkflowState):
        """未完了リポジトリを取得"""
        backend = init_repository_state("Backend", "/path/backend", "feature/test", "main")
        backend["phase_status"] = {5: "completed", 6: "completed"}

        api = init_repository_state("API", "/path/api", "feature/test", "main")
        api["phase_status"] = {5: "completed", 6: "failed"}

        minimal_state["repositories"] = [backend, api]

        pending = get_pending_repositories(minimal_state, 6)
        assert len(pending) == 1
        assert pending[0]["name"] == "API"


class TestAllRepositoriesCompleted:
    """all_repositories_completed のテスト"""

    def test_all_completed(self, minimal_state: WorkflowState):
        """全リポジトリが完了"""
        backend = init_repository_state("Backend", "/path/backend", "feature/test", "main")
        backend["phase_status"] = {6: "completed"}

        api = init_repository_state("API", "/path/api", "feature/test", "main")
        api["phase_status"] = {6: "completed"}

        minimal_state["repositories"] = [backend, api]

        assert all_repositories_completed(minimal_state, 6) is True

    def test_not_all_completed(self, minimal_state: WorkflowState):
        """全リポジトリが完了していない"""
        backend = init_repository_state("Backend", "/path/backend", "feature/test", "main")
        backend["phase_status"] = {6: "completed"}

        api = init_repository_state("API", "/path/api", "feature/test", "main")
        api["phase_status"] = {6: "failed"}

        minimal_state["repositories"] = [backend, api]

        assert all_repositories_completed(minimal_state, 6) is False

    def test_empty_repositories(self, minimal_state: WorkflowState):
        """リポジトリが空の場合"""
        assert all_repositories_completed(minimal_state, 6) is True
