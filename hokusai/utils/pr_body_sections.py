"""
PR 本文のセクション操作ユーティリティ

Markdown の ## 見出しを境界としてセクションを差し替え・追加する。
"""

import re


def upsert_section(body: str, section_title: str, section_content: str) -> str:
    """PR 本文内の指定セクションを差し替え、なければ末尾に追加する。

    Args:
        body: 現在の PR 本文（Markdown）
        section_title: セクションの見出し（例: "変更サマリー"）
        section_content: セクション本文（見出し行を含まない）

    Returns:
        更新後の PR 本文
    """
    heading = f"## {section_title}"
    new_section = f"{heading}\n\n{section_content}"

    # 既存セクションを探す: ## <title> から次の ## まで
    pattern = re.compile(
        rf"^## {re.escape(section_title)}\s*\n.*?(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )

    if pattern.search(body):
        # 既存セクションを置換
        return pattern.sub(new_section.rstrip() + "\n", body).rstrip() + "\n"

    # セクションが存在しない場合は末尾に追加
    if body and not body.endswith("\n"):
        body += "\n"
    return f"{body}\n{new_section}\n"
