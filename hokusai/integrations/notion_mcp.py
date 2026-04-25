"""
Notion MCP Client (Claude Code経由)

Claude Code経由でNotion MCPツールを呼び出し、
Notionページにコンテンツを追加する。

Note:
    Claude CodeはNotion OAuth認証を内部的に管理しているため、
    hokusaiから直接MCP APIを呼び出すのではなく、
    Claude Code経由でNotionツールを使用する。
"""

import re

from ..logging_config import get_logger
from .claude_code import ClaudeCodeClient

logger = get_logger("notion_mcp")


class NotionConnectionError(Exception):
    """Notion接続エラー"""

    pass


class NotionMCPClient:
    """Claude Code経由でNotion MCPツールを使用するクライアント"""

    def __init__(self):
        """初期化"""
        self._claude = None

    @property
    def claude(self) -> ClaudeCodeClient:
        """Claude Codeクライアントを遅延初期化"""
        if self._claude is None:
            self._claude = ClaudeCodeClient()
        return self._claude

    def check_connection(self, timeout: int = 120) -> bool:
        """
        Notion接続を確認

        Claude Code経由でNotionツールが使用可能か確認する。

        Args:
            timeout: タイムアウト秒数

        Returns:
            接続成功の場合True

        Raises:
            NotionConnectionError: 接続失敗時
        """
        try:
            # Claude CodeでNotionページを検索してツールが使えるか確認
            prompt = """Notionの接続を確認してください。
mcp__notion__notion-search ツールを使って "test" で検索し、
結果が返ってきたら「Notion接続: OK」と出力してください。
エラーが発生した場合は「Notion接続: エラー」と出力し、エラー内容を表示してください。
"""
            result = self.claude.execute_prompt(prompt, timeout=timeout, allow_mcp_tools=True)

            if "Notion接続: OK" in result or "検索結果" in result or "results" in result.lower():
                logger.info("Notion接続確認: 成功（Claude Code経由）")
                return True

            if "エラー" in result or "error" in result.lower() or "failed" in result.lower():
                error_detail = result[:500] if len(result) > 500 else result
                raise NotionConnectionError(
                    f"Notion接続エラー: {error_detail}\n\n"
                    "Claude CodeでNotionに再認証してください。"
                )

            # 曖昧な結果の場合も成功とみなす（検索結果が返ってきている可能性）
            logger.info("Notion接続確認: 成功（Claude Code経由）")
            return True

        except TimeoutError:
            raise NotionConnectionError(
                f"Notion接続確認がタイムアウトしました（{timeout}秒）"
            )
        except FileNotFoundError as e:
            raise NotionConnectionError(
                f"Claude Codeが見つかりません: {e}\n"
                "Claude Codeがインストールされていることを確認してください。"
            )
        except Exception as e:
            error_msg = str(e)
            if "認証" in error_msg or "auth" in error_msg.lower():
                raise NotionConnectionError(
                    f"Notion認証エラー: {error_msg}\n\n"
                    "Claude CodeでNotionツールを使用して再認証してください。\n"
                    "例: 「Notionのページを検索して」と入力"
                )
            raise NotionConnectionError(f"Notion接続エラー: {error_msg}")

    def _extract_page_id(self, url_or_id: str) -> str:
        """
        NotionページURLまたはIDからページIDを抽出

        Args:
            url_or_id: NotionページURLまたはページID

        Returns:
            ページID（ハイフン付き形式）
        """
        # すでにUUID形式の場合（ハイフン付きまたはなし）
        if re.match(r"^[a-f0-9-]{32,36}$", url_or_id, re.IGNORECASE):
            clean_id = url_or_id.replace("-", "")
            return f"{clean_id[:8]}-{clean_id[8:12]}-{clean_id[12:16]}-{clean_id[16:20]}-{clean_id[20:]}"

        # URLからIDを抽出
        # NotionのURLは末尾に32文字のHEX IDがある形式:
        # https://www.notion.so/workspace/page-title-2f45e03d57e28092bc05e21e932d4a0e
        # 末尾の32文字を取得する（タイトル内のHEX文字と混同しないため）
        clean_url = url_or_id.replace("-", "")
        # 末尾から32文字のHEX文字列を抽出
        match = re.search(r"([a-f0-9]{32})$", clean_url, re.IGNORECASE)
        if match:
            clean_id = match.group(1)
            return f"{clean_id[:8]}-{clean_id[8:12]}-{clean_id[12:16]}-{clean_id[16:20]}-{clean_id[20:]}"

        raise ValueError(f"Invalid Notion page URL or ID: {url_or_id}")

    def insert_after_existing(
        self,
        page_url: str,
        markdown_content: str,
        after_marker: str | None = None,
    ) -> bool:
        """
        Notionページの特定位置の後にコンテンツを挿入

        Claude Code経由でNotion MCPツールを使用して挿入する。

        Args:
            page_url: NotionページURLまたはID
            markdown_content: 挿入するMarkdownコンテンツ
            after_marker: この文字列の後に挿入（省略時は末尾追記）。
                callout 等のコンテナブロック自体を指定すると内側に挿入される
                リスクがあるため、末尾追記（None）を推奨。

        Returns:
            成功した場合 True、失敗または結果不明確の場合 False。
            例外は呼び出し側の except で捕捉される。
        """
        try:
            page_id = self._extract_page_id(page_url)

            # コンテンツをエスケープ（プロンプト内で使用するため）
            escaped_content = markdown_content.replace('"""', '\\"\\"\\"')

            if after_marker:
                # after_marker から selection_with_ellipsis を構築
                if "..." in after_marker:
                    selection_ellipsis = after_marker
                else:
                    marker_start = after_marker[:15] if len(after_marker) > 15 else after_marker
                    marker_end = after_marker[-15:] if len(after_marker) > 15 else after_marker
                    selection_ellipsis = f"{marker_start}...{marker_end}"

                prompt = f"""以下のNotionページにコンテンツを挿入してください。

ページID: {page_id}

挿入位置マーカー（selection_with_ellipsis）: {selection_ellipsis}

挿入するコンテンツ:
\"\"\"
{escaped_content}
\"\"\"

手順:
1. mcp__notion__notion-fetch でページの現在のコンテンツを取得
2. selection_with_ellipsis に一致するブロックを特定
3. mcp__notion__notion-update-page の insert_content_after コマンドで、
   selection_with_ellipsis="{selection_ellipsis}" を指定してコンテンツを挿入

重要:
- コンテンツはマッチしたブロックの兄弟要素（sibling）として直後に追加すること。
- calloutブロックの内側（子要素）には絶対に入れないこと。

成功したら「保存完了」、失敗したら「保存失敗: <理由>」と出力してください。
"""
            else:
                # 末尾に追記（コンテナ内部混入のリスクがない安全なパス）
                prompt = f"""以下のNotionページの末尾にコンテンツを追加してください。

ページID: {page_id}

追加するコンテンツ:
\"\"\"
{escaped_content}
\"\"\"

手順:
1. mcp__notion__notion-fetch でページの現在のコンテンツを取得
2. ページの最後のトップレベルブロックを特定
   （calloutブロック内のブロックではなく、最も外側のブロック）
3. mcp__notion__notion-update-page の insert_content_after コマンドで、
   最後のトップレベルブロックの selection_with_ellipsis を指定してコンテンツを追加

重要:
- calloutブロック（::: callout ... :::）の内側には絶対にコンテンツを入れないこと。
- 必ず callout の閉じタグ（:::）より後に追加すること。

成功したら「保存完了」、失敗したら「保存失敗: <理由>」と出力してください。
"""

            result = self.claude.execute_prompt(prompt, timeout=120, allow_mcp_tools=True)

            if "保存完了" in result or "成功" in result or "updated" in result.lower():
                logger.info(f"Notionページへの保存成功: {page_id}")
                return True

            if "保存失敗" in result or "エラー" in result or "error" in result.lower():
                logger.warning(f"Notionページへの保存失敗: {result[:500]}")
                return False

            # 明確な成功/失敗キーワードがない場合は警告付きで失敗扱い
            logger.warning(
                f"Notionページへの保存結果が不明確（失敗扱い）: {result[:300]}"
            )
            return False

        except Exception as e:
            logger.error(f"Notionへの保存に失敗: {e}")
            raise

    def append_content(self, page_url: str, markdown_content: str) -> bool:
        """
        Notionページの末尾にコンテンツを追記

        Args:
            page_url: NotionページURLまたはID
            markdown_content: 追記するMarkdownコンテンツ

        Returns:
            成功した場合True
        """
        return self.insert_after_existing(page_url, markdown_content, after_marker=None)

    def replace_content_block(self, page_url: str, old_str: str, new_str: str) -> bool:
        """ページ内の既存 Markdown 断片を search-and-replace で置換する。"""
        try:
            page_id = self._extract_page_id(page_url)
            escaped_old = old_str.replace('"""', '\\"\\"\\"')
            escaped_new = new_str.replace('"""', '\\"\\"\\"')
            prompt = f"""以下のNotionページの本文の一部を置換してください。

ページID: {page_id}

置換前:
\"\"\"
{escaped_old}
\"\"\"

置換後:
\"\"\"
{escaped_new}
\"\"\"

手順:
1. mcp__notion__notion-update-page ツールを使用
2. command は "update_content"
3. content_updates に old_str / new_str を1件だけ渡す

成功したら「更新完了」、失敗したら「更新失敗: <理由>」と出力してください。
"""
            result = self.claude.execute_prompt(prompt, timeout=180, allow_mcp_tools=True)
            if "更新完了" in result or "成功" in result or "updated" in result.lower():
                return True
            if "更新失敗" in result or "エラー" in result or "error" in result.lower():
                logger.warning(f"Notion部分更新失敗: {result[:500]}")
                return False
            logger.warning(f"Notion部分更新結果が不明確: {result[:300]}")
            return False
        except Exception as e:
            logger.error(f"Notion部分更新に失敗: {e}")
            raise

    def get_page_content(self, page_url: str) -> str:
        """
        Notionページのコンテンツを取得

        Args:
            page_url: NotionページURLまたはID

        Returns:
            ページコンテンツ（Markdown形式）
        """
        try:
            page_id = self._extract_page_id(page_url)

            prompt = f"""以下のNotionページのコンテンツを取得してください。

ページID: {page_id}

mcp__notion__notion-fetch ツールを使用してコンテンツを取得し、
ページの本文をそのまま出力してください。
"""

            result = self.claude.execute_prompt(prompt, timeout=120, allow_mcp_tools=True)
            return result

        except Exception as e:
            logger.error(f"Notionページの取得に失敗: {e}")
            raise
