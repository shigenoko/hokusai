"""
GitClient worktree 操作のテスト

実際の git リポジトリを使って worktree の作成/削除/確認を検証する。
"""

import subprocess
from pathlib import Path

import pytest

from hokusai.integrations.git import GitClient


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """テスト用 bare リポジトリを作成"""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )

    # clone して初期コミットを作成
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)

    # git config を設定（tmp repo なのでグローバル設定に依存しない）
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=clone, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=clone, check=True, capture_output=True,
    )

    # main ブランチで初期コミット
    subprocess.run(
        ["git", "checkout", "-b", "main"],
        cwd=clone, capture_output=True,  # 既に main なら失敗してもOK
    )
    (clone / "README.md").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=clone, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=clone, check=True, capture_output=True,
    )

    return clone


@pytest.fixture
def git_client(bare_repo: Path) -> GitClient:
    """bare_repo の clone に対する GitClient"""
    return GitClient(str(bare_repo))


class TestCreateWorktree:
    """create_worktree() のテスト"""

    def test_creates_new_branch_and_worktree(self, git_client: GitClient, tmp_path: Path):
        """新規ブランチ + worktree を作成できる"""
        wt_path = tmp_path / "worktrees" / "test_wt"

        git_client.create_worktree(wt_path, "feature/test-wt", "origin/main")

        assert wt_path.exists()
        assert (wt_path / "README.md").exists()
        # ブランチが作成されていること
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=wt_path, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "feature/test-wt"

    def test_creates_worktree_with_existing_branch(self, git_client: GitClient, tmp_path: Path):
        """既存ブランチで worktree を作成できる"""
        # まず通常のブランチを作成
        subprocess.run(
            ["git", "branch", "feature/existing", "origin/main"],
            cwd=git_client.repo_path, check=True, capture_output=True,
        )

        wt_path = tmp_path / "worktrees" / "existing_wt"
        git_client.create_worktree(wt_path, "feature/existing", "origin/main")

        assert wt_path.exists()
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=wt_path, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "feature/existing"

    def test_recreates_if_path_exists(self, git_client: GitClient, tmp_path: Path):
        """既存パスがある場合は削除して再作成する"""
        wt_path = tmp_path / "worktrees" / "recreate_wt"

        # 1回目
        git_client.create_worktree(wt_path, "feature/recreate", "origin/main")
        assert wt_path.exists()

        # 2回目（同じパスに再作成）
        git_client.create_worktree(wt_path, "feature/recreate", "origin/main")
        assert wt_path.exists()

    def test_parent_directory_created(self, git_client: GitClient, tmp_path: Path):
        """親ディレクトリが存在しなくても自動作成される"""
        wt_path = tmp_path / "deep" / "nested" / "worktree"

        git_client.create_worktree(wt_path, "feature/nested", "origin/main")

        assert wt_path.exists()


class TestRemoveWorktree:
    """remove_worktree() のテスト"""

    def test_removes_worktree(self, git_client: GitClient, tmp_path: Path):
        """worktree を正常に削除できる"""
        wt_path = tmp_path / "worktrees" / "to_remove"
        git_client.create_worktree(wt_path, "feature/remove-me", "origin/main")
        assert wt_path.exists()

        git_client.remove_worktree(wt_path)

        assert not wt_path.exists()

    def test_force_remove(self, git_client: GitClient, tmp_path: Path):
        """force 指定で未コミット変更がある worktree も削除できる"""
        wt_path = tmp_path / "worktrees" / "force_remove"
        git_client.create_worktree(wt_path, "feature/force-rm", "origin/main")

        # worktree 内に未コミット変更を作成
        (wt_path / "dirty.txt").write_text("dirty")

        git_client.remove_worktree(wt_path, force=True)

        assert not wt_path.exists()

    def test_remove_nonexistent_is_safe(self, git_client: GitClient, tmp_path: Path):
        """存在しない worktree の削除はエラーにならない"""
        # エラーが出ても例外にならないことを確認
        git_client.remove_worktree(tmp_path / "nonexistent", force=True)


class TestIsWorktree:
    """is_worktree() のテスト"""

    def test_detects_existing_worktree(self, git_client: GitClient, tmp_path: Path):
        """worktree として登録されたパスを正しく検出する"""
        wt_path = tmp_path / "worktrees" / "detect_wt"
        git_client.create_worktree(wt_path, "feature/detect", "origin/main")

        assert git_client.is_worktree(wt_path) is True

    def test_returns_false_for_non_worktree(self, git_client: GitClient, tmp_path: Path):
        """worktree でないパスは False を返す"""
        assert git_client.is_worktree(tmp_path / "nonexistent") is False

    def test_returns_false_after_removal(self, git_client: GitClient, tmp_path: Path):
        """削除後は False を返す"""
        wt_path = tmp_path / "worktrees" / "removed_wt"
        git_client.create_worktree(wt_path, "feature/removed", "origin/main")
        git_client.remove_worktree(wt_path)

        assert git_client.is_worktree(wt_path) is False
