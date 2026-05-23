"""Notion rich_text / title array 共通 helper（Issue #54 / Workgraph 完成）

`_join_rich_text_items`（prime_renderer）と `_join_rich_text_text`
（project_memory_db）に同形の連結ロジックを別 module に持っていたところ、
SonarCloud duplication で検出されたため共通 helper に集約する。

Notion API は装飾 / mention / equation 等で rich_text を複数 element に分割
するため、先頭要素しか読まないとテキスト欠落が発生する。本 helper は全
element の plain_text → text.content の優先順で連結し、mention 等で text
キーが無い要素は skip する。
"""

from __future__ import annotations


def join_rich_text_items(items: list[dict]) -> str:
    """rich_text / title array の全要素から plain_text / text.content を連結する。

    各 element は以下の優先順で文字列を取り出す:
    1. `plain_text`（Notion API がレンダリング済みテキストを入れる）
    2. `text.content`（plain_text が無い場合のフォールバック）

    `mention` / `equation` 等で `text` キーが無い要素は skip（空文字を返さず
    完全に除外）。dict 以外の要素も skip して防御的に動作する。
    """
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        plain = item.get("plain_text")
        if isinstance(plain, str) and plain:
            parts.append(plain)
            continue
        text = item.get("text")
        if isinstance(text, dict):
            content = text.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    return "".join(parts)
