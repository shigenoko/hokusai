"""
Tests for issue comment handling

Issue comment（PR全体へのコメント）の取得・返信・統合処理のテスト。
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock

from hokusai.integrations.git_hosting.base import ReviewComment as ReviewCommentDC
from hokusai.integrations.git_hosting.github import GitHubHostingClient
from hokusai.state import (
    WorkflowState,
    ReviewComment,
    PullRequestInfo,
    PRStatus,
)


class TestReviewCommentType:
    """ReviewComment の comment_type フィールドのテスト"""

    def test_dataclass_default_type(self):
        """デフォルトの comment_type は 'review'"""
        comment = ReviewCommentDC(id=1, body="test")
        assert comment.comment_type == "review"

    def test_dataclass_issue_type(self):
        """comment_type に 'issue' を設定できる"""
        comment = ReviewCommentDC(id=1, body="test", comment_type="issue")
        assert comment.comment_type == "issue"

    def test_to_dict_includes_comment_type(self):
        """to_dict に comment_type が含まれる"""
        comment = ReviewCommentDC(id=1, body="test", comment_type="issue")
        d = comment.to_dict()
        assert d["comment_type"] == "issue"

    def test_from_dict_with_comment_type(self):
        """from_dict で comment_type が復元される"""
        d = {"id": 1, "body": "test", "comment_type": "issue"}
        comment = ReviewCommentDC.from_dict(d)
        assert comment.comment_type == "issue"

    def test_from_dict_default_comment_type(self):
        """from_dict で comment_type がない場合はデフォルト 'review'"""
        d = {"id": 1, "body": "test"}
        comment = ReviewCommentDC.from_dict(d)
        assert comment.comment_type == "review"


class TestGitHubGetIssueComments:
    """GitHubHostingClient.get_issue_comments() のテスト"""

    def _make_client(self):
        """テスト用クライアントを作成"""
        client = GitHubHostingClient(owner="test-owner", repo="test-repo")
        return client

    def _mock_api_response(self, comments):
        """GitHub API レスポンスをモック"""
        return Mock(stdout=json.dumps(comments))

    def test_basic_retrieval(self):
        """正常取得: issue comment を取得できる"""
        client = self._make_client()
        api_comments = [
            {
                "id": 100,
                "body": "設計に問題があります",
                "user": {"login": "reviewer1"},
            },
            {
                "id": 101,
                "body": "テストが不足しています",
                "user": {"login": "reviewer2"},
            },
        ]

        with patch.object(
            client, "_get_shell", return_value=Mock()
        ):
            with patch(
                "hokusai.integrations.git_hosting.github.ShellRunner"
            ) as mock_shell_cls:
                mock_shell = Mock()
                mock_shell.run_gh.return_value = self._mock_api_response(api_comments)
                mock_shell_cls.return_value = mock_shell

                result = client.get_issue_comments(42)

        assert len(result) == 2
        assert result[0].id == 100
        assert result[0].body == "設計に問題があります"
        assert result[0].comment_type == "issue"
        assert result[0].path is None
        assert result[0].line is None
        assert result[1].id == 101

    def test_exclude_reply_marker_comments(self):
        """返信マーカー付きコメントは除外される（投稿者名に依存しない）"""
        client = self._make_client()
        api_comments = [
            {
                "id": 100,
                "body": "設計に問題があります",
                "user": {"login": "reviewer1"},
            },
            {
                "id": 200,
                "body": "<!-- hokusai-reply-to: 100 -->\n修正しました。",
                "user": {"login": "some-user"},  # 著者名にhokusaiを含まない
            },
        ]

        with patch(
            "hokusai.integrations.git_hosting.github.ShellRunner"
        ) as mock_shell_cls:
            mock_shell = Mock()
            mock_shell.run_gh.return_value = self._mock_api_response(api_comments)
            mock_shell_cls.return_value = mock_shell

            result = client.get_issue_comments(42)

        assert len(result) == 1
        assert result[0].id == 100
        assert result[0].replied is True  # マーカーで返信済み判定

    def test_other_bots_not_excluded(self):
        """hokusai 以外の bot は除外しない（devin-ai-integration[bot] 等）"""
        client = self._make_client()
        api_comments = [
            {
                "id": 100,
                "body": "設計に問題があります",
                "user": {"login": "reviewer1"},
            },
            {
                "id": 101,
                "body": "Devin からの指摘です",
                "user": {"login": "devin-ai-integration[bot]"},
            },
            {
                "id": 102,
                "body": "別の bot コメント",
                "user": {"login": "github-actions[bot]"},
            },
        ]

        with patch(
            "hokusai.integrations.git_hosting.github.ShellRunner"
        ) as mock_shell_cls:
            mock_shell = Mock()
            mock_shell.run_gh.return_value = self._mock_api_response(api_comments)
            mock_shell_cls.return_value = mock_shell

            result = client.get_issue_comments(42)

        assert len(result) == 3
        assert result[0].id == 100
        assert result[1].id == 101
        assert result[2].id == 102

    def test_replied_detection(self):
        """返信済み判定: 返信マーカーで replied を検出（著者名非依存）"""
        client = self._make_client()
        api_comments = [
            {
                "id": 100,
                "body": "設計に問題があります",
                "user": {"login": "reviewer1"},
            },
            {
                "id": 101,
                "body": "テストが不足しています",
                "user": {"login": "reviewer2"},
            },
            {
                "id": 200,
                "body": "<!-- hokusai-reply-to: 100 -->\n修正しました。",
                "user": {"login": "some-user"},  # 著者名にhokusaiを含まなくてもOK
            },
        ]

        with patch(
            "hokusai.integrations.git_hosting.github.ShellRunner"
        ) as mock_shell_cls:
            mock_shell = Mock()
            mock_shell.run_gh.return_value = self._mock_api_response(api_comments)
            mock_shell_cls.return_value = mock_shell

            result = client.get_issue_comments(42)

        assert len(result) == 2
        # ID=100 は返信済み
        assert result[0].id == 100
        assert result[0].replied is True
        # ID=101 は未返信
        assert result[1].id == 101
        assert result[1].replied is False

    def test_exclude_authors(self):
        """著者フィルタ: exclude_authors で除外"""
        client = self._make_client()
        api_comments = [
            {
                "id": 100,
                "body": "Copilot suggestion",
                "user": {"login": "copilot[bot]"},
            },
            {
                "id": 101,
                "body": "Human review",
                "user": {"login": "reviewer1"},
            },
        ]

        with patch(
            "hokusai.integrations.git_hosting.github.ShellRunner"
        ) as mock_shell_cls:
            mock_shell = Mock()
            mock_shell.run_gh.return_value = self._mock_api_response(api_comments)
            mock_shell_cls.return_value = mock_shell

            result = client.get_issue_comments(42, exclude_authors=["copilot"])

        assert len(result) == 1
        assert result[0].id == 101


class TestGitHubReplyToIssueComment:
    """GitHubHostingClient.reply_to_issue_comment() のテスト"""

    def test_success(self):
        """正常に投稿できる"""
        client = GitHubHostingClient(owner="test-owner", repo="test-repo")

        with patch(
            "hokusai.integrations.git_hosting.github.ShellRunner"
        ) as mock_shell_cls:
            mock_shell = Mock()
            mock_shell.run_gh.return_value = Mock(stdout="")
            mock_shell_cls.return_value = mock_shell

            result = client.reply_to_issue_comment(42, "修正しました。")

        assert result is True
        # API呼び出しの確認
        mock_shell.run_gh.assert_called_once()
        call_args = mock_shell.run_gh.call_args
        assert "repos/test-owner/test-repo/issues/42/comments" in call_args[0]


class TestBuildReviewFixPrompt:
    """_build_review_fix_prompt の issue comment 対応テスト"""

    def test_issue_comment_formatting(self):
        """issue comment のプロンプト整形"""
        from hokusai.nodes.phase8.review_fix import _build_review_fix_prompt

        comments = [
            {
                "path": "src/main.py",
                "line": 10,
                "body": "変数名が不適切です",
                "author": "reviewer1",
                "comment_type": "review",
            },
            {
                "path": None,
                "line": None,
                "body": "設計に問題があります",
                "author": "reviewer2",
                "comment_type": "issue",
            },
        ]

        with patch("hokusai.prompts.get_prompt") as mock_get_prompt:
            mock_get_prompt.return_value = "mocked"
            _build_review_fix_prompt(comments, 42)

            call_args = mock_get_prompt.call_args
            comments_section = call_args[1]["comments_section"]

            # review comment は通常のファイル/行表示
            assert "- ファイル: src/main.py" in comments_section
            assert "- 行: 10" in comments_section

            # issue comment はPR全体表示
            assert "- ファイル: (PR全体への指摘)" in comments_section
            assert "- 行: -" in comments_section


class TestReplyToAllComments:
    """_reply_to_all_comments の issue comment 区別テスト"""

    def test_issue_comment_uses_reply_to_issue_comment(self):
        """issue comment は reply_to_issue_comment を使う"""
        from hokusai.nodes.phase8.comment_handler import _reply_to_all_comments

        mock_git = Mock()
        mock_git.reply_to_issue_comment.return_value = True
        mock_git.reply_to_comment.return_value = True
        mock_git.get_thread_id_for_comment.return_value = "thread-1"
        mock_git.resolve_thread.return_value = True

        comments = [
            ReviewComment(
                id=100,
                thread_id=None,
                body="設計に問題があります",
                path=None,
                line=None,
                replied=False,
                resolved=False,
                fix_summary=None,
                comment_type="issue",
                author="reviewer1",
            ),
            ReviewComment(
                id=200,
                thread_id=None,
                body="変数名が不適切です",
                path="src/main.py",
                line=10,
                replied=False,
                resolved=False,
                fix_summary=None,
            ),
        ]

        state = {}
        result = _reply_to_all_comments(
            state, comments, pr_number=42, git_hosting=mock_git
        )

        # issue comment は reply_to_issue_comment で返信
        mock_git.reply_to_issue_comment.assert_called_once()
        call_args = mock_git.reply_to_issue_comment.call_args
        assert call_args[0][0] == 42
        reply_body = call_args[0][1]
        assert "<!-- hokusai-reply-to: 100 -->" in reply_body
        # _generate_issue_comment_reply の出力が含まれる（引用・@メンション）
        assert "@reviewer1" in reply_body
        assert "> 設計に問題があります" in reply_body
        # "修正しました" は含まれない
        assert "修正しました" not in reply_body

        # review comment は reply_to_comment で返信（"修正しました" が含まれる）
        mock_git.reply_to_comment.assert_called_once()
        assert mock_git.reply_to_comment.call_args[0][1] == 200
        review_reply = mock_git.reply_to_comment.call_args[0][2]
        assert "修正しました" in review_reply

        # 両方とも replied=True
        assert result[0]["replied"] is True
        assert result[1]["replied"] is True

    def test_issue_comment_skips_thread_resolution(self):
        """issue comment はスレッド解決をスキップする"""
        from hokusai.nodes.phase8.comment_handler import _reply_to_all_comments

        mock_git = Mock()
        mock_git.reply_to_issue_comment.return_value = True

        comments = [
            ReviewComment(
                id=100,
                thread_id=None,
                body="設計に問題があります",
                path=None,
                line=None,
                replied=False,
                resolved=False,
                fix_summary=None,
                comment_type="issue",
            ),
        ]

        state = {}
        _reply_to_all_comments(
            state, comments, pr_number=42, git_hosting=mock_git,
            resolve_after_reply=True,
        )

        # issue comment ではスレッド解決が呼ばれない
        mock_git.get_thread_id_for_comment.assert_not_called()
        mock_git.resolve_thread.assert_not_called()

    def test_issue_comment_no_action_needed_skips_reply(self):
        """対応不要の issue comment は返信せずスキップする"""
        from hokusai.nodes.phase8.comment_handler import _reply_to_all_comments

        mock_git = Mock()
        mock_git.reply_to_issue_comment.return_value = True

        comments = [
            ReviewComment(
                id=100,
                thread_id=None,
                body="全体的にポジティブなフィードバック",
                path=None,
                line=None,
                replied=False,
                resolved=False,
                fix_summary="全体的なポジティブフィードバックのためスキップ",
                comment_type="issue",
                author="reviewer1",
            ),
            ReviewComment(
                id=200,
                thread_id=None,
                body="重要な指摘があります",
                path=None,
                line=None,
                replied=False,
                resolved=False,
                fix_summary="監査ログを追加しました",
                comment_type="issue",
                author="reviewer2",
            ),
        ]

        state = {}
        result = _reply_to_all_comments(
            state, comments, pr_number=42, git_hosting=mock_git
        )

        # 対応不要コメント: 返信APIは呼ばれないが replied=True
        assert result[0]["replied"] is True
        # 対応が必要なコメント: 返信APIが呼ばれる
        assert result[1]["replied"] is True
        # reply_to_issue_comment は1回だけ（対応が必要な方のみ）
        mock_git.reply_to_issue_comment.assert_called_once()

    def test_no_action_keywords(self):
        """各種スキップキーワードが正しく検出される"""
        from hokusai.nodes.phase8.comment_handler import _is_no_action_needed

        assert _is_no_action_needed({"fix_summary": "対応不要"}) is True
        assert _is_no_action_needed({"fix_summary": "情報のみ。コードスキップ"}) is True
        assert _is_no_action_needed({"fix_summary": "ポジティブフィードバック"}) is True
        assert _is_no_action_needed({"fix_summary": "変更不要です"}) is True
        assert _is_no_action_needed({"fix_summary": "対応なし"}) is True
        # 対応が必要なもの
        assert _is_no_action_needed({"fix_summary": "監査ログを追加しました"}) is False
        assert _is_no_action_needed({"fix_summary": None}) is False
        assert _is_no_action_needed({}) is False


class TestCheckReviewCommentsPhaseIsolation:
    """段階別フロー（Copilot/Human分離）での issue comment 混入防止テスト"""

    def test_copilot_phase_does_not_include_issue_comments(self, minimal_state: WorkflowState):
        """Copilot フェーズでは issue comment の unreplied_count に加算しない"""
        from hokusai.nodes.phase8.review_check import _check_review_comments

        mock_git = Mock()
        # review comment（Copilot のみ返す）
        mock_git.get_review_comments.return_value = [
            ReviewCommentDC(id=1, body="Fix this", author="copilot[bot]"),
        ]
        mock_git.is_changes_requested.return_value = False

        pr = {
            "number": 42,
            "url": "https://github.com/test/repo/pull/42",
            "owner": "test",
            "repo": "repo",
            "copilot_comments": [],
            "issue_comments": [],
        }

        with patch(
            "hokusai.nodes.phase8.review_check._get_git_client_for_pr",
            return_value=mock_git,
        ):
            # exclude_authors=None → Copilot モード
            comments, passed, unreplied_count, _ = \
                _check_review_comments(minimal_state, pr)

        # get_issue_comments は呼ばれない（Copilot フェーズ）
        mock_git.get_issue_comments.assert_not_called()
        # unreplied_count は review comment のみ
        assert unreplied_count == 1

    def test_human_phase_includes_issue_comments(self, minimal_state: WorkflowState):
        """Human フェーズでは issue comment も unreplied_count に加算する"""
        from hokusai.nodes.phase8.review_check import _check_review_comments

        mock_git = Mock()
        mock_git.get_review_comments.return_value = [
            ReviewCommentDC(id=1, body="Fix this", author="reviewer1"),
        ]
        mock_git.get_issue_comments.return_value = [
            ReviewCommentDC(id=100, body="Design issue", author="reviewer2", comment_type="issue"),
        ]
        mock_git.is_changes_requested.return_value = False

        pr = {
            "number": 42,
            "url": "https://github.com/test/repo/pull/42",
            "owner": "test",
            "repo": "repo",
            "human_comments": [],
            "issue_comments": [],
        }

        with patch(
            "hokusai.nodes.phase8.review_check._get_git_client_for_pr",
            return_value=mock_git,
        ):
            # exclude_authors=["copilot"] → Human モード
            comments, passed, unreplied_count, _ = \
                _check_review_comments(
                    minimal_state, pr, exclude_authors=["copilot"],
                )

        # issue comment も取得される
        mock_git.get_issue_comments.assert_called_once()
        # unreplied_count は review + issue
        assert unreplied_count == 2
        assert passed is False


class TestCheckAllReviewComments:
    """_check_all_review_comments の issue comment 統合テスト"""

    def test_includes_issue_comments(self, minimal_state: WorkflowState):
        """issue comment が取得結果に含まれる"""
        from hokusai.nodes.phase8.review_check import _check_all_review_comments

        mock_git = Mock()
        # review comments
        mock_git.get_review_comments.return_value = [
            ReviewCommentDC(id=1, body="Fix this", author="reviewer1"),
        ]
        # issue comments
        mock_git.get_issue_comments.return_value = [
            ReviewCommentDC(id=100, body="Design issue", author="reviewer2", comment_type="issue"),
        ]
        mock_git.is_changes_requested.return_value = False

        pr = {
            "number": 42,
            "url": "https://github.com/test/repo/pull/42",
            "owner": "test",
            "repo": "repo",
            "copilot_comments": [],
            "human_comments": [],
            "issue_comments": [],
        }

        with patch(
            "hokusai.nodes.phase8.review_check._get_git_client_for_pr",
            return_value=mock_git,
        ):
            copilot, human, issue, passed, unreplied, changes = \
                _check_all_review_comments(minimal_state, pr)

        assert len(issue) == 1
        assert issue[0]["comment_type"] == "issue"
        assert issue[0]["body"] == "Design issue"
        assert unreplied == 2  # 1 review + 1 issue
        assert passed is False

    def test_passed_when_all_replied(self, minimal_state: WorkflowState):
        """全コメント返信済みの場合 passed=True"""
        from hokusai.nodes.phase8.review_check import _check_all_review_comments

        mock_git = Mock()
        mock_git.get_review_comments.return_value = [
            ReviewCommentDC(id=1, body="Fix this", author="reviewer1", replied=True),
        ]
        mock_git.get_issue_comments.return_value = [
            ReviewCommentDC(
                id=100, body="Design issue", author="reviewer2",
                comment_type="issue", replied=True,
            ),
        ]
        mock_git.is_changes_requested.return_value = False

        pr = {
            "number": 42,
            "url": "https://github.com/test/repo/pull/42",
            "owner": "test",
            "repo": "repo",
            "copilot_comments": [],
            "human_comments": [],
            "issue_comments": [{"id": 100, "replied": True}],
        }

        with patch(
            "hokusai.nodes.phase8.review_check._get_git_client_for_pr",
            return_value=mock_git,
        ):
            copilot, human, issue, passed, unreplied, changes = \
                _check_all_review_comments(minimal_state, pr)

        assert unreplied == 0
        assert passed is True
