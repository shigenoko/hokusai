"""Prompt テンプレートの読み込み・変数埋め込み・保存を行うローダー"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# prompts/ ディレクトリのルートパス（プロジェクトルート/prompts/）
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_REGISTRY_PATH = _PROMPTS_DIR / "registry.yaml"

# モジュールロード時にレジストリをキャッシュ
_registry: list[dict] | None = None


def _load_registry() -> list[dict]:
    """registry.yaml を読み込みキャッシュする"""
    global _registry
    if _registry is None:
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            _registry = yaml.safe_load(f) or []
    return _registry


def _find_entry(prompt_id: str) -> dict:
    """指定 ID のレジストリエントリを取得する"""
    for entry in _load_registry():
        if entry["id"] == prompt_id:
            return entry
    raise KeyError(f"Unknown prompt ID: {prompt_id}")


def _resolve_path(entry: dict) -> Path:
    """レジストリエントリからテンプレートファイルのパスを解決する"""
    return _PROMPTS_DIR / entry["file"]


def get_prompt(prompt_id: str, **kwargs: object) -> str:
    """テンプレートを読み込み、変数を埋め込んで返す

    Args:
        prompt_id: registry.yaml に定義された ID
        **kwargs: テンプレート内の {variable} に埋め込む値

    Returns:
        変数埋め込み済みのプロンプト文字列
    """
    entry = _find_entry(prompt_id)
    template_path = _resolve_path(entry)
    template = template_path.read_text(encoding="utf-8")
    try:
        return template.format(**kwargs)
    except KeyError as e:
        raise KeyError(
            f"Prompt '{prompt_id}' requires variable {e} but it was not provided"
        ) from e


def list_prompts() -> list[dict]:
    """レジストリの全エントリを返す（ダッシュボード表示用）

    各エントリに mtime（ファイル最終更新時刻）を付加する。
    ダッシュボードから常に最新の情報を返すため、毎回ファイルから読み直す。
    """
    global _registry
    _registry = None  # キャッシュを破棄して再読込
    results = []
    for entry in _load_registry():
        item = dict(entry)
        path = _resolve_path(entry)
        if path.exists():
            item["mtime"] = path.stat().st_mtime
        results.append(item)
    return results


def read_prompt_file(prompt_id: str) -> str:
    """テンプレートの生テキスト（変数未埋め込み）を返す"""
    entry = _find_entry(prompt_id)
    template_path = _resolve_path(entry)
    return template_path.read_text(encoding="utf-8")


def _validate_template_syntax(content: str, required_vars: list[str]) -> list[str]:
    """テンプレートの構文を検証する。

    - 必須変数の存在チェック
    - str.format() 構文の妥当性チェック（不整合な波括弧、未知のプレースホルダ）

    Returns:
        エラーメッセージのリスト（空なら問題なし）
    """
    errors: list[str] = []

    # 1. 必須変数の存在チェック
    missing = []
    for var in required_vars:
        pattern = r"(?<!\{)\{" + re.escape(var) + r"\}(?!\})"
        if not re.search(pattern, content):
            missing.append(var)
    if missing:
        errors.append(f"必須変数が見つかりません: {', '.join(missing)}")

    # 2. str.format() 構文チェック: ダミー値で試行して構文エラーを検出
    import string
    try:
        # テンプレート内の全フィールド名を抽出
        formatter = string.Formatter()
        field_names = set()
        for _, field_name, _, _ in formatter.parse(content):
            if field_name is not None:
                # "foo.bar" や "foo[0]" の場合、ルート名だけ取る
                root = field_name.split(".")[0].split("[")[0]
                if root:
                    field_names.add(root)
    except (ValueError, KeyError) as e:
        errors.append(f"テンプレート構文エラー: {e}")
        return errors  # 構文が壊れている場合、これ以上のチェックは不要

    # 3. 未知のプレースホルダをチェック
    allowed = set(required_vars)
    unknown = field_names - allowed
    if unknown:
        errors.append(
            f"未定義の変数が含まれています: {', '.join(sorted(unknown))}。"
            f"許可された変数: {', '.join(required_vars) if required_vars else 'なし'}"
        )

    return errors


def write_prompt_file(prompt_id: str, content: str) -> None:
    """テンプレートファイルを上書き保存する

    保存前にテンプレート構文のバリデーションを行う。

    Args:
        prompt_id: registry.yaml に定義された ID
        content: 保存するテンプレート内容

    Raises:
        ValueError: テンプレート構文に問題がある場合
        ValueError: 内容が空の場合
    """
    if not content or not content.strip():
        raise ValueError("テンプレートの内容が空です")

    entry = _find_entry(prompt_id)
    required_vars = entry.get("variables", [])

    validation_errors = _validate_template_syntax(content, required_vars)
    if validation_errors:
        raise ValueError("; ".join(validation_errors))

    template_path = _resolve_path(entry)
    template_path.write_text(content, encoding="utf-8")
