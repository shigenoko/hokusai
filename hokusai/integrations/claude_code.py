"""
Claude Code Client

Claude Codeのスキルを実行するためのクライアント。
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..config import get_config
from ..logging_config import get_logger
from ..utils.shell import ShellRunner

logger = get_logger("claude_code")


class ClaudeCodeClient:
    """Claude Codeを操作するクライアント"""

    def __init__(self, working_dir: str | Path | None = None):
        """
        初期化

        Args:
            working_dir: Claude Code実行時の作業ディレクトリ
        """
        config = get_config()

        if working_dir:
            self.working_dir = Path(working_dir)
        elif config.project_root.exists():
            self.working_dir = config.project_root
        else:
            self.working_dir = Path.cwd()

        # claude コマンドの検出は遅延化（claude 未インストール環境でも初期化を可能にするため）
        self._claude_path: str | None = None

    @property
    def claude_path(self) -> str:
        """claude コマンドの絶対パス（必要時に検出）"""
        if self._claude_path is None:
            self._claude_path = self._find_claude_command()
        return self._claude_path

    def execute_skill(
        self,
        skill: str,
        args: str | None = None,
        timeout: int = 300,
        allow_all_permissions: bool = True,
        disallowed_tools: list[str] | None = None,
        append_system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """
        Claude Codeのスキルを実行

        Args:
            skill: スキル名（例: "task-research", "dev-plan", "final-review", "pr-creator"）
            args: スキルに渡す引数
            timeout: タイムアウト秒数
            allow_all_permissions: 全ての権限を許可する場合True（デフォルト）
                スキルは通常ファイル操作やコマンド実行が必要なため、デフォルトでTrue
            disallowed_tools: 使用を禁止するツール名のリスト
            append_system_prompt: システムプロンプトに追記する制約文
                --append-system-prompt フラグとして渡される

        Returns:
            スキルの実行結果
        """
        # スキル実行コマンドを構築
        prompt = f"/{skill}"
        if args:
            prompt += f" {args}"

        try:
            # スキルはファイル操作やMCPツールが必要なことが多いので
            # デフォルトでbypassPermissionsを使用
            permission_mode = "bypassPermissions" if allow_all_permissions else "dontAsk"
            result = self._run_claude_code(
                prompt, timeout,
                permission_mode=permission_mode,
                disallowed_tools=disallowed_tools,
                append_system_prompt=append_system_prompt,
                gateway_purpose=f"skill_execution:{skill}",
            )
            return self._parse_skill_result(skill, result)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"スキル '{skill}' の実行がタイムアウトしました")
        except Exception as e:
            raise RuntimeError(f"スキル '{skill}' の実行に失敗しました: {e}")

    def execute_prompt(
        self,
        prompt: str,
        timeout: int = 300,
        allow_mcp_tools: bool = False,
        allow_file_operations: bool = False,
        disallowed_tools: list[str] | None = None,
    ) -> str:
        """
        任意のプロンプトをClaude Codeに実行させる

        Args:
            prompt: 実行するプロンプト
            timeout: タイムアウト秒数
            allow_mcp_tools: MCPツールへのアクセスを許可する場合True
            allow_file_operations: ファイル操作（Edit, Write, Bash等）を許可する場合True
            disallowed_tools: 使用を禁止するツール名のリスト

        Returns:
            実行結果のテキスト
        """
        try:
            # MCPツールやファイル操作を使う場合はbypassPermissionsモードを使用
            if allow_mcp_tools or allow_file_operations:
                permission_mode = "bypassPermissions"
            else:
                permission_mode = "dontAsk"
            return self._run_claude_code(
                prompt, timeout,
                permission_mode=permission_mode,
                disallowed_tools=disallowed_tools,
                gateway_purpose="execute_prompt",
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError("プロンプトの実行がタイムアウトしました")

    def _find_claude_command(self) -> str:
        """claudeコマンドのパスを検出"""
        import shutil

        # 環境変数から
        if os.environ.get("CLAUDE_PATH"):
            return os.environ["CLAUDE_PATH"]

        # 環境変数PATHから検索
        claude_path = shutil.which("claude")
        if claude_path:
            return claude_path

        # NVM経由でインストールされたclaude（任意のNodeバージョンに対応）
        nvm_dir = Path.home() / ".nvm/versions/node"
        if nvm_dir.exists():
            # Find claude in any installed node version (newest first)
            claude_paths = sorted(nvm_dir.glob("*/bin/claude"), reverse=True)
            for claude_path in claude_paths:
                if claude_path.exists():
                    return str(claude_path)

        # その他の一般的なインストール場所を確認
        common_paths = [
            Path("/usr/local/bin/claude"),
            Path("/opt/homebrew/bin/claude"),
        ]

        for path in common_paths:
            if path.exists():
                return str(path)

        raise FileNotFoundError(
            "claudeコマンドが見つかりません。Claude Codeがインストールされていることを確認してください。"
        )

    def _run_claude_code(
        self,
        prompt: str,
        timeout: int,
        permission_mode: str = "dontAsk",
        disallowed_tools: list[str] | None = None,
        append_system_prompt: str | None = None,
        gateway_purpose: str = "claude_code_invoke",
    ) -> str:
        """
        Claude Codeをサブプロセスとして実行

        Args:
            prompt: 実行するプロンプト
            timeout: タイムアウト秒数
            permission_mode: パーミッションモード
            disallowed_tools: 使用を禁止するツール名のリスト
            append_system_prompt: システムプロンプトに追記するテキスト
            gateway_purpose: LLM Gateway interceptor に渡す purpose 識別子
                （"skill_execution" / "execute_prompt" 等）。Phase 1 は log のみ

        Returns:
            実行結果の標準出力
        """
        # LLM Gateway interceptor (#39 / Phase 1: log-only)
        # Gateway が無効化されている、または例外が出た場合でも既存フローには
        # 一切影響を与えないため、ここで try/except する。
        self._invoke_llm_gateway_interceptor(
            prompt, gateway_purpose, append_system_prompt=append_system_prompt
        )

        cmd = [
            self.claude_path,
            "-p", prompt,
            "--output-format", "text",
            "--permission-mode", permission_mode,
        ]
        if disallowed_tools:
            cmd.extend(["--disallowed-tools", ",".join(disallowed_tools)])
        if append_system_prompt:
            cmd.extend(["--append-system-prompt", append_system_prompt])

        shell = ShellRunner(cwd=self.working_dir)
        result = shell.run(cmd, timeout=timeout)

        logger.info(
            f"Claude Code実行完了: exit={result.returncode}, "
            f"stdout={len(result.stdout)}chars, stderr={len(result.stderr)}chars, "
            f"duration={result.duration_ms}ms"
        )
        if result.stderr:
            logger.warning(f"Claude Code stderr: {result.stderr[:500]}")
        if len(result.stdout) < 100:
            logger.warning(f"Claude Code stdout (short): {repr(result.stdout)}")

        if not result.success:
            error_msg = result.stderr or result.stdout
            raise RuntimeError(
                f"Claude Code実行エラー (exit code {result.returncode}): {error_msg}"
            )

        return result.stdout

    @staticmethod
    def _invoke_llm_gateway_interceptor(
        prompt: str,
        purpose: str,
        *,
        append_system_prompt: str | None = None,
        workflow_id: str | None = None,
        phase: int | None = None,
    ) -> None:
        """LLM Gateway interceptor を呼んで decision を log する（#39 / Phase 1）。

        共通 dispatch helper への薄いラッパー。provider="claude_code"
        固定（Claude Code CLI 経由なので）、model は本 client 側では取得
        できないため空文字（interceptor 側で空 model は evaluation skip）。
        `append_system_prompt` 指定時はその hash/length を metadata に積み、
        CLI に追加される内容と audit が一致するようにする（PR #40 Copilot
        1 回目指摘）。詳細な safety pattern は
        `hokusai.llm_gateway.dispatch_via_gateway` の docstring を参照
        （PR #66 で 3 client から DRY 化）。
        """
        try:
            # 軽量パス（dispatch module）から直接 import し、`__init__.py` の
            # policy/decisions 等まで巻き込まれる依存を避ける。import 時例外
            # （循環参照 / 依存欠落）も含めて握り潰すため try/except で囲む
            # （Issue #66 Copilot Round 2 指摘）。
            from ..llm_gateway.dispatch import (
                dispatch_via_gateway,
                log_suppressed_exception,
            )

            metadata: dict[str, object] = {}
            if append_system_prompt:
                # encode が失敗（不正サロゲート等で UnicodeEncodeError）した
                # 場合でもワークフローを落とさないよう metadata 構築自体を
                # try/except で握り潰し、helper 呼び出しは継続する。
                # append_system_prompt の hash/length は audit の補助情報で
                # あり、欠落しても interceptor 配線全体は機能する。
                try:
                    import hashlib

                    encoded = append_system_prompt.encode("utf-8")
                    metadata["append_system_prompt_length"] = len(
                        append_system_prompt
                    )
                    metadata["append_system_prompt_hash"] = hashlib.sha256(
                        encoded
                    ).hexdigest()[:16]
                except Exception as exc:
                    log_suppressed_exception(
                        "append_system_prompt の hash 計算に失敗。metadata を省略して継続",
                        exc,
                    )

            dispatch_via_gateway(
                provider="claude_code",
                model="",
                purpose=purpose,
                prompt=prompt,
                metadata=metadata,
                workflow_id=workflow_id,
                phase=phase,
            )
        except Exception as exc:
            # Issue #102 / Phase 2 enforcement 本体配線:
            # 意図的な policy block (LLMGatewayBlockedError) は握り潰さず
            # 上位伝播し、LLM 実送信を中断する。それ以外の例外は M1.3 §4.4
            # fail-open 原則に従って従来通り握り潰す（既存フロー影響ゼロ）。
            is_policy_block = False
            try:
                from ..llm_gateway.dispatch import LLMGatewayBlockedError
                is_policy_block = isinstance(exc, LLMGatewayBlockedError)
            except ImportError:
                # 古い環境で LLMGatewayBlockedError が未配置のケースは
                # 「block 機能なし」とみなし fail-open 経路で続行。
                pass

            if is_policy_block:
                raise

            # 第一選択: dispatch.log_suppressed_exception で 3 client 統一。
            # import 失敗時のみ inline fallback（Issue #66 Copilot Round 5）。
            try:
                from ..llm_gateway.dispatch import log_suppressed_exception

                log_suppressed_exception(
                    "LLM Gateway interceptor 呼び出しに失敗", exc
                )
            except Exception:
                logger.debug(
                    "LLM Gateway interceptor 呼び出しに失敗 (type=%s)",
                    type(exc).__name__,
                )

    def _parse_skill_result(self, skill: str, output: str) -> dict[str, Any]:
        """
        スキル実行結果をパース

        Args:
            skill: スキル名
            output: 実行結果のテキスト

        Returns:
            パースされた結果の辞書
        """
        # スキルごとに結果のパース方法を変える
        if skill == "task-research":
            return {
                "research_report": output,
                "design_document": self._extract_design_section(output),
            }
        elif skill == "dev-plan":
            return {
                "work_plan": output,
                "checklist": self._extract_checklist(output),
            }
        elif skill == "final-review":
            issues = self._extract_issues(output)
            # 問題が検出されておらず、明示的な失敗表現がなければ合格
            has_failure = any(
                phrase in output
                for phrase in ["違反があります", "問題があります", "失敗", "エラー"]
            )
            # 合格を示す表現があるか、問題がなく失敗表現もない場合は合格
            passed = (
                "違反なし" in output
                or "合格" in output
                or "パス" in output
                or (len(issues) == 0 and not has_failure)
            )
            return {
                "passed": passed,
                "issues": issues,
            }
        elif skill == "pr-creator":
            return {
                "pr_url": self._extract_pr_url(output),
                "pr_number": self._extract_pr_number(output),
            }
        else:
            return {"output": output}

    def _extract_design_section(self, output: str) -> str:
        """設計セクションを抽出"""
        # TODO: 実際のフォーマットに合わせて実装
        return output

    def _extract_checklist(self, output: str) -> list[str]:
        """チェックリストを抽出"""
        checklist = []
        for line in output.split("\n"):
            if line.strip().startswith("- [ ]") or line.strip().startswith("- [x]"):
                checklist.append(line.strip())
        return checklist

    def _extract_issues(self, output: str) -> list[str]:
        """問題点を抽出"""
        issues = []
        in_issues_section = False
        for line in output.split("\n"):
            if "違反" in line or "問題" in line:
                in_issues_section = True
            if in_issues_section and line.strip().startswith("-"):
                issues.append(line.strip()[1:].strip())
        return issues

    def _extract_pr_url(self, output: str) -> str | None:
        """PR URLを抽出"""
        import re
        match = re.search(r"https://github\.com/[^/]+/[^/]+/pull/\d+", output)
        return match.group(0) if match else None

    def _extract_pr_number(self, output: str) -> int | None:
        """PR番号を抽出

        以下のパターンを順に試す:
        1. PR URL (https://github.com/.../pull/123) から抽出
        2. #番号 形式から抽出
        """
        import re

        # PR URLから抽出（最も信頼性が高い）
        url_match = re.search(r"https://github\.com/[^/]+/[^/]+/pull/(\d+)", output)
        if url_match:
            return int(url_match.group(1))

        # #番号 形式から抽出
        hash_match = re.search(r"#(\d+)", output)
        if hash_match:
            return int(hash_match.group(1))

        return None


class ClaudeCodeHumanInTheLoop:
    """
    Human-in-the-loop用のClaude Code連携

    Phase 5（実装フェーズ）で使用。
    ワークフロー状態をファイルに保存し、手動でClaude Codeを起動して
    実装作業を行う。
    """

    def __init__(self, state_dir: str | None = None):
        """
        初期化

        Args:
            state_dir: 状態ファイルの保存ディレクトリ
        """
        self.state_dir = Path(state_dir or os.path.expanduser("~/.hokusai"))
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def save_implementation_context(
        self,
        workflow_id: str,
        task_url: str,
        work_plan: str,
        checklist: list[str],
    ) -> Path:
        """
        実装コンテキストをファイルに保存

        Args:
            workflow_id: ワークフローID
            task_url: NotionタスクURL
            work_plan: 作業計画
            checklist: チェックリスト

        Returns:
            保存されたコンテキストファイルのパス
        """
        context = {
            "workflow_id": workflow_id,
            "task_url": task_url,
            "work_plan": work_plan,
            "checklist": checklist,
            "status": "waiting_for_implementation",
        }

        context_file = self.state_dir / f"{workflow_id}_context.json"
        with open(context_file, "w", encoding="utf-8") as f:
            json.dump(context, f, ensure_ascii=False, indent=2)

        return context_file

    def check_implementation_complete(self, workflow_id: str) -> bool:
        """
        実装が完了したかチェック

        Args:
            workflow_id: ワークフローID

        Returns:
            完了している場合True
        """
        context_file = self.state_dir / f"{workflow_id}_context.json"
        if not context_file.exists():
            return False

        with open(context_file, "r", encoding="utf-8") as f:
            context = json.load(f)

        return context.get("status") == "implementation_complete"

    def mark_implementation_complete(self, workflow_id: str) -> None:
        """
        実装完了としてマーク

        Args:
            workflow_id: ワークフローID
        """
        context_file = self.state_dir / f"{workflow_id}_context.json"
        if not context_file.exists():
            raise FileNotFoundError(f"コンテキストファイルが見つかりません: {context_file}")

        with open(context_file, "r", encoding="utf-8") as f:
            context = json.load(f)

        context["status"] = "implementation_complete"

        with open(context_file, "w", encoding="utf-8") as f:
            json.dump(context, f, ensure_ascii=False, indent=2)
