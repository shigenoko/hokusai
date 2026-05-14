"""Gemini Client

Google Gemini CLI を使ったクロス LLM レビュー用クライアント。
CodexClient と同パターンで subprocess 経由で実行する。

設計指針（Issue #31 / v0.4.6）:
- cross-review 用途の `review_document()` は CodexClient と同インターフェース
- B 案（主コーディングエージェント抽象化、v0.5.x 予定）で再利用できるよう、
  汎用 `generate()` メソッドも用意（任意プロンプトをテキストで生成）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..logging_config import get_logger

logger = get_logger("gemini")


class GeminiClient:
    """Google Gemini CLI を操作するクライアント"""

    def __init__(self, model: str = "gemini-2.5-pro", timeout: int = 300):
        """初期化

        Args:
            model: 使用する Gemini モデル名（例: "gemini-2.5-pro" / "gemini-1.5-flash"）
            timeout: デフォルトのタイムアウト秒数
        """
        self.model = model
        self.timeout = timeout
        self.gemini_path = self._find_gemini_command()

    @staticmethod
    def _find_gemini_command() -> str:
        """gemini コマンドのパスを検出する。

        優先順位:
        1. 環境変数 `GEMINI_PATH`
        2. PATH 上の `gemini`
        3. 一般的な npm global / Homebrew インストールパス

        見つからなければ `FileNotFoundError` を送出する。
        """
        env_path = os.environ.get("GEMINI_PATH")
        if env_path:
            return env_path

        which_path = shutil.which("gemini")
        if which_path:
            return which_path

        common_paths = [
            Path.home() / ".npm-global/bin/gemini",
            Path("/usr/local/bin/gemini"),
            Path("/opt/homebrew/bin/gemini"),
        ]
        for path in common_paths:
            if path.exists():
                return str(path)

        raise FileNotFoundError(
            "gemini コマンドが見つかりません。Gemini CLI がインストールされて "
            "PATH に通っていることを確認してください "
            "（https://github.com/google-gemini/gemini-cli）。"
        )

    # ------------------------------------------------------------------
    # cross-review 用途（CodexClient と同インターフェース）
    # ------------------------------------------------------------------

    def review_document(
        self,
        document: str,
        review_prompt: str,
        schema_path: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """ドキュメントをレビューする。

        Args:
            document: レビュー対象のドキュメント
            review_prompt: レビュー用のプロンプト
            schema_path: 構造化出力用の JSON スキーマファイルパス（任意）
            timeout: タイムアウト秒数（省略時はデフォルト値）

        Returns:
            レビュー結果の辞書（schemas/review_schema.json と互換）

        Raises:
            TimeoutError: タイムアウト時
            RuntimeError: 実行失敗時
        """
        actual_timeout = timeout or self.timeout

        full_prompt = (
            f"{review_prompt}\n\n"
            "重要: すべての回答（title, description, suggestion, summary）は"
            "日本語で記述してください。\n\n"
            f"---\n\n{document}"
        )

        cmd: list[str] = [
            self.gemini_path,
            "-m", self.model,
            "-p", full_prompt,
        ]
        if schema_path:
            # gemini CLI は構造化出力スキーマを直接サポートしていないため、
            # プロンプトにスキーマを埋め込む形で誘導する（出力パース側で吸収）。
            with Path(schema_path).open(encoding="utf-8") as fh:
                schema_content = fh.read()
            full_prompt += (
                "\n\n出力は以下の JSON スキーマに厳密に従ってください。"
                "前後の説明文や markdown フェンスは付けず、JSON オブジェクトのみ"
                "を返してください:\n\n"
                f"{schema_content}\n"
            )
            cmd = [
                self.gemini_path,
                "-m", self.model,
                "-p", full_prompt,
            ]

        logger.debug(
            "Gemini 実行: model=%s timeout=%ds", self.model, actual_timeout,
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=actual_timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(
                f"Gemini レビューがタイムアウトしました（{actual_timeout} 秒）"
            ) from e

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout
            raise RuntimeError(
                f"Gemini 実行エラー (exit code {result.returncode}): {error_msg}"
            )

        return self._parse_output(result.stdout)

    # ------------------------------------------------------------------
    # B 案で再利用する汎用 generate（Phase 2/3/4 等から呼ばれる想定）
    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        files: list[Path] | None = None,
        timeout: int | None = None,
    ) -> str:
        """任意プロンプトに対する Gemini のテキスト出力を返す。

        B 案（主コーディングエージェント抽象化）で `CodingAgentClient` Protocol
        に適合させるための汎用 API。cross-review 以外の用途を見据えた汎用化。

        Args:
            prompt: Gemini に渡すプロンプト
            files: コンテキストとして渡すファイルパスのリスト（任意）。
                内容を読み込んでプロンプト末尾に追記する。
            timeout: タイムアウト秒数（省略時はデフォルト値）

        Returns:
            Gemini の生のテキスト出力

        Raises:
            TimeoutError: タイムアウト時
            RuntimeError: 実行失敗時
        """
        actual_timeout = timeout or self.timeout

        full_prompt = prompt
        if files:
            for f in files:
                content = Path(f).read_text(encoding="utf-8")
                full_prompt += f"\n\n--- file: {f} ---\n{content}\n"

        cmd = [
            self.gemini_path,
            "-m", self.model,
            "-p", full_prompt,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=actual_timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(
                f"Gemini 実行がタイムアウトしました（{actual_timeout} 秒）"
            ) from e

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout
            raise RuntimeError(
                f"Gemini 実行エラー (exit code {result.returncode}): {error_msg}"
            )

        return result.stdout

    # ------------------------------------------------------------------
    # 出力パース
    # ------------------------------------------------------------------

    def _parse_output(self, output: str) -> dict[str, Any]:
        """Gemini 出力を JSON として解釈する（フォールバック付き）。

        順序:
        1. 全体を json.loads
        2. markdown コードフェンス（```json ... ```）から抽出
        3. 最後の `{ ... }` ブロックを抽出
        4. どれも失敗したらフォールバック dict（parse_error=True）を返す
        """
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass

        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", output, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        brace_start = output.rfind("{")
        brace_end = output.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                return json.loads(output[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning(
            "Gemini 出力を JSON としてパースできません。テキストとして返します。"
        )
        return {
            "findings": [],
            "overall_assessment": "needs_discussion",
            "summary": output[:500],
            "parse_error": True,
        }


# ----------------------------------------------------------------------
# シングルトンファクトリ（CodexClient と同パターン）
# ----------------------------------------------------------------------

_gemini_client: GeminiClient | None = None


def get_gemini_client(
    model: str = "gemini-2.5-pro",
    timeout: int = 300,
) -> GeminiClient:
    """GeminiClient のシングルトンインスタンスを取得する。"""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = GeminiClient(model=model, timeout=timeout)
    return _gemini_client


def reset_gemini_client() -> None:
    """GeminiClient シングルトンをリセットする（テスト用）。"""
    global _gemini_client
    _gemini_client = None
