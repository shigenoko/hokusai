"""
Shell Runner

外部コマンド実行の統一ラッパー。
subprocess.runの直接呼び出しを集約し、一貫したエラーハンドリングとロギングを提供。
"""

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from hokusai.logging_config import get_logger

logger = get_logger("shell")


@dataclass
class ShellResult:
    """コマンド実行結果"""

    returncode: int
    stdout: str
    stderr: str
    command: list[str]
    duration_ms: int = 0
    success: bool = field(init=False)

    def __post_init__(self):
        self.success = self.returncode == 0

    @property
    def output(self) -> str:
        """stdoutを返す（互換性のため）"""
        return self.stdout

    @property
    def error_output(self) -> str:
        """stderrを返す（互換性のため）"""
        return self.stderr


class ShellError(Exception):
    """シェルコマンド実行エラー"""

    def __init__(self, result: ShellResult, message: str | None = None):
        self.result = result
        msg = message or f"Command failed: {' '.join(result.command)}"
        if result.stderr:
            msg += f"\nstderr: {result.stderr[:500]}"
        super().__init__(msg)


class ShellRunner:
    """外部コマンド実行の統一ラッパー"""

    DEFAULT_TIMEOUT = 120  # 2分

    def __init__(self, cwd: Path | str | None = None):
        """
        初期化

        Args:
            cwd: デフォルトの作業ディレクトリ
        """
        self.default_cwd = Path(cwd) if cwd else None

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | str | None = None,
        timeout: int | None = None,
        check: bool = False,
        capture_output: bool = True,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> ShellResult:
        """
        コマンドを実行

        Args:
            cmd: コマンドと引数のリスト
            cwd: 作業ディレクトリ（省略時はdefault_cwd）
            timeout: タイムアウト秒数（省略時はDEFAULT_TIMEOUT）
            check: Trueの場合、失敗時にShellErrorを送出
            capture_output: Trueの場合、stdout/stderrをキャプチャ
            env: 追加の環境変数
            input_text: プロセスの標準入力に渡すテキスト

        Returns:
            ShellResult: 実行結果

        Raises:
            ShellError: check=Trueで実行失敗時
            subprocess.TimeoutExpired: タイムアウト時
        """
        work_dir = cwd or self.default_cwd
        actual_timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT

        logger.debug(f"Executing: {' '.join(cmd)}")
        if work_dir:
            logger.debug(f"  cwd: {work_dir}")

        start_time = time.monotonic()

        try:
            proc_result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=capture_output,
                text=True,
                timeout=actual_timeout,
                env=env,
                input=input_text,
            )
        except subprocess.TimeoutExpired:
            # コマンド文字列が長い場合（-p でプロンプト全文を含む等）は先頭のみ表示
            cmd_str = ' '.join(cmd)
            if len(cmd_str) > 200:
                cmd_str = cmd_str[:200] + f"... ({len(cmd_str)} chars)"
            logger.warning(f"Command timed out after {actual_timeout}s: {cmd_str}")
            raise

        duration_ms = int((time.monotonic() - start_time) * 1000)

        result = ShellResult(
            returncode=proc_result.returncode,
            stdout=proc_result.stdout or "",
            stderr=proc_result.stderr or "",
            command=cmd,
            duration_ms=duration_ms,
        )

        if result.success:
            logger.debug(f"Command succeeded in {duration_ms}ms")
        else:
            logger.debug(
                f"Command failed with code {result.returncode} in {duration_ms}ms"
            )
            if result.stderr:
                logger.debug(f"  stderr: {result.stderr[:200]}")

        if check and not result.success:
            raise ShellError(result)

        return result

    def run_git(
        self,
        *args: str,
        cwd: Path | str | None = None,
        timeout: int | None = None,
        check: bool = False,
    ) -> ShellResult:
        """
        Gitコマンドを実行

        Args:
            *args: Gitコマンドの引数（"commit", "-m", "message"など）
            cwd: 作業ディレクトリ
            timeout: タイムアウト秒数
            check: Trueの場合、失敗時にShellErrorを送出

        Returns:
            ShellResult: 実行結果
        """
        cmd = ["git", *args]
        return self.run(cmd, cwd=cwd, timeout=timeout, check=check)

    def run_gh(
        self,
        *args: str,
        cwd: Path | str | None = None,
        timeout: int | None = None,
        check: bool = False,
    ) -> ShellResult:
        """
        GitHub CLIコマンドを実行

        Args:
            *args: ghコマンドの引数（"pr", "create", "--draft"など）
            cwd: 作業ディレクトリ
            timeout: タイムアウト秒数
            check: Trueの場合、失敗時にShellErrorを送出

        Returns:
            ShellResult: 実行結果
        """
        cmd = ["gh", *args]
        return self.run(cmd, cwd=cwd, timeout=timeout, check=check)

    def run_claude(
        self,
        prompt: str,
        *,
        cwd: Path | str | None = None,
        timeout: int = 600,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 16000,
    ) -> ShellResult:
        """
        Claude Codeコマンドを実行

        Args:
            prompt: Claude Codeに渡すプロンプト
            cwd: 作業ディレクトリ
            timeout: タイムアウト秒数（デフォルト10分）
            model: 使用するモデル
            max_tokens: 最大トークン数

        Returns:
            ShellResult: 実行結果
        """
        cmd = [
            "claude",
            "-p", prompt,
            "--model", model,
            "--output-format", "text",
            "--max-turns", "50",
        ]
        return self.run(cmd, cwd=cwd, timeout=timeout, check=False)


# モジュールレベルのデフォルトインスタンス
_default_runner: ShellRunner | None = None


def get_shell_runner(cwd: Path | str | None = None) -> ShellRunner:
    """
    ShellRunnerインスタンスを取得

    Args:
        cwd: 作業ディレクトリ（指定時は新規インスタンスを作成）

    Returns:
        ShellRunner: ランナーインスタンス
    """
    global _default_runner
    if cwd:
        return ShellRunner(cwd)
    if _default_runner is None:
        _default_runner = ShellRunner()
    return _default_runner
