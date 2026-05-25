"""
Environment Check Service

環境変数とシステム設定をチェックするサービス。
"""

from ...utils.skip_notion import active_skip_env_name


def check_environment() -> list[str]:
    """環境設定をチェックし、未設定の項目について警告を返す

    Returns:
        警告メッセージのリスト（問題がなければ空リスト）
    """
    warnings = []

    # Issue #111: profile-aware な lookup（HOKUSAI_ACTIVE_PROFILE 経由の profile
    # suffix env と legacy global の両方を統合評価）。
    # warning 文言には実際に効いている env 名を出すことで、ユーザがどちらを
    # unset すれば skip 解除できるか分かるようにする（Copilot Round 1 指摘）。
    skip_env = active_skip_env_name()
    if skip_env is not None:
        warnings.append(
            f"{skip_env}=1: Notion接続をスキップモードで実行します"
        )

    # 将来の拡張: 他の環境変数チェックをここに追加
    # - GITHUB_TOKEN: PR作成に必要（gh CLIが使用）
    # - ANTHROPIC_API_KEY: Claude Code実行に必要

    return warnings
