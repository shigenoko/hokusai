"""
workflow.py の worktree 検証テスト
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hokusai.workflow import WorkflowRunner


class TestValidateWorktrees:
    """_validate_worktrees() のテスト"""

    def _make_runner(self):
        """テスト用 WorkflowRunner を作成（初期化を最小限に）"""
        with patch("hokusai.workflow.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                database_path=":memory:",
                checkpoint_db_path=":memory:",
            )
            with patch("hokusai.workflow.SQLiteStore"):
                runner = WorkflowRunner.__new__(WorkflowRunner)
                runner.config = mock_config.return_value
                runner.store = MagicMock()
                runner.compiled_workflow = None
                runner.verbose = False
                runner.dry_run = False
                runner.step_mode = False
                return runner

    def test_passes_when_worktree_exists(self, tmp_path: Path):
        """worktree が存在する場合はエラーなし"""
        runner = self._make_runner()
        wt_path = tmp_path / "worktree_exists"
        wt_path.mkdir()

        state = {
            "workflow_id": "wf-test",
            "repositories": [
                {
                    "name": "Backend",
                    "path": str(wt_path),
                    "source_path": "/original",
                    "worktree_created": True,
                }
            ],
        }

        # エラーなしで通過
        runner._validate_worktrees(state)

    def test_raises_when_worktree_missing(self, tmp_path: Path):
        """worktree が存在しない場合は RuntimeError"""
        runner = self._make_runner()

        state = {
            "workflow_id": "wf-missing",
            "repositories": [
                {
                    "name": "Backend",
                    "path": str(tmp_path / "nonexistent"),
                    "source_path": "/original",
                    "worktree_created": True,
                }
            ],
        }

        with pytest.raises(RuntimeError, match="Worktree が存在しません"):
            runner._validate_worktrees(state)

    def test_skips_non_worktree_repos(self, tmp_path: Path):
        """worktree_created=False のリポジトリはスキップ"""
        runner = self._make_runner()

        state = {
            "workflow_id": "wf-legacy",
            "repositories": [
                {
                    "name": "Backend",
                    "path": str(tmp_path / "nonexistent"),
                    "source_path": str(tmp_path / "nonexistent"),
                    "worktree_created": False,
                }
            ],
        }

        # worktree_created=False なのでエラーにならない
        runner._validate_worktrees(state)

    def test_skips_empty_repositories(self):
        """repositories が空の場合はスキップ"""
        runner = self._make_runner()

        state = {
            "workflow_id": "wf-empty",
            "repositories": [],
        }

        runner._validate_worktrees(state)

    def test_error_message_includes_details(self, tmp_path: Path):
        """エラーメッセージに worktree path と workflow_id が含まれる"""
        runner = self._make_runner()
        missing_path = str(tmp_path / "missing_wt")

        state = {
            "workflow_id": "wf-detail",
            "repositories": [
                {
                    "name": "API",
                    "path": missing_path,
                    "source_path": "/src",
                    "worktree_created": True,
                }
            ],
        }

        with pytest.raises(RuntimeError) as exc_info:
            runner._validate_worktrees(state)

        error_msg = str(exc_info.value)
        assert missing_path in error_msg
        assert "wf-detail" in error_msg
        assert "API" in error_msg

    def test_multiple_repos_one_missing(self, tmp_path: Path):
        """複数リポジトリのうち1つだけ欠損している場合もエラー"""
        runner = self._make_runner()
        existing = tmp_path / "exists"
        existing.mkdir()

        state = {
            "workflow_id": "wf-partial",
            "repositories": [
                {
                    "name": "Backend",
                    "path": str(existing),
                    "source_path": "/src/be",
                    "worktree_created": True,
                },
                {
                    "name": "Frontend",
                    "path": str(tmp_path / "missing"),
                    "source_path": "/src/fe",
                    "worktree_created": True,
                },
            ],
        }

        with pytest.raises(RuntimeError, match="Frontend"):
            runner._validate_worktrees(state)
