"""
Tests for hokusai.nodes.phase1_prepare module

setup_command 関連のテスト
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hokusai.config.models import RepositoryConfig


@pytest.fixture
def mock_config():
    """モック WorkflowConfig"""
    config = MagicMock()
    config.worktree_root = Path("/tmp/worktrees")
    config.submodule_enabled = False
    config.command_timeout = 300
    return config


@pytest.fixture
def mock_repo_with_setup():
    """setup_command 付きリポジトリ設定"""
    return RepositoryConfig(
        name="Backend",
        path=Path("/repo/backend"),
        base_branch="main",
        setup_command="pnpm install",
    )


@pytest.fixture
def mock_repo_without_setup():
    """setup_command なしリポジトリ設定"""
    return RepositoryConfig(
        name="API",
        path=Path("/repo/api"),
        base_branch="main",
    )


PATCH_PREFIX = "hokusai.nodes.phase1_prepare"


class TestSetupCommandExecution:
    """setup_command 実行のテスト"""

    @patch(f"{PATCH_PREFIX}.subprocess")
    @patch(f"{PATCH_PREFIX}.GitClient")
    @patch(f"{PATCH_PREFIX}.get_config")
    @patch(f"{PATCH_PREFIX}.get_task_client")
    def test_setup_command_executed_after_worktree_creation(
        self,
        mock_get_task_client,
        mock_get_config,
        mock_git_client_cls,
        mock_subprocess,
        minimal_state,
        mock_config,
        mock_repo_with_setup,
    ):
        """worktree 作成後に setup_command が実行される"""
        mock_config.get_target_repositories.return_value = [mock_repo_with_setup]
        mock_config.repositories = [mock_repo_with_setup]
        mock_get_config.return_value = mock_config

        task_client = MagicMock()
        task_client.fetch_task.return_value = {"title": "Test"}
        task_client.get_task_title.return_value = "Test Task"
        task_client.update_status.return_value = MagicMock(spec=[])
        task_client.prepend_content.return_value = MagicMock(spec=[])
        mock_get_task_client.return_value = task_client

        mock_git = MagicMock()
        mock_git.generate_branch_name.return_value = "feature/test"
        mock_git_client_cls.return_value = mock_git

        mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        minimal_state["branch_name"] = ""

        from hokusai.nodes.phase1_prepare import phase1_prepare_node
        phase1_prepare_node(minimal_state)

        mock_subprocess.run.assert_called_once_with(
            "pnpm install",
            shell=True,
            cwd=str(mock_config.worktree_root / "Backend_wf-test0001"),
            capture_output=True,
            text=True,
            timeout=300,
        )

    @patch(f"{PATCH_PREFIX}.subprocess")
    @patch(f"{PATCH_PREFIX}.GitClient")
    @patch(f"{PATCH_PREFIX}.get_config")
    @patch(f"{PATCH_PREFIX}.get_task_client")
    def test_setup_command_failure_triggers_rollback(
        self,
        mock_get_task_client,
        mock_get_config,
        mock_git_client_cls,
        mock_subprocess,
        minimal_state,
        mock_config,
        mock_repo_with_setup,
    ):
        """setup_command 失敗時に worktree がロールバックされる"""
        mock_config.get_target_repositories.return_value = [mock_repo_with_setup]
        mock_config.repositories = [mock_repo_with_setup]
        mock_get_config.return_value = mock_config

        task_client = MagicMock()
        task_client.fetch_task.return_value = {"title": "Test"}
        task_client.get_task_title.return_value = "Test Task"
        task_client.update_status.return_value = MagicMock(spec=[])
        mock_get_task_client.return_value = task_client

        mock_git = MagicMock()
        mock_git.generate_branch_name.return_value = "feature/test"
        mock_git_client_cls.return_value = mock_git

        mock_subprocess.run.return_value = MagicMock(
            returncode=1,
            stderr="ERR_PNPM_NO_LOCKFILE",
        )

        minimal_state["branch_name"] = ""

        from hokusai.nodes.phase1_prepare import phase1_prepare_node
        with pytest.raises(RuntimeError, match="setup_command が失敗しました"):
            phase1_prepare_node(minimal_state)

        mock_git.remove_worktree.assert_called_once()

    @patch(f"{PATCH_PREFIX}.subprocess")
    @patch(f"{PATCH_PREFIX}.GitClient")
    @patch(f"{PATCH_PREFIX}.get_config")
    @patch(f"{PATCH_PREFIX}.get_task_client")
    def test_setup_command_none_skipped(
        self,
        mock_get_task_client,
        mock_get_config,
        mock_git_client_cls,
        mock_subprocess,
        minimal_state,
        mock_config,
        mock_repo_without_setup,
    ):
        """setup_command が未設定の場合はスキップされる"""
        mock_config.get_target_repositories.return_value = [mock_repo_without_setup]
        mock_config.repositories = [mock_repo_without_setup]
        mock_get_config.return_value = mock_config

        task_client = MagicMock()
        task_client.fetch_task.return_value = {"title": "Test"}
        task_client.get_task_title.return_value = "Test Task"
        task_client.update_status.return_value = MagicMock(spec=[])
        task_client.prepend_content.return_value = MagicMock(spec=[])
        mock_get_task_client.return_value = task_client

        mock_git = MagicMock()
        mock_git.generate_branch_name.return_value = "feature/test"
        mock_git_client_cls.return_value = mock_git

        minimal_state["branch_name"] = ""

        from hokusai.nodes.phase1_prepare import phase1_prepare_node
        phase1_prepare_node(minimal_state)

        mock_subprocess.run.assert_not_called()
