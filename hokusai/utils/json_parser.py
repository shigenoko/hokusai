"""
JSON parsing utilities for extracting structured data from LLM output.

Claude Code やその他の LLM の出力文字列から JSON オブジェクトや配列を
安全に抽出するためのユーティリティ。
"""

import json
import re
from typing import Any


def extract_json_from_output(
    output: str,
    expected_type: type = dict,
    default: Any = None,
) -> dict | list:
    """LLM の出力文字列から JSON を抽出してパースする。

    以下の順序で JSON の抽出を試みる:
      1. ```json ... ``` コードブロック内の JSON をパース
      2. 出力全体から JSON デリミタ（{...} または [...]）を検索してパース
      3. いずれも失敗した場合はデフォルト値を返す

    Args:
        output: LLM（Claude Code 等）の出力文字列。
        expected_type: 期待する JSON のトップレベル型。
            ``dict`` の場合は ``{...}`` を、``list`` の場合は ``[...]`` を
            フォールバック検索で探す。デフォルトは ``dict``。
        default: パースに失敗した場合に返すデフォルト値。

    Returns:
        パースされた辞書またはリスト。パース失敗時は *default* を返す。
    """
    # 1. ```json ... ``` ブロックを検索
    json_match = re.search(r"```json\s*([\s\S]*?)\s*```", output)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # 2. 直接 JSON デリミタを検索（expected_type に応じてパターンを切り替え）
    if expected_type is list:
        pattern = r"\[[\s\S]*\]"
    else:
        pattern = r"\{[\s\S]*\}"

    json_match = re.search(pattern, output)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    # 3. フォールバック
    return default
