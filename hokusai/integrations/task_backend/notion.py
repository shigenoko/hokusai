"""
Notion Task Client

Claude Code経由でMCP Notion Serverを使用してNotionタスクページを操作する。
"""

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ...config import get_config
from ...logging_config import get_logger
from ...utils.json_parser import extract_json_from_output
from ..claude_code import ClaudeCodeClient
from .base import TaskBackendClient

logger = get_logger("notion_task_client")


class NotionResult(str, Enum):
    """Notion操作の結果ステータス"""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class NotionOperationResult:
    """Notion操作の構造化結果"""
    result: NotionResult
    operation: str  # e.g. "update_status", "prepend_content"
    reason: str | None = None  # error/skip reason

    @property
    def is_success(self) -> bool:
        return self.result == NotionResult.SUCCESS


class NotionTaskClient(TaskBackendClient):
    """Claude Code経由でMCP Notion Serverを操作するクライアント"""

    def __init__(self, claude_client: ClaudeCodeClient | None = None):
        """
        初期化

        Args:
            claude_client: ClaudeCodeClientのインスタンス
        """
        self.claude = claude_client or ClaudeCodeClient()

    @staticmethod
    def _is_skip_notion() -> bool:
        """HOKUSAI_SKIP_NOTION 環境変数が設定されているか確認"""
        return os.environ.get("HOKUSAI_SKIP_NOTION") == "1"

    def _skip_result(self, operation: str) -> NotionOperationResult:
        """スキップ結果を生成しログ出力"""
        reason = "Notion未接続のためスキップ（HOKUSAI_SKIP_NOTION=1）"
        logger.info(f"Notion操作スキップ: {operation} — {reason}")
        print(f"⏭️  Notion操作スキップ: {operation}")
        return NotionOperationResult(
            result=NotionResult.SKIPPED,
            operation=operation,
            reason=reason,
        )

    def fetch_task(self, task_url: str) -> dict[str, Any]:
        """
        Notionタスクページの情報を取得

        Args:
            task_url: NotionタスクページのURL

        Returns:
            タスク情報の辞書
        """
        prompt = f"""
NotionのタスクページのURLを使って、以下の情報をJSON形式で取得してください。
MCP Notion Serverのツール（mcp__notion__notion-fetch）を使用してください。

URL: {task_url}

以下のJSON形式で結果を返してください:
```json
{{
  "url": "タスクURL",
  "title": "タスクタイトル",
  "status": "ステータス",
  "properties": {{}}
}}
```
"""
        try:
            config = get_config()
            result = self.claude.execute_prompt(prompt, timeout=config.command_timeout, allow_mcp_tools=True)
            return self._parse_json_from_output(
                result,
                {
                    "url": task_url,
                    "title": "",
                    "status": "",
                    "properties": {},
                },
            )
        except Exception as e:
            print(f"⚠️ Notionタスク取得エラー: {e}")
            return {
                "url": task_url,
                "title": "",
                "status": "",
                "properties": {},
            }

    def update_status(self, task_url: str, status: str) -> NotionOperationResult:
        """
        Notionタスクのステータスを更新

        Args:
            task_url: NotionタスクページのURL
            status: 新しいステータス

        Returns:
            操作結果（success/failed/skipped）
        """
        operation = "update_status"
        if self._is_skip_notion():
            return self._skip_result(operation)

        prompt = f"""
Notionのタスクページのステータスを更新してください。
MCP Notion Serverのツール（mcp__notion__notion-update-page）を使用してください。

URL: {task_url}
新しいステータス: {status}

注意: ステータスプロパティの名前は「ステータス」または「Status」です。
"""
        try:
            config = get_config()
            self.claude.execute_prompt(prompt, timeout=config.command_timeout, allow_mcp_tools=True)
            print(f"📝 Notionステータスを更新: {status}")
            return NotionOperationResult(result=NotionResult.SUCCESS, operation=operation)
        except Exception as e:
            reason = str(e)
            logger.error(f"Notionステータス更新失敗: {reason}")
            print(f"⚠️ Notionステータス更新エラー: {e}")
            return NotionOperationResult(
                result=NotionResult.FAILED, operation=operation, reason=reason,
            )

    def append_progress(self, task_url: str, content: str) -> NotionOperationResult:
        """
        Notionタスクページに進捗記録を追記（末尾）

        Args:
            task_url: NotionタスクページのURL
            content: 追記する内容（Markdown形式）

        Returns:
            操作結果（success/failed/skipped）
        """
        operation = "append_progress"
        if self._is_skip_notion():
            return self._skip_result(operation)

        # コンテンツをエスケープ
        escaped_content = content.replace('"""', '\\"\\"\\"')

        prompt = f'''
Notionのタスクページの本文に以下の内容を追記してください。
MCP Notion Serverのツールを使用してください。

URL: {task_url}

追記する内容:
"""
{escaped_content}
"""

既存の内容の末尾に追記し、既存の内容は削除しないでください。
'''
        try:
            config = get_config()
            self.claude.execute_prompt(prompt, timeout=config.command_timeout, allow_mcp_tools=True)
            print("📝 Notionに進捗を追記")
            return NotionOperationResult(result=NotionResult.SUCCESS, operation=operation)
        except Exception as e:
            reason = str(e)
            logger.error(f"Notion追記失敗: {reason}")
            print(f"⚠️ Notion追記エラー: {e}")
            return NotionOperationResult(
                result=NotionResult.FAILED, operation=operation, reason=reason,
            )

    def prepend_content(self, task_url: str, content: str) -> NotionOperationResult:
        """
        Notionタスクページの先頭にコンテンツを追記

        Args:
            task_url: NotionタスクページのURL
            content: 先頭に追記する内容（Markdown形式）

        Returns:
            操作結果（success/failed/skipped）
        """
        operation = "prepend_content"
        if self._is_skip_notion():
            return self._skip_result(operation)

        # コンテンツをエスケープ
        escaped_content = content.replace('"""', '\\"\\"\\"')

        prompt = f'''
Notionのタスクページの本文の先頭に以下の内容を追記してください。
MCP Notion Serverのツールを使用してください。

URL: {task_url}

追記する内容:
"""
{escaped_content}
"""

既存の内容の先頭（一番上）に追記し、既存の内容は削除しないでください。
'''
        try:
            config = get_config()
            self.claude.execute_prompt(prompt, timeout=config.command_timeout, allow_mcp_tools=True)
            print("📝 Notionの先頭にコンテンツを追記")
            return NotionOperationResult(result=NotionResult.SUCCESS, operation=operation)
        except Exception as e:
            reason = str(e)
            logger.error(f"Notion先頭追記失敗: {reason}")
            print(f"⚠️ Notion先頭追記エラー: {e}")
            return NotionOperationResult(
                result=NotionResult.FAILED, operation=operation, reason=reason,
            )

    def update_checkboxes(
        self,
        task_url: str,
        completed_items: list[str],
        section_hint: str | None = None,
    ) -> NotionOperationResult:
        """
        Notionタスクページ内のチェックボックスを更新

        Args:
            task_url: NotionタスクページのURL
            completed_items: 完了したアイテムのリスト（部分一致で検索）
            section_hint: チェックボックスを探すセクションのヒント

        Returns:
            操作結果（success/failed/skipped）
        """
        operation = "update_checkboxes"
        if self._is_skip_notion():
            return self._skip_result(operation)

        if not completed_items:
            return NotionOperationResult(
                result=NotionResult.SUCCESS, operation=operation,
                reason="completed_items が空",
            )

        # 完了アイテムをリスト化
        items_list = "\n".join([f"- {item}" for item in completed_items])

        section_context = ""
        if section_hint:
            section_context = f"\n検索対象セクション: 「{section_hint}」セクション内のチェックボックスを優先的に更新してください。"

        prompt = f'''
Notionのタスクページ内のチェックボックスを更新してください。
MCP Notion Serverのツール（mcp__notion__notion-fetch, mcp__notion__notion-update-page）を使用してください。

URL: {task_url}
{section_context}

以下のアイテムに対応するチェックボックスを「完了」（チェック済み）に更新してください。
チェックボックスのテキストと部分一致するものを探してください。

完了したアイテム:
{items_list}

手順:
1. まずページの内容を取得（notion-fetch）
2. チェックボックス（- [ ] 形式）を探す
3. 上記アイテムに部分一致するチェックボックスを - [x] に変更
4. 変更をページに適用（notion-update-page の replace_content_range）

注意:
- 既存のチェック済み項目（- [x]）はそのまま維持
- 部分一致で検索（例: "2.1" は "- [ ] **2.1** UseCase..." にマッチ）
- 変更がない場合は何もしなくてOK
'''
        try:
            config = get_config()
            self.claude.execute_prompt(prompt, timeout=config.command_timeout, allow_mcp_tools=True)
            print(f"📝 Notionのチェックボックスを更新: {len(completed_items)}件")
            return NotionOperationResult(result=NotionResult.SUCCESS, operation=operation)
        except Exception as e:
            reason = str(e)
            logger.error(f"Notionチェックボックス更新失敗: {reason}")
            print(f"⚠️ Notionチェックボックス更新エラー: {e}")
            return NotionOperationResult(
                result=NotionResult.FAILED, operation=operation, reason=reason,
            )

    def get_checkbox_items(
        self,
        task_url: str,
        section_hint: str | None = None,
    ) -> list[dict]:
        """
        Notionタスクページ内のチェックボックス項目を取得

        Args:
            task_url: NotionタスクページのURL
            section_hint: チェックボックスを探すセクションのヒント

        Returns:
            チェックボックス項目のリスト
        """
        section_context = ""
        if section_hint:
            section_context = f"「{section_hint}」セクション内の"

        prompt = f'''
Notionのタスクページから{section_context}チェックボックス項目を取得してください。
MCP Notion Serverのツール（mcp__notion__notion-fetch）を使用してください。

URL: {task_url}

結果をJSON配列で返してください:
```json
[
  {{"text": "チェックボックスのテキスト", "checked": true}},
  {{"text": "別のチェックボックス", "checked": false}}
]
```
'''
        try:
            config = get_config()
            result = self.claude.execute_prompt(prompt, timeout=config.command_timeout, allow_mcp_tools=True)
            return self._parse_json_array_from_output(result, [])
        except Exception as e:
            print(f"⚠️ Notionチェックボックス取得エラー: {e}")
            return []

    def get_section_content(
        self,
        task_url: str,
        section_name: str,
    ) -> str | None:
        """
        Notionタスクページ内の特定セクションのコンテンツを取得

        Args:
            task_url: NotionタスクページのURL
            section_name: セクション名（例: "開発計画", "事前調査結果"）

        Returns:
            セクションのコンテンツ（Markdown形式）、見つからない場合はNone
        """
        prompt = f'''
Notionのタスクページから「{section_name}」セクションのコンテンツを取得してください。
MCP Notion Serverのツール（mcp__notion__notion-fetch）を使用してください。

URL: {task_url}

手順:
1. ページの内容を取得
2. 「## {section_name}」または「# {section_name}」見出しを探す
3. その見出しから次の見出し（## または ---）までの内容を抽出
4. 抽出したMarkdownコンテンツを返す

結果を以下の形式で返してください:
```markdown
（抽出したセクションの内容）
```

セクションが見つからない場合は:
```markdown
NOT_FOUND
```
'''
        try:
            config = get_config()
            result = self.claude.execute_prompt(prompt, timeout=config.command_timeout, allow_mcp_tools=True)
            # ```markdown ... ``` ブロックを検索
            md_match = re.search(r"```markdown\s*([\s\S]*?)\s*```", result)
            if md_match:
                content = md_match.group(1).strip()
                if content == "NOT_FOUND":
                    return None
                return content
            return None
        except Exception as e:
            print(f"⚠️ Notionセクション取得エラー: {e}")
            return None

    def _parse_json_from_output(
        self,
        output: str,
        default: dict[str, Any],
    ) -> dict[str, Any]:
        """出力からJSONオブジェクトを抽出してパース（共通関数への委譲）"""
        return extract_json_from_output(output, expected_type=dict, default=default)

    def _parse_json_array_from_output(
        self,
        output: str,
        default: list,
    ) -> list:
        """出力からJSON配列を抽出してパース（共通関数への委譲）"""
        return extract_json_from_output(output, expected_type=list, default=default)
