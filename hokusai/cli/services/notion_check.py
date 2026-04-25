"""
Notion Connection Check Service

Notion MCPへの接続を確認するサービス。
"""
import os

from ...ui.console import (
    print_notion_checking,
    print_notion_connection_error,
    print_notion_continue_no,
    print_notion_continue_prompt,
    print_notion_continue_yes,
    print_notion_dry_run,
    print_notion_environment_error,
    print_notion_ok,
    print_notion_unexpected_error,
)


def check_notion_connection(dry_run: bool = False) -> tuple[bool, bool]:
    """
    Notion接続を確認

    Args:
        dry_run: ドライランモードの場合はスキップ

    Returns:
        (接続可能か, 続行可能か) のタプル
        - (True, True): 接続OK、続行OK
        - (False, True): 接続NG、ユーザーが続行を選択
        - (False, False): 接続NG、続行しない
    """
    if dry_run:
        print_notion_dry_run()
        return True, True

    print_notion_checking()

    try:
        from ...integrations.notion_mcp import NotionConnectionError, NotionMCPClient

        client = NotionMCPClient()
        client.check_connection()
        print_notion_ok()
        return True, True

    except NotionConnectionError as e:
        print_notion_connection_error(str(e))

    except FileNotFoundError as e:
        print_notion_environment_error(str(e))

    except Exception as e:
        print_notion_unexpected_error(type(e).__name__, str(e))

    # 非対話モード（ダッシュボードなど）では入力待ちせず続行する
    if os.environ.get("HOKUSAI_NONINTERACTIVE_CONTINUE", "0") == "1":
        print_notion_continue_yes()
        return False, True

    # 続行するかユーザーに確認
    print_notion_continue_prompt()

    try:
        response = input("   続行する場合は y を入力 [y/N]: ").strip().lower()
        if response == "y":
            print_notion_continue_yes()
            return False, True
        else:
            print_notion_continue_no()
            return False, False
    except (KeyboardInterrupt, EOFError):
        print_notion_continue_no()
        return False, False
