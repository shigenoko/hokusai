"""
ベースブランチ同期・構造差分検出のテスト

既存ブランチ再利用時の base 整合チェック機能を検証する。
.gitmodules 差分や submodule 構造変更による fail-fast を含む。
"""

import subprocess
from pathlib import Path

import pytest

from hokusai.integrations.git import GitClient, BranchReuseDenied


def _git(cwd: Path, *args: str) -> str:
    """テスト用 git コマンド実行ヘルパー"""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """origin (bare) + clone のペアを作成

    Returns:
        (clone_path, bare_path)
    """
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=beta", str(bare)],
        check=True, capture_output=True,
    )

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(bare), str(clone)],
        check=True, capture_output=True,
    )
    _git(clone, "config", "user.name", "Test")
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "checkout", "-b", "beta")

    # 初期コミット
    (clone / "README.md").write_text("initial")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "initial commit")
    _git(clone, "push", "-u", "origin", "beta")

    return clone, bare


@pytest.fixture
def git_client(repo_with_remote: tuple[Path, Path]) -> GitClient:
    clone, _ = repo_with_remote
    return GitClient(str(clone))


class TestFetchBaseBranch:
    """fetch_base_branch() のテスト"""

    def test_fetches_base_branch(self, git_client: GitClient):
        """origin/<base_branch> を fetch できる"""
        # エラーなく完了すること
        git_client.fetch_base_branch("beta")

    def test_handles_origin_prefix(self, git_client: GitClient):
        """origin/ 接頭辞付きでも動作する"""
        git_client.fetch_base_branch("origin/beta")

    def test_nonexistent_branch_does_not_raise(self, git_client: GitClient):
        """存在しないブランチでも例外にならない（warning のみ）"""
        git_client.fetch_base_branch("nonexistent-branch")


class TestSyncLocalBaseBranch:
    """sync_local_base_branch() のテスト"""

    def test_syncs_local_to_origin(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """ローカルブランチが origin に同期される"""
        clone, bare = repo_with_remote

        # beta から離れる（branch -f は checkout 中のブランチには使えない）
        _git(clone, "checkout", "--detach", "HEAD")

        # origin に新しいコミットを追加（別 clone 経由）
        clone2 = clone.parent / "clone2"
        subprocess.run(
            ["git", "clone", str(bare), str(clone2)],
            check=True, capture_output=True,
        )
        _git(clone2, "config", "user.name", "Test")
        _git(clone2, "config", "user.email", "test@test.com")
        (clone2 / "new_file.txt").write_text("new")
        _git(clone2, "add", ".")
        _git(clone2, "commit", "-m", "new commit on beta")
        _git(clone2, "push", "origin", "beta")

        # fetch してから sync
        git_client.fetch_base_branch("beta")
        git_client.sync_local_base_branch("beta")

        # ローカル beta が origin/beta と同じコミットを指すこと
        local_sha = _git(clone, "rev-parse", "beta")
        origin_sha = _git(clone, "rev-parse", "origin/beta")
        assert local_sha == origin_sha


class TestGetBranchAheadBehind:
    """get_branch_ahead_behind() のテスト"""

    def test_new_branch_is_zero_zero(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """base と同じ地点のブランチは (0, 0)"""
        clone, _ = repo_with_remote
        _git(clone, "branch", "feature/test", "origin/beta")
        git_client.fetch_base_branch("beta")

        ahead, behind = git_client.get_branch_ahead_behind("feature/test", "beta")
        assert ahead == 0
        assert behind == 0

    def test_ahead_branch(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """独自コミットがある branch は ahead > 0"""
        clone, _ = repo_with_remote
        _git(clone, "checkout", "-b", "feature/ahead")
        (clone / "ahead.txt").write_text("ahead")
        _git(clone, "add", ".")
        _git(clone, "commit", "-m", "ahead commit")
        _git(clone, "checkout", "beta")
        git_client.fetch_base_branch("beta")

        ahead, behind = git_client.get_branch_ahead_behind("feature/ahead", "beta")
        assert ahead == 1
        assert behind == 0

    def test_behind_branch(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """base が進んだ branch は behind > 0"""
        clone, bare = repo_with_remote

        # feature branch を作成
        _git(clone, "branch", "feature/behind", "origin/beta")

        # origin に新しいコミットを追加
        clone2 = clone.parent / "clone2"
        subprocess.run(
            ["git", "clone", str(bare), str(clone2)],
            check=True, capture_output=True,
        )
        _git(clone2, "config", "user.name", "Test")
        _git(clone2, "config", "user.email", "test@test.com")
        (clone2 / "behind.txt").write_text("new on beta")
        _git(clone2, "add", ".")
        _git(clone2, "commit", "-m", "beta advanced")
        _git(clone2, "push", "origin", "beta")

        git_client.fetch_base_branch("beta")

        ahead, behind = git_client.get_branch_ahead_behind("feature/behind", "beta")
        assert ahead == 0
        assert behind == 1


class TestDetectBaseStructureConflicts:
    """detect_base_structure_conflicts() のテスト"""

    def test_no_conflicts_when_identical(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """base と同じ状態のブランチは空リスト"""
        clone, _ = repo_with_remote
        _git(clone, "branch", "feature/clean", "origin/beta")
        git_client.fetch_base_branch("beta")

        issues = git_client.detect_base_structure_conflicts("feature/clean", "beta")
        assert issues == []

    def test_detects_gitmodules_diff(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """.gitmodules 差分を検出する（submodule 廃止ケース再現）"""
        clone, bare = repo_with_remote

        # feature branch で .gitmodules を作成
        _git(clone, "checkout", "-b", "feature/old-submodule")
        (clone / ".gitmodules").write_text(
            '[submodule "lib/schema"]\n\tpath = lib/schema\n\turl = git@github.com:org/schema.git\n'
        )
        _git(clone, "add", ".gitmodules")
        _git(clone, "commit", "-m", "add submodule config")
        _git(clone, "checkout", "beta")

        # origin/beta では .gitmodules なし → feature branch のみに存在
        git_client.fetch_base_branch("beta")

        issues = git_client.detect_base_structure_conflicts(
            "feature/old-submodule", "beta"
        )
        assert len(issues) >= 1
        assert any(".gitmodules" in issue for issue in issues)

    def test_detects_gitmodules_removal_on_base(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """base 側で .gitmodules が空にされた場合の差分を検出"""
        clone, bare = repo_with_remote

        # beta に .gitmodules を追加してプッシュ
        (clone / ".gitmodules").write_text(
            '[submodule "lib/api"]\n\tpath = lib/api\n\turl = git@github.com:org/api.git\n'
        )
        _git(clone, "add", ".gitmodules")
        _git(clone, "commit", "-m", "add gitmodules")
        _git(clone, "push", "origin", "beta")

        # feature branch を作成（.gitmodules あり）
        _git(clone, "checkout", "-b", "feature/has-submodule")
        (clone / "feature.txt").write_text("feature work")
        _git(clone, "add", ".")
        _git(clone, "commit", "-m", "feature work")

        # beta で .gitmodules を空にしてプッシュ（submodule 取り込み完了を模擬）
        _git(clone, "checkout", "beta")
        (clone / ".gitmodules").write_text("")
        _git(clone, "add", ".gitmodules")
        _git(clone, "commit", "-m", "remove submodule, inline into repo")
        _git(clone, "push", "origin", "beta")

        git_client.fetch_base_branch("beta")

        issues = git_client.detect_base_structure_conflicts(
            "feature/has-submodule", "beta"
        )
        assert len(issues) >= 1
        assert any(".gitmodules" in issue for issue in issues)


class TestValidateBranchReuseAgainstBase:
    """validate_branch_reuse_against_base() のテスト"""

    def test_clean_branch_passes(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """base と整合した branch は空リスト（問題なし）"""
        clone, _ = repo_with_remote
        _git(clone, "branch", "feature/ok", "origin/beta")

        messages = git_client.validate_branch_reuse_against_base("feature/ok", "beta")
        assert messages == []

    def test_gitmodules_diff_triggers_error(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path]
    ):
        """.gitmodules 差分がある branch はエラーメッセージを返す"""
        clone, _ = repo_with_remote
        _git(clone, "checkout", "-b", "feature/bad-submodule")
        (clone / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = y\n')
        _git(clone, "add", ".gitmodules")
        _git(clone, "commit", "-m", "add gitmodules")
        _git(clone, "checkout", "beta")

        messages = git_client.validate_branch_reuse_against_base(
            "feature/bad-submodule", "beta"
        )
        assert len(messages) >= 1
        assert any(".gitmodules" in m for m in messages)


class TestCreateWorktreeWithBaseGuard:
    """create_worktree() の base 整合ガードのテスト"""

    def test_new_branch_from_latest_base(
        self, git_client: GitClient, tmp_path: Path
    ):
        """新規ブランチは最新 origin/<base> から作成される"""
        wt_path = tmp_path / "wt" / "new"
        git_client.create_worktree(wt_path, "feature/new-branch", "origin/beta")

        assert wt_path.exists()
        assert (wt_path / "README.md").exists()

    def test_clean_existing_branch_allowed(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ):
        """base と整合した既存ブランチは worktree 作成できる"""
        clone, _ = repo_with_remote
        _git(clone, "branch", "feature/clean-reuse", "origin/beta")

        wt_path = tmp_path / "wt" / "clean"
        git_client.create_worktree(wt_path, "feature/clean-reuse", "origin/beta")

        assert wt_path.exists()

    def test_gitmodules_diff_blocks_worktree(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ):
        """.gitmodules 差分がある既存ブランチは BranchReuseDenied で停止"""
        clone, _ = repo_with_remote
        _git(clone, "checkout", "-b", "feature/blocked")
        (clone / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = y\n')
        _git(clone, "add", ".gitmodules")
        _git(clone, "commit", "-m", "add gitmodules")
        _git(clone, "checkout", "beta")

        wt_path = tmp_path / "wt" / "blocked"
        with pytest.raises(BranchReuseDenied) as exc_info:
            git_client.create_worktree(wt_path, "feature/blocked", "origin/beta")

        assert exc_info.value.branch_name == "feature/blocked"
        assert exc_info.value.base_branch == "beta"
        assert any(".gitmodules" in issue for issue in exc_info.value.issues)
        # worktree は作成されていないこと
        assert not wt_path.exists()

    def test_error_message_includes_next_action(
        self, git_client: GitClient, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ):
        """エラーメッセージに推奨アクションが含まれる"""
        clone, _ = repo_with_remote
        _git(clone, "checkout", "-b", "feature/msg-test")
        (clone / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = y\n')
        _git(clone, "add", ".gitmodules")
        _git(clone, "commit", "-m", "add gitmodules")
        _git(clone, "checkout", "beta")

        wt_path = tmp_path / "wt" / "msg"
        with pytest.raises(BranchReuseDenied) as exc_info:
            git_client.create_worktree(wt_path, "feature/msg-test", "origin/beta")

        msg = str(exc_info.value)
        assert "git fetch origin beta" in msg
        assert "git rebase origin/beta" in msg
        assert "git merge origin/beta" in msg


class TestPhase1BranchReuseError:
    """Phase 1 の _format_branch_reuse_error() のテスト"""

    def test_format_includes_repo_and_actions(self):
        """整形メッセージに repo名・branch名・next action が含まれる"""
        from hokusai.nodes.phase1_prepare import _format_branch_reuse_error

        error = BranchReuseDenied(
            "feature/old",
            "beta",
            [".gitmodules が origin/beta と不一致"],
        )

        msg = _format_branch_reuse_error("Backend", error)
        assert "Backend" in msg
        assert "feature/old" in msg
        assert "origin/beta" in msg
        assert "git fetch origin beta" in msg
        assert "git rebase origin/beta" in msg
        assert "hokusai continue" in msg
