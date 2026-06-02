"""
Tests for Copilot code review request

PR 作成時に Copilot へコードレビューを依頼する機能のテスト。
- GitHubHostingClient.request_copilot_review()
- suggestedActors による事前チェック（有効性判定）
- Phase 8a の依頼ヘルパー _request_copilot_reviews()
"""

import json
from unittest.mock import Mock, patch

from hokusai.integrations.git_hosting.base import GitHostingClient
from hokusai.integrations.git_hosting.github import GitHubHostingClient
from hokusai.utils.shell import ShellError, ShellResult


def _make_client() -> GitHubHostingClient:
    """テスト用クライアント（owner/repo 固定で get_repo_info の shell 呼び出しを回避）"""
    return GitHubHostingClient(owner="test-owner", repo="test-repo")


def _suggested_actors_response(*, with_copilot: bool, pr_node_id: str | None = "PR_node1") -> Mock:
    """suggestedActors クエリのレスポンスをモック"""
    nodes = [
        {"login": "alice", "__typename": "User", "id": "USER_alice"},
    ]
    if with_copilot:
        nodes.append(
            {
                "login": "copilot-pull-request-reviewer",
                "__typename": "Bot",
                "id": "BOT_copilot",
            }
        )
    pr = {"id": pr_node_id} if pr_node_id is not None else None
    payload = {
        "data": {
            "repository": {
                "pullRequest": pr,
                "suggestedActors": {"nodes": nodes},
            }
        }
    }
    return Mock(stdout=json.dumps(payload))


def _mutation_ok_response() -> Mock:
    return Mock(stdout=json.dumps({"data": {"requestReviews": {"pullRequest": {"id": "PR_node1"}}}}))


def _shell_error(stderr: str = "boom") -> ShellError:
    return ShellError(
        ShellResult(returncode=1, stdout="", stderr=stderr, command=["gh", "api", "graphql"])
    )


class TestRequestCopilotReview:
    """GitHubHostingClient.request_copilot_review() のテスト"""

    def test_requests_when_available(self):
        """Copilot レビュアーが利用可能なら依頼が送信される"""
        client = _make_client()
        mock_shell = Mock()
        mock_shell.run_gh.side_effect = [
            _suggested_actors_response(with_copilot=True),
            _mutation_ok_response(),
        ]

        with patch.object(client, "_get_shell", return_value=mock_shell):
            result = client.request_copilot_review(42)

        assert result["requested"] is True
        assert result["available"] is True
        assert result["reason"] is None
        # 2回呼ばれる（事前チェック → ミューテーション）
        assert mock_shell.run_gh.call_count == 2
        # ミューテーション呼び出しに bot/PR の node ID が渡る
        mutation_args = mock_shell.run_gh.call_args_list[1].args
        joined = " ".join(mutation_args)
        assert "prId=PR_node1" in joined
        assert "botId=BOT_copilot" in joined

    def test_unavailable_when_no_copilot_actor(self):
        """suggestedActors に Copilot bot がいない場合は未対応として扱う"""
        client = _make_client()
        mock_shell = Mock()
        mock_shell.run_gh.return_value = _suggested_actors_response(with_copilot=False)

        with patch.object(client, "_get_shell", return_value=mock_shell):
            result = client.request_copilot_review(42)

        assert result["requested"] is False
        assert result["available"] is False
        assert "有効化されていません" in result["reason"]
        # 未対応ならミューテーションは呼ばない
        assert mock_shell.run_gh.call_count == 1

    def test_available_but_mutation_fails(self):
        """利用可能だがミューテーションが失敗した場合は requested=False"""
        client = _make_client()
        mock_shell = Mock()
        mock_shell.run_gh.side_effect = [
            _suggested_actors_response(with_copilot=True),
            _shell_error("permission denied"),
        ]

        with patch.object(client, "_get_shell", return_value=mock_shell):
            result = client.request_copilot_review(42)

        assert result["requested"] is False
        assert result["available"] is True
        assert "permission denied" in result["reason"]

    def test_available_but_pr_node_id_missing(self):
        """PR node ID が取得できない場合は依頼しない"""
        client = _make_client()
        mock_shell = Mock()
        mock_shell.run_gh.return_value = _suggested_actors_response(
            with_copilot=True, pr_node_id=None
        )

        with patch.object(client, "_get_shell", return_value=mock_shell):
            result = client.request_copilot_review(42)

        assert result["requested"] is False
        assert result["available"] is True
        # ミューテーションは呼ばない（事前チェックのみ）
        assert mock_shell.run_gh.call_count == 1

    def test_resolve_failure_is_handled(self):
        """事前チェックのクエリが例外を投げても安全に処理する"""
        client = _make_client()
        mock_shell = Mock()
        mock_shell.run_gh.side_effect = _shell_error("network error")

        with patch.object(client, "_get_shell", return_value=mock_shell):
            result = client.request_copilot_review(42)

        assert result["requested"] is False
        assert result["available"] is False
        assert "レビュアー候補取得失敗" in result["reason"]


class TestBaseClientDefault:
    """ベースクラスのデフォルト実装のテスト"""

    def test_default_returns_unavailable(self):
        """デフォルト実装は未対応を返す"""

        class _Dummy(GitHostingClient):
            def get_repo_info(self):
                return ("o", "r")

            def create_draft_pull_request(self, title, body, head_branch, base_branch):
                ...

            def mark_ready_for_review(self, pr_number):
                ...

            def get_review_comments(self, pr_number, exclude_authors=None):
                return []

            def reply_to_comment(self, pr_number, comment_id, body):
                return True

            def resolve_thread(self, thread_id):
                return True

            def get_thread_id_for_comment(self, pr_number, comment_id):
                return None

            def is_changes_requested(self, pr_number):
                return False

            def get_pr_for_branch(self, branch_name):
                return None

        result = _Dummy().request_copilot_review(1)
        assert result["requested"] is False
        assert result["available"] is False


class TestPhase8aRequestHelper:
    """Phase 8a の _request_copilot_reviews() ヘルパーのテスト"""

    def test_requests_for_new_prs(self):
        """新規 PR ごとに依頼が呼ばれる"""
        from hokusai.nodes.phase8.pr_creation import _request_copilot_reviews

        repo = Mock(name="Backend", path="/repo/backend")
        repo.name = "Backend"
        pr_info = {"number": 7, "url": "https://github.com/o/r/pull/7"}

        mock_client = Mock()
        mock_client.request_copilot_review.return_value = {
            "requested": True, "available": True, "reason": None,
        }

        with patch(
            "hokusai.nodes.phase8.pr_creation.GitHubHostingClient",
            return_value=mock_client,
        ):
            _request_copilot_reviews([(repo, pr_info)])

        mock_client.request_copilot_review.assert_called_once_with(7)

    def test_skips_pr_without_number(self):
        """PR番号が無い場合はスキップ"""
        from hokusai.nodes.phase8.pr_creation import _request_copilot_reviews

        repo = Mock()
        repo.name = "Backend"
        pr_info = {"number": 0, "url": "x"}

        mock_client = Mock()
        with patch(
            "hokusai.nodes.phase8.pr_creation.GitHubHostingClient",
            return_value=mock_client,
        ):
            _request_copilot_reviews([(repo, pr_info)])

        mock_client.request_copilot_review.assert_not_called()

    def test_exception_does_not_propagate(self):
        """依頼で例外が出ても workflow を止めない"""
        from hokusai.nodes.phase8.pr_creation import _request_copilot_reviews

        repo = Mock()
        repo.name = "Backend"
        pr_info = {"number": 7, "url": "x"}

        mock_client = Mock()
        mock_client.request_copilot_review.side_effect = RuntimeError("boom")
        with patch(
            "hokusai.nodes.phase8.pr_creation.GitHubHostingClient",
            return_value=mock_client,
        ):
            # 例外が外に伝播しないこと
            _request_copilot_reviews([(repo, pr_info)])
