"""
Environment Check Service

環境変数とシステム設定をチェックするサービス。
"""

import os


def check_environment() -> list[str]:
    """環境設定をチェックし、未設定の項目について警告を返す

    Returns:
        警告メッセージのリスト（問題がなければ空リスト）
    """
    warnings = []

    # HOKUSAI_SKIP_NOTION: 既に設定されている場合は警告
    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        warnings.append(
            "HOKUSAI_SKIP_NOTION=1: Notion接続をスキップモードで実行します"
        )

    # 将来の拡張: 他の環境変数チェックをここに追加
    # - GITHUB_TOKEN: PR作成に必要（gh CLIが使用）
    # - ANTHROPIC_API_KEY: Claude Code実行に必要

    return warnings
