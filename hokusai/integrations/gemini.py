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

# Gemini CLI のドキュメント URL（複数モジュールで参照される）
GEMINI_CLI_DOCS_URL = "https://github.com/google-gemini/gemini-cli"

# モデル名のバリデーション用パターン:
# Notion / OpenAI / Google のモデル名は英数字 / ハイフン / ドット / アンダースコア
# / コロン / スラッシュで構成される。subprocess に渡す前に検証して、フラグ注入
# （例: "-r maliciousflag"）を防ぐ。
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]+$")


class GeminiClient:
    """Google Gemini CLI を操作するクライアント"""

    def __init__(self, model: str = "gemini-2.5-pro", timeout: int = 300):
        """初期化

        Args:
            model: 使用する Gemini モデル名（例: "gemini-2.5-pro" / "gemini-1.5-flash"）
            timeout: デフォルトのタイムアウト秒数

        Raises:
            ValueError: model 名に不正な文字（フラグ注入の可能性）が含まれる場合
            FileNotFoundError: gemini コマンドが見つからない場合
        """
        if not _MODEL_NAME_PATTERN.match(model):
            raise ValueError(
                f"Gemini model 名に不正な文字が含まれています: {model!r}。"
                "英数字 / ハイフン / ドット / アンダースコア / コロン / スラッシュのみ許容。"
            )
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
            f"PATH に通っていることを確認してください（{GEMINI_CLI_DOCS_URL}）。"
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

        # プロンプトを組み立てる: 共通の review_prompt + ドキュメント、
        # schema_path 指定時はスキーマ要求を末尾に追記。
        full_prompt = (
            f"{review_prompt}\n\n"
            "重要: すべての回答（title, description, suggestion, summary）は"
            "日本語で記述してください。\n\n"
            f"---\n\n{document}"
        )
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

        # 安全性メモ:
        # - shell=False（list 形式の cmd で既定）でフラグ / コマンド注入を防止
        # - self.gemini_path は _find_gemini_command() で検証済みのパス
        # - self.model は __init__ で _MODEL_NAME_PATTERN による whitelist 検証済み
        # - プロンプト（full_prompt）は argv ではなく stdin で渡す。これにより
        #   コマンド引数長制限の回避だけでなく、SonarCloud の taint 解析が懸念する
        #   user-controlled argv 経路を物理的に分離する（pythonsecurity:S6350）
        cmd: list[str] = [self.gemini_path, "-m", self.model]

        logger.debug(
            "Gemini 実行: model=%s timeout=%ds", self.model, actual_timeout,
        )

        try:
            result = self._run_with_stdin_prompt(cmd, full_prompt, actual_timeout)
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

        # 安全性メモ: review_document() と同じく argv には whitelist 済みの値
        # のみ載せ、user-controlled なプロンプトは stdin で渡す。
        cmd = [self.gemini_path, "-m", self.model]

        try:
            result = self._run_with_stdin_prompt(cmd, full_prompt, actual_timeout)
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

    def _run_with_stdin_prompt(
        self,
        cmd: list[str],
        prompt: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        """argv に whitelist 済み値のみを載せ、prompt は stdin で gemini CLI に渡す。

        gemini CLI は `-p <prompt>` の代わりに stdin からプロンプトを読み取れる
        （対話モード / `--prompt -` フラグの代替）。本実装は user-controlled な
        プロンプト内容を argv から完全に切り離し、command 引数経路の taint 流入
        （SonarCloud pythonsecurity:S6350 が指摘するリスク）を解消する。
        """
        # cmd の値はすべてバリデーション済み:
        # - cmd[0]: _find_gemini_command で検証されたパス
        # - cmd[1:]: "-m" + _MODEL_NAME_PATTERN で whitelist された model 名
        # shell=False で実行されるため shell 注入のリスクなし。
        return subprocess.run(  # noqa: S603
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    # ------------------------------------------------------------------
    # 出力パース
    # ------------------------------------------------------------------

    def _parse_output(self, output: str) -> dict[str, Any]:
        """Gemini 出力を JSON として解釈する（フォールバック付き）。

        順序:
        1. 全体を json.loads
        2. markdown コードフェンス（```json ... ```）から抽出
        3. ブレース balance を見て **最初の `{` から対応する `}` まで** を抽出
           （Gemini は前置きの prose のあとに JSON オブジェクトを返すパターンが多く、
           ネストした `{` が含まれるため rfind では partial fragment になる）
        4. どれも失敗したらフォールバック dict（parse_error=True）を返す
        """
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass

        # markdown コードフェンス（```json ... ``` または ``` ... ```）を抽出。
        # ReDoS（python:S5852）対策で regex のラジー量子化子は使わず、
        # 線形時間の str.find ベースで明示的に取り出す。
        fenced = self._extract_fenced_block(output)
        if fenced is not None:
            try:
                return json.loads(fenced)
            except json.JSONDecodeError:
                pass

        extracted = self._extract_first_top_level_object(output)
        if extracted is not None:
            try:
                return json.loads(extracted)
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

    @staticmethod
    def _extract_fenced_block(text: str) -> str | None:
        """markdown コードフェンス内の本文を抽出する（regex なし、O(N)）。

        対応形式:
            ```json
            { ... }
            ```
            または
            ```
            { ... }
            ```

        最初に見つかったフェンスペアの中身を返す。閉じフェンスが無ければ None。
        ReDoS 攻撃面を避けるため re.search の lazy quantifier は使わない。
        """
        fence = "```"
        start = text.find(fence)
        if start == -1:
            return None
        # 言語タグ（"json" 等）と改行をスキップして本文の開始位置を決める
        body_start = text.find("\n", start + len(fence))
        if body_start == -1:
            return None
        body_start += 1  # 改行直後から本文
        end = text.find(fence, body_start)
        if end == -1:
            return None
        # 末尾の改行を除去
        return text[body_start:end].rstrip("\n")

    @staticmethod
    def _extract_first_top_level_object(text: str) -> str | None:
        """`text` 中で最初に現れる top-level `{ ... }` を抽出する。

        ネストした `{ }` を考慮して brace balance を取り、最初の `{` から
        対応する `}` までを返す。文字列リテラル内の `{`/`}` は escape を考慮
        せず単純走査（JSON parser に委ねるため厳密さは json.loads 側で担保）。

        対応する `}` が見つからない（unbalanced）場合は None。
        """
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None


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
