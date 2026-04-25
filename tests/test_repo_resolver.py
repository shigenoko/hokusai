"""
Runtime Repository Resolver のテスト
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from hokusai.utils.repo_resolver import (
    RuntimeRepository,
    resolve_runtime_repositories,
    get_runtime_repository,
)


@dataclass
class MockRepositoryConfig:
    name: str
    path: Path
    base_branch: str = "main"
    description: str | None = None
    build_command: str | None = "npm run build"
    test_command: str | None = "npm run test"
    lint_command: str | None = "npm run lint"
    coding_rules: str | None = None
    setup_command: str | None = None
    default_target: bool = True


class MockWorkflowConfig:
    base_branch = "main"

    def __init__(self, repos=None):
        self._repos = repos or []

    def get_all_repositories(self):
        return self._repos


class TestResolveRuntimeRepositories:
    """resolve_runtime_repositories() のテスト"""

    def test_resolves_from_state_with_worktree(self):
        """state に repositories がある場合、worktree path を返す"""
        state = {
            "repositories": [
                {
                    "name": "Backend",
                    "path": "/tmp/worktrees/Backend_wf-123",
                    "source_path": "/home/user/repo/backend",
                    "branch": "feature/test",
                    "base_branch": "main",
                    "worktree_created": True,
                }
            ],
            "branch_name": "feature/test",
        }
        config = MockWorkflowConfig(repos=[
            MockRepositoryConfig(
                name="Backend",
                path=Path("/home/user/repo/backend"),
                build_command="make build",
            ),
        ])

        repos = resolve_runtime_repositories(state, config)

        assert len(repos) == 1
        assert repos[0].name == "Backend"
        assert repos[0].path == Path("/tmp/worktrees/Backend_wf-123")
        assert repos[0].source_path == Path("/home/user/repo/backend")
        assert repos[0].worktree_created is True
        # config からコマンドが補完される
        assert repos[0].build_command == "make build"

    def test_resolves_from_state_without_worktree(self):
        """state に worktree_created=False の場合、path をそのまま返す"""
        state = {
            "repositories": [
                {
                    "name": "Backend",
                    "path": "/home/user/repo/backend",
                    "source_path": "/home/user/repo/backend",
                    "branch": "feature/test",
                    "base_branch": "main",
                    "worktree_created": False,
                }
            ],
        }
        config = MockWorkflowConfig()

        repos = resolve_runtime_repositories(state, config)

        assert repos[0].path == Path("/home/user/repo/backend")
        assert repos[0].worktree_created is False

    def test_fallback_to_config_when_state_empty(self):
        """state に repositories がない場合、config から構築する（後方互換）"""
        state = {
            "repositories": [],
            "branch_name": "feature/test",
            "base_branch": "develop",
        }
        config = MockWorkflowConfig(repos=[
            MockRepositoryConfig(
                name="API",
                path=Path("/home/user/repo/api"),
                base_branch="develop",
                build_command="go build ./...",
                test_command="go test ./...",
                lint_command="golangci-lint run",
            ),
        ])

        repos = resolve_runtime_repositories(state, config)

        assert len(repos) == 1
        assert repos[0].name == "API"
        assert repos[0].path == Path("/home/user/repo/api")
        assert repos[0].source_path == Path("/home/user/repo/api")
        assert repos[0].branch == "feature/test"
        assert repos[0].worktree_created is False
        assert repos[0].build_command == "go build ./..."

    def test_multiple_repositories(self):
        """複数リポジトリが正しく解決される"""
        state = {
            "repositories": [
                {
                    "name": "Backend",
                    "path": "/tmp/wt/Backend_wf-1",
                    "source_path": "/repo/backend",
                    "branch": "feature/x",
                    "base_branch": "main",
                    "worktree_created": True,
                },
                {
                    "name": "Frontend",
                    "path": "/tmp/wt/Frontend_wf-1",
                    "source_path": "/repo/frontend",
                    "branch": "feature/x",
                    "base_branch": "main",
                    "worktree_created": True,
                },
            ],
        }
        config = MockWorkflowConfig()

        repos = resolve_runtime_repositories(state, config)

        assert len(repos) == 2
        assert repos[0].name == "Backend"
        assert repos[1].name == "Frontend"

    def test_source_path_fallback_to_path(self):
        """source_path が未設定の場合、path にフォールバック"""
        state = {
            "repositories": [
                {
                    "name": "Old",
                    "path": "/repo/old",
                    "branch": "feat",
                    "base_branch": "main",
                }
            ],
        }
        config = MockWorkflowConfig()

        repos = resolve_runtime_repositories(state, config)

        assert repos[0].source_path == Path("/repo/old")
        assert repos[0].worktree_created is False


class TestGetRuntimeRepository:
    """get_runtime_repository() のテスト"""

    def test_finds_by_name(self):
        """名前で単一リポジトリを取得できる"""
        state = {
            "repositories": [
                {"name": "A", "path": "/a", "branch": "b", "base_branch": "main"},
                {"name": "B", "path": "/b", "branch": "b", "base_branch": "main"},
            ],
        }
        config = MockWorkflowConfig()

        repo = get_runtime_repository(state, config, "B")

        assert repo is not None
        assert repo.name == "B"
        assert repo.path == Path("/b")

    def test_returns_none_for_unknown_name(self):
        """存在しない名前は None を返す"""
        state = {"repositories": []}
        config = MockWorkflowConfig()

        repo = get_runtime_repository(state, config, "Unknown")

        assert repo is None
