"""
Codex Client

OpenAI Codex CLIを使ったクロスLLMレビュー用クライアント。
ClaudeCodeClientと同パターンでsubprocess経由で実行する。
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..logging_config import get_logger

logger = get_logger("codex")


class CodexClient:
    """OpenAI Codexを操作するクライアント"""

    def __init__(self, model: str = "codex-mini-latest", timeout: int = 300):
        """
        初期化

        Args:
            model: 使用するCodexモデル
            timeout: デフォルトのタイムアウト秒数
        """
        self.model = model
        self.timeout = timeout
        self.codex_path = self._find_codex_command()

    def _find_codex_command(self) -> str:
        """codexコマンドのパスを検出"""
        import shutil

        # 環境変数から
        if os.environ.get("CODEX_PATH"):
            return os.environ["CODEX_PATH"]

        # 環境変数PATHから検索
        codex_path = shutil.which("codex")
        if codex_path:
            return codex_path

        # npm global インストール場所を確認
        common_paths = [
            Path.home() / ".npm-global/bin/codex",
            Path("/usr/local/bin/codex"),
            Path("/opt/homebrew/bin/codex"),
        ]

        for path in common_paths:
            if path.exists():
                return str(path)

        raise FileNotFoundError(
            "codexコマンドが見つかりません。OpenAI Codex CLIがインストールされていることを確認してください。"
        )

    def review_document(
        self,
        document: str,
        review_prompt: str,
        schema_path: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """
        ドキュメントをレビューする

        Args:
            document: レビュー対象のドキュメント
            review_prompt: レビュー用のプロンプト
            schema_path: 構造化出力用のJSONスキーマファイルパス
            timeout: タイムアウト秒数（省略時はデフォルト値）

        Returns:
            レビュー結果の辞書

        Raises:
            TimeoutError: タイムアウト時
            RuntimeError: 実行失敗時
        """
        actual_timeout = timeout or self.timeout

        # プロンプトを構築（ドキュメントをコンテキストとして含める）
        full_prompt = (
            f"{review_prompt}\n\n"
            "重要: すべての回答（title, description, suggestion, summary）は日本語で記述してください。\n\n"
            f"---\n\n{document}"
        )

        # LLM Gateway interceptor (Issue #62 / Phase 1: log-only)
        # provider="codex" / model=self.model / purpose="cross_review" として
        # 評価する。Gateway 無効化や例外時は副作用なしで透過する。
        # `has_schema` は実 CLI 呼び出し（後段の `if schema_path:`）と判定基準を
        # 揃えるため `bool(schema_path)` で評価する。空文字列も「未指定」と
        # 同等に扱い、audit metadata と実 invocation の意味を一致させる
        # （PR #63 Copilot Round 3 指摘）。
        self._invoke_llm_gateway_interceptor(
            full_prompt, has_schema=bool(schema_path)
        )

        cmd = [
            self.codex_path,
            "exec",
            full_prompt,
            "--model", self.model,
        ]

        if schema_path:
            cmd.extend(["--output-schema", schema_path])

        logger.debug(f"Codex実行: model={self.model}, timeout={actual_timeout}s")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=actual_timeout,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"Codexレビューがタイムアウトしました（{actual_timeout}秒）"
            )

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout
            raise RuntimeError(
                f"Codex実行エラー (exit code {result.returncode}): {error_msg}"
            )

        # JSON出力をパース
        return self._parse_output(result.stdout)

    def _invoke_llm_gateway_interceptor(
        self, prompt: str, *, has_schema: bool
    ) -> None:
        """LLM Gateway interceptor を呼ぶ（Issue #62 / Phase 1: log-only）。

        ClaudeCodeClient._invoke_llm_gateway_interceptor と同じ safety pattern:
        - Gateway が無効化（enabled=False）なら interceptor 内部で no-op
        - audit_log_enabled なら構造化 log entry が logger に流れる
        - decision は Phase 1 では常に "log" / "skipped" で値は使わない
        - **既存フローへの影響をゼロにするため例外は完全に握り潰す**。
          stack trace は `exc_info=True` で debug ログにのみ残し、メッセージ
          経由で secret/PII が log にこぼれないよう本文は出さない。

        provider="codex" / model=self.model を context に詰めるため、PR #61 で
        追加した policy_hits 評価（unknown_model / high_cost_model）が
        Codex callsite 経由で初めて意味を持つ。
        """
        try:
            from ..config import get_config
            from ..llm_gateway import LLMGatewayContext, LLMGatewayInterceptor

            config = get_config()
            gateway_config = getattr(config, "llm_gateway", None)
            if gateway_config is None:
                return
            context = LLMGatewayContext(
                provider="codex",
                model=self.model,
                purpose="cross_review",
                metadata={"has_schema": has_schema},
            )
            LLMGatewayInterceptor(gateway_config).intercept(context, prompt)
        except Exception:
            logger.debug("LLM Gateway interceptor 内例外を抑制", exc_info=True)

    def _parse_output(self, output: str) -> dict[str, Any]:
        """Codex出力をパース"""
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            # JSON以外の出力が含まれる場合、JSONブロックを抽出
            return self._extract_json_from_output(output)

    def _extract_json_from_output(self, output: str) -> dict[str, Any]:
        """出力からJSONブロックを抽出"""
        # ```json ... ``` ブロックを探す
        import re
        match = re.search(r"```json\s*\n(.*?)\n```", output, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 最後の { ... } ブロックを試す
        brace_start = output.rfind("{")
        brace_end = output.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                return json.loads(output[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        # パースできない場合はテキストをそのまま返す
        logger.warning("Codex出力をJSONとしてパースできません。テキストとして返します。")
        return {
            "findings": [],
            "overall_assessment": "needs_discussion",
            "summary": output[:500],
            "parse_error": True,
        }


# ファクトリ関数
_codex_client: CodexClient | None = None


def get_codex_client(
    model: str = "codex-mini-latest",
    timeout: int = 300,
) -> CodexClient:
    """CodexClientのシングルトンインスタンスを取得"""
    global _codex_client
    if _codex_client is None:
        _codex_client = CodexClient(model=model, timeout=timeout)
    return _codex_client


def reset_codex_client() -> None:
    """CodexClientをリセット（テスト用）"""
    global _codex_client
    _codex_client = None
