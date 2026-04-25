"""
Tests for hokusai.utils.shell module
"""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from hokusai.utils.shell import (
    ShellError,
    ShellResult,
    ShellRunner,
    get_shell_runner,
)


class TestShellResult:
    """ShellResult dataclass のテスト"""

    def test_success_result(self):
        """正常終了の場合はsuccess=True"""
        result = ShellResult(
            returncode=0,
            stdout="output",
            stderr="",
            command=["echo", "hello"],
            duration_ms=100,
        )
        assert result.success is True
        assert result.output == "output"
        assert result.error_output == ""

    def test_failure_result(self):
        """異常終了の場合はsuccess=False"""
        result = ShellResult(
            returncode=1,
            stdout="",
            stderr="error message",
            command=["false"],
            duration_ms=50,
        )
        assert result.success is False
        assert result.output == ""
        assert result.error_output == "error message"

    def test_non_zero_returncode(self):
        """returncode != 0 の場合はsuccess=False"""
        result = ShellResult(
            returncode=128,
            stdout="",
            stderr="",
            command=["git", "status"],
        )
        assert result.success is False


class TestShellRunner:
    """ShellRunner クラスのテスト"""

    def test_run_success(self):
        """正常終了するコマンドの実行"""
        runner = ShellRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="hello",
                stderr="",
            )

            result = runner.run(["echo", "hello"])

            assert result.success is True
            assert result.stdout == "hello"
            mock_run.assert_called_once()

    def test_run_failure(self):
        """失敗するコマンドの実行（check=False）"""
        runner = ShellRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stdout="",
                stderr="error",
            )

            result = runner.run(["false"])

            assert result.success is False
            assert result.returncode == 1

    def test_run_with_check_raises(self):
        """check=Trueで失敗時はShellErrorを送出"""
        runner = ShellRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stdout="",
                stderr="command not found",
            )

            with pytest.raises(ShellError) as exc_info:
                runner.run(["nonexistent"], check=True)

            assert exc_info.value.result.returncode == 1
            assert "command not found" in str(exc_info.value)

    def test_run_with_cwd(self):
        """作業ディレクトリ指定"""
        runner = ShellRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="",
                stderr="",
            )

            runner.run(["ls"], cwd="/tmp")

            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["cwd"] == "/tmp"

    def test_run_with_default_cwd(self):
        """デフォルト作業ディレクトリ"""
        runner = ShellRunner(cwd="/home/user")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="",
                stderr="",
            )

            runner.run(["pwd"])

            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["cwd"] == Path("/home/user")

    def test_run_timeout(self):
        """タイムアウトの伝播"""
        runner = ShellRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["sleep", "10"],
                timeout=1,
            )

            with pytest.raises(subprocess.TimeoutExpired):
                runner.run(["sleep", "10"], timeout=1)

    def test_run_git(self):
        """run_git ヘルパー"""
        runner = ShellRunner(cwd="/repo")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="main",
                stderr="",
            )

            result = runner.run_git("branch", "--show-current")

            assert result.success is True
            call_args = mock_run.call_args.args[0]
            assert call_args == ["git", "branch", "--show-current"]

    def test_run_gh(self):
        """run_gh ヘルパー"""
        runner = ShellRunner(cwd="/repo")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/123",
                stderr="",
            )

            result = runner.run_gh("pr", "create", "--draft")

            assert result.success is True
            call_args = mock_run.call_args.args[0]
            assert call_args == ["gh", "pr", "create", "--draft"]

    def test_duration_tracking(self):
        """実行時間の計測"""
        runner = ShellRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="",
                stderr="",
            )
            with patch("time.monotonic", side_effect=[0.0, 0.150]):
                result = runner.run(["echo"])

            assert result.duration_ms == 150


class TestGetShellRunner:
    """get_shell_runner 関数のテスト"""

    def test_returns_runner(self):
        """ShellRunnerインスタンスを返す"""
        runner = get_shell_runner()
        assert isinstance(runner, ShellRunner)

    def test_with_cwd_returns_new_instance(self):
        """cwd指定時は新しいインスタンスを返す"""
        runner1 = get_shell_runner()
        runner2 = get_shell_runner(cwd="/tmp")

        assert runner2 is not runner1
        assert runner2.default_cwd == Path("/tmp")


class TestShellError:
    """ShellError のテスト"""

    def test_error_message_includes_command(self):
        """エラーメッセージにコマンドが含まれる"""
        result = ShellResult(
            returncode=1,
            stdout="",
            stderr="file not found",
            command=["cat", "nonexistent.txt"],
        )
        error = ShellError(result)

        assert "cat nonexistent.txt" in str(error)
        assert "file not found" in str(error)

    def test_error_with_custom_message(self):
        """カスタムメッセージ"""
        result = ShellResult(
            returncode=127,
            stdout="",
            stderr="",
            command=["nonexistent"],
        )
        error = ShellError(result, message="Custom error")

        assert str(error) == "Custom error"
