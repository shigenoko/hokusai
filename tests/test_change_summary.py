"""hokusai/utils/change_summary.py のテスト"""

from unittest.mock import patch, MagicMock

from hokusai.utils.change_summary import (
    build_repo_change_summary,
    build_pr_change_summary,
    build_combined_change_summary,
)


class TestBuildRepoChangeSummary:
    """build_repo_change_summary のテスト"""

    @patch("hokusai.utils.change_summary.GitClient")
    def test_returns_empty_for_no_changes(self, mock_git_cls):
        """差分なしで空文字列を返す"""
        mock_git = MagicMock()
        mock_git.get_diff_files.return_value = []
        mock_git_cls.return_value = mock_git

        result = build_repo_change_summary("/repo", "main", repo_name="Test")
        assert result == ""

    @patch("hokusai.utils.change_summary.GitClient")
    def test_generates_markdown_with_files(self, mock_git_cls):
        """変更ファイルがある場合にMarkdownを生成"""
        mock_git = MagicMock()
        mock_git.get_diff_files.return_value = ["src/foo.py", "src/bar.ts"]
        mock_git.get_diff_stat.return_value = " 2 files changed, 10 insertions(+)"
        mock_git.get_file_diff.return_value = "+new line\n-old line"
        mock_git_cls.return_value = mock_git

        result = build_repo_change_summary("/repo", "main", repo_name="Backend")

        assert "### Backend" in result
        assert "`src/foo.py`" in result
        assert "`src/bar.ts`" in result

    @patch("hokusai.utils.change_summary.GitClient")
    def test_respects_max_files(self, mock_git_cls):
        """max_files を超えるファイルを切り詰め"""
        mock_git = MagicMock()
        mock_git.get_diff_files.return_value = [f"file{i}.py" for i in range(50)]
        mock_git.get_diff_stat.return_value = ""
        mock_git.get_file_diff.return_value = ""
        mock_git_cls.return_value = mock_git

        result = build_repo_change_summary("/repo", "main", max_files=5)
        # 5件のみ表示
        assert result.count("`file") == 5


class TestBuildPrChangeSummary:
    """build_pr_change_summary のテスト"""

    @patch("hokusai.utils.change_summary.build_repo_change_summary")
    def test_generates_per_repo(self, mock_build):
        """リポジトリごとにサマリーを生成"""
        mock_build.side_effect = lambda **kwargs: f"### {kwargs.get('repo_name', '')}\n- file"

        state = {
            "repositories": [
                {"name": "Backend", "path": "/backend", "base_branch": "beta"},
                {"name": "API", "path": "/api", "base_branch": "main"},
            ],
        }
        result = build_pr_change_summary(state)

        assert "Backend" in result
        assert "API" in result
        assert len(result) == 2

    def test_empty_repositories(self):
        """リポジトリなしで空辞書を返す"""
        result = build_pr_change_summary({"repositories": []})
        assert result == {}


class TestBuildCombinedChangeSummary:
    """build_combined_change_summary のテスト"""

    @patch("hokusai.utils.change_summary.build_pr_change_summary")
    def test_combines_summaries(self, mock_build):
        """複数リポジトリのサマリーを結合"""
        mock_build.return_value = {
            "Backend": "### Backend\n- file1",
            "API": "### API\n- file2",
        }
        result = build_combined_change_summary({})
        assert "### Backend" in result
        assert "### API" in result

    @patch("hokusai.utils.change_summary.build_pr_change_summary")
    def test_returns_empty_for_no_summaries(self, mock_build):
        """サマリーなしで空文字列を返す"""
        mock_build.return_value = {}
        result = build_combined_change_summary({})
        assert result == ""
