"""
Environment Check Service

環境変数とシステム設定をチェックするサービス。
"""

from ...utils.skip_notion import is_skip_notion


def check_environment() -> list[str]:
    """環境設定をチェックし、未設定の項目について警告を返す

    Returns:
        警告メッセージのリスト（問題がなければ空リスト）
    """
    warnings = []

    # Issue #111: profile-aware な is_skip_notion() で判定（HOKUSAI_ACTIVE_PROFILE
    # 経由の profile suffix env と legacy global の両方を統合評価）。
    if is_skip_notion():
        warnings.append(
            "HOKUSAI_SKIP_NOTION=1: Notion接続をスキップモードで実行します"
        )

    # 将来の拡張: 他の環境変数チェックをここに追加
    # - GITHUB_TOKEN: PR作成に必要（gh CLIが使用）
    # - ANTHROPIC_API_KEY: Claude Code実行に必要

    return warnings
