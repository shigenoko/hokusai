"""GitHubIssueClient のテスト（Issue #73）

検証項目:
- update_status がラベル不在で graceful degrade（例外を投げず FAILED 結果を返す）
- update_status が成功時 SUCCESS 結果を返す
- Issue 番号が解決できない URL でも例外を投げず FAILED 結果を返す
- 既存ラベル削除エラーは継続（既存挙動の維持）
- print されるメッセージで失敗事実をユーザーに伝える
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hokusai.integrations.task_backend.github_issue import (
    GitHubIssueClient,
    GitHubIssueOperationResult,
    GitHubIssueResult,
)
from hokusai.utils.shell import ShellError, ShellResult


def _make_shell_error(stderr: str, returncode: int = 1) -> ShellError:
    """ShellError を本物の `ShellResult` から組み立てる（Issue #73）。

    `ShellError.__init__` は `result.command` (list[str]) を `' '.join()` する
    ため、MagicMock で `cmd: str` を持たせるだけでは `TypeError` で例外生成
    自体が失敗し、テストが不安定になる。本物の `ShellResult` を使って
    interface 準拠を保証する。
    """
    result = ShellResult(
        returncode=returncode,
        stdout="",
        stderr=stderr,
        command=["gh", "issue", "edit", "42", "--add-label", "進行中"],
    )
    return ShellError(result)


class TestUpdateStatusGracefulDegrade:
    """update_status の graceful degrade 動作（Issue #73）"""

    def test_returns_success_when_label_added(self, capsys):
        """ラベル追加成功時 SUCCESS 結果"""
        client = GitHubIssueClient(repo="owner/repo")

        with patch(
            "hokusai.integrations.task_backend.github_issue.ShellRunner"
        ) as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner
            # remove + add すべて成功
            mock_runner.run_gh.return_value = MagicMock(
                returncode=0, stdout="", stderr=""
            )

            result = client.update_status(
                "https://github.com/owner/repo/issues/42", "進行中"
            )

        assert isinstance(result, GitHubIssueOperationResult)
        assert result.result == GitHubIssueResult.SUCCESS
        assert result.operation == "update_status"
        assert result.is_success
        captured = capsys.readouterr()
        assert "📝" in captured.out
        assert "進行中" in captured.out

    def test_returns_failed_when_label_does_not_exist(self, capsys):
        """ラベル不在で gh issue edit が失敗 → FAILED 結果（例外なし）"""
        client = GitHubIssueClient(repo="owner/repo")

        with patch(
            "hokusai.integrations.task_backend.github_issue.ShellRunner"
        ) as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

            def _side_effect(*args, **kwargs):
                # remove-label は check=False で握り潰される想定。
                # add-label のみ失敗させる。
                if "--add-label" in args:
                    raise _make_shell_error("'進行中' not found")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_runner.run_gh.side_effect = _side_effect

            # 例外を投げないこと
            result = client.update_status(
                "https://github.com/owner/repo/issues/42", "進行中"
            )

        assert result.result == GitHubIssueResult.FAILED
        assert result.operation == "update_status"
        assert "進行中" in result.reason
        assert "not found" in result.reason or "ラベル" in result.reason
        captured = capsys.readouterr()
        # ユーザーに失敗事実が見える
        assert "⚠️" in captured.out
        assert "継続" in captured.out

    def test_returns_failed_when_url_invalid(self, capsys):
        """URL から Issue 番号を解決できない → FAILED 結果（例外なし）"""
        client = GitHubIssueClient()

        result = client.update_status("not-a-valid-url", "進行中")

        # update_status は常に GitHubIssueOperationResult を返す（SonarCloud
        # pythonbugs:S2259 対策で型を明示的に検証）
        assert isinstance(result, GitHubIssueOperationResult)
        assert result.result == GitHubIssueResult.FAILED
        assert result.reason is not None
        assert "Issue 番号" in result.reason or "Invalid" in result.reason
        captured = capsys.readouterr()
        assert "⚠️" in captured.out
        # メッセージ表現は「失敗（継続）」に統一（Issue #73）
        assert "失敗" in captured.out or "継続" in captured.out

    def test_returns_failed_when_gh_cli_missing(self, capsys):
        """gh CLI が未インストール → FileNotFoundError でも graceful degrade
        （Issue #73: ShellError 以外も catch する）"""
        client = GitHubIssueClient(repo="owner/repo")

        with patch(
            "hokusai.integrations.task_backend.github_issue.ShellRunner"
        ) as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

            def _side_effect(*args, **kwargs):
                # remove-label は check=False で握り潰される想定。
                # add-label のみ FileNotFoundError を発生させる。
                if "--add-label" in args:
                    raise FileNotFoundError("gh: command not found")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_runner.run_gh.side_effect = _side_effect

            # 例外を投げないこと
            result = client.update_status(
                "https://github.com/owner/repo/issues/42", "進行中"
            )

        assert isinstance(result, GitHubIssueOperationResult)
        assert result.result == GitHubIssueResult.FAILED
        assert result.reason is not None
        assert "FileNotFoundError" in result.reason
        captured = capsys.readouterr()
        assert "⚠️" in captured.out
        assert "継続" in captured.out

    def test_remove_label_includes_status_mapping_values(self):
        """status_mapping を渡すと、その値（例 「進行中」「完了」）が削除
        対象に含まれる（Issue #73 Copilot 指摘: 日本語ラベルが残らないように）"""
        client = GitHubIssueClient(
            repo="owner/repo",
            status_mapping={
                "in_progress": "進行中",
                "reviewing": "対応待ち/レビュー中",
                "done": "完了",
            },
        )

        with patch(
            "hokusai.integrations.task_backend.github_issue.ShellRunner"
        ) as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

            remove_targets: list[str] = []

            def _side_effect(*args, **kwargs):
                if "--remove-label" in args:
                    idx = args.index("--remove-label")
                    remove_targets.append(args[idx + 1])
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_runner.run_gh.side_effect = _side_effect

            client.update_status(
                "https://github.com/owner/repo/issues/42", "進行中"
            )

        # 日本語ラベルが削除対象に含まれる（"進行中" 自身は除外 = これから追加するため）
        assert "対応待ち/レビュー中" in remove_targets
        assert "完了" in remove_targets
        assert "進行中" not in remove_targets
        # 英語 fallback も含まれる
        assert "in-progress" in remove_targets
        assert "done" in remove_targets

    def test_unexpected_exception_message_is_truncated(self, capsys):
        """想定外例外の reason はメッセージ先頭 200 文字に truncate
        （Issue #73 Copilot 指摘: コメント「短く要約」と実装の整合）"""
        client = GitHubIssueClient(repo="owner/repo")

        with patch(
            "hokusai.integrations.task_backend.github_issue.ShellRunner"
        ) as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

            long_msg = "X" * 500

            def _side_effect(*args, **kwargs):
                if "--add-label" in args:
                    raise RuntimeError(long_msg)
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_runner.run_gh.side_effect = _side_effect

            result = client.update_status(
                "https://github.com/owner/repo/issues/42", "進行中"
            )

        assert result.result == GitHubIssueResult.FAILED
        # reason に含まれる 'X' の数は 200 以下
        assert result.reason.count("X") <= 200
        assert result.reason.count("X") >= 100  # 短すぎないこと


    def test_remove_label_failures_are_silent(self):
        """既存ラベル削除での失敗は継続（HOKUSAI を初めて入れた環境で
        in-progress/done/reviewing/open が無いケースの維持）"""
        client = GitHubIssueClient(repo="owner/repo")

        with patch(
            "hokusai.integrations.task_backend.github_issue.ShellRunner"
        ) as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

            call_log = []

            def _side_effect(*args, **kwargs):
                call_log.append(args)
                if "--remove-label" in args:
                    # 削除は失敗しても継続
                    if kwargs.get("check") is False:
                        return MagicMock(returncode=1, stdout="", stderr="not found")
                    raise _make_shell_error("not found")
                if "--add-label" in args:
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_runner.run_gh.side_effect = _side_effect

            result = client.update_status(
                "https://github.com/owner/repo/issues/42", "進行中"
            )

        # 削除失敗は無視され、追加が成功すれば SUCCESS
        assert result.result == GitHubIssueResult.SUCCESS
        # 4 個の status label + 1 個の add で計 5 回呼ばれる
        remove_calls = [c for c in call_log if "--remove-label" in c]
        add_calls = [c for c in call_log if "--add-label" in c]
        assert len(remove_calls) == 4
        assert len(add_calls) == 1


class TestGitHubIssueOperationResult:
    """GitHubIssueOperationResult の dataclass 動作"""

    def test_is_success_true_for_success(self):
        result = GitHubIssueOperationResult(
            result=GitHubIssueResult.SUCCESS, operation="update_status"
        )
        assert result.is_success

    def test_is_success_false_for_failed(self):
        result = GitHubIssueOperationResult(
            result=GitHubIssueResult.FAILED,
            operation="update_status",
            reason="label missing",
        )
        assert not result.is_success

    def test_is_success_false_for_skipped(self):
        result = GitHubIssueOperationResult(
            result=GitHubIssueResult.SKIPPED, operation="update_status"
        )
        assert not result.is_success


class TestPhase1PrepareIntegration:
    """phase1_prepare との接続: update_status の戻り値で audit log に
    記録できる仕組みが回ること（Notion パターンと同じ）"""

    def test_result_object_has_audit_compatible_fields(self):
        """phase1_prepare.py の `hasattr(result, 'result')` 分岐で audit log を
        残せる形式になっていることを検証"""
        result = GitHubIssueOperationResult(
            result=GitHubIssueResult.FAILED,
            operation="update_status",
            reason="label missing",
        )
        assert hasattr(result, "result")
        # phase1_prepare は result.result.value を audit log に書く
        assert result.result.value == "failed"
        # reason 属性も audit log の error フィールドに渡される
        assert hasattr(result, "reason")
        assert result.reason == "label missing"
