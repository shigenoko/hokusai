"""profile 共有テンプレートの妥当性テスト（Issue #22 / v0.4.7）

検証内容:
- `configs/profile-template.yaml` と `configs/profile-config-template.yaml` が
  valid YAML であること
- プレースホルダを実値に置換した後、**hokusai の本物の profile registry loader
  および各種 config parser** でロードできること（profile 名 regex、port 範囲、
  default_profile キーの一致、notion_dashboard / cross_review のバリデーションを
  実際に通過することを保証）
- テンプレート内にシークレット実値（"sk-..." / "secret_..." 形式等）が含まれて
  いないこと（人為的な commit 漏れを防ぐ）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_REGISTRY_TEMPLATE = REPO_ROOT / "configs" / "profile-template.yaml"
PROFILE_CONFIG_TEMPLATE = REPO_ROOT / "configs" / "profile-config-template.yaml"

# シークレット実値らしき文字列の正規表現。env 変数名や placeholder は除外する。
# 検出対象例: "sk-abc123..." / "secret_..." / Slack webhook URL / "ghp_..."
_SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),                  # OpenAI 系
    re.compile(r"\bsecret_[A-Za-z0-9]{20,}"),                # Notion integration token
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),                   # GitHub PAT
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),               # GitLab PAT
    re.compile(r"\bxox[bopas]-[A-Za-z0-9-]{20,}"),           # Slack token
    re.compile(r"https://hooks\.slack\.com/services/[A-Z0-9/]{20,}"),  # Slack webhook
]


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# 存在 / YAML 妥当性
# ---------------------------------------------------------------------------


def test_profile_registry_template_exists():
    assert PROFILE_REGISTRY_TEMPLATE.is_file(), (
        f"{PROFILE_REGISTRY_TEMPLATE} が存在しません"
    )


def test_profile_config_template_exists():
    assert PROFILE_CONFIG_TEMPLATE.is_file(), (
        f"{PROFILE_CONFIG_TEMPLATE} が存在しません"
    )


def test_profile_registry_template_is_valid_yaml():
    data = _load_yaml(PROFILE_REGISTRY_TEMPLATE)
    assert isinstance(data, dict)
    assert "profiles" in data, "profile registry は profiles キーを持つべき"


def test_profile_config_template_is_valid_yaml():
    data = _load_yaml(PROFILE_CONFIG_TEMPLATE)
    assert isinstance(data, dict)
    # 主要セクションが存在することを確認
    assert "project_root" in data
    assert "task_backend" in data
    assert "git_hosting" in data


# ---------------------------------------------------------------------------
# シークレット混入の検出
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template_path",
    [PROFILE_REGISTRY_TEMPLATE, PROFILE_CONFIG_TEMPLATE],
    ids=["registry", "config"],
)
def test_template_contains_no_secret_values(template_path: Path):
    """テンプレートにシークレット実値が含まれていないこと（commit 漏れ防止）。"""
    content = template_path.read_text(encoding="utf-8")
    for pattern in _SECRET_VALUE_PATTERNS:
        match = pattern.search(content)
        assert match is None, (
            f"{template_path.name} にシークレット実値らしき文字列が含まれています: "
            f"{match.group(0)!r}（pattern={pattern.pattern!r}）"
        )


# ---------------------------------------------------------------------------
# プレースホルダ置換後のロード検証
# ---------------------------------------------------------------------------


def _substitute_placeholders(content: str, replacements: dict[str, str]) -> str:
    """`<TODO:...>` プレースホルダを実値に置換する。

    替えたい placeholder のキーは TODO 後ろの内容で先頭一致判定する。
    """
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        for key, value in replacements.items():
            if inner.startswith(key):
                return value
        return match.group(0)

    return re.sub(r"<TODO:([^>]+)>", repl, content)


def _walk_values(node: object):
    """ネストした dict / list を辿って全リーフ値を yield する。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_values(k)
            yield from _walk_values(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_values(item)
    else:
        yield node


def _has_unresolved_todo_in_data(data: object) -> bool:
    """YAML パース後のデータ構造に `<TODO:` 文字列が残っていないか検査する。

    コメント中の説明文（残置検出ガイド等）は対象外とする。
    """
    for value in _walk_values(data):
        if isinstance(value, str) and "<TODO:" in value:
            return True
    return False


def test_profile_registry_template_loads_via_real_loader(tmp_path):
    """registry template のプレースホルダ置換後、本物の `load_profile_registry`
    でロードできることを保証する。

    本物の loader は profile 名 regex（`^[a-z][a-z0-9_-]*$`）、default_profile が
    profiles のキーに一致するか、port range 等を検証する。
    Copilot レビュー 1 回目 #1 対応。
    """
    from hokusai.config.profiles import load_profile_registry

    cfg_path = tmp_path / "demo-config.yaml"
    data_dir = tmp_path / "data"
    content = PROFILE_REGISTRY_TEMPLATE.read_text(encoding="utf-8")
    substituted = _substitute_placeholders(
        content,
        {
            "既定 profile 名": "demo",
            "profile_name": "demo",
            "profile config の絶対パス": str(cfg_path),
            "profile 用 data_dir": str(data_dir),
            "案件ラベル": "Demo Project",
            "案件の短い説明": "demo profile for tests",
        },
    )
    registry_path = tmp_path / "profiles.yaml"
    registry_path.write_text(substituted, encoding="utf-8")

    # 本物の loader でパース（profile 名 regex / default_profile キー一致 等を検証）
    registry = load_profile_registry(registry_path)
    assert registry.default_profile == "demo"
    assert "demo" in registry.profiles
    profile = registry.profiles["demo"]
    assert profile.dashboard_port == 8765
    assert str(profile.config_path) == str(cfg_path)

    # データ構造に <TODO:...> 残置がないこと
    data = yaml.safe_load(substituted)
    assert not _has_unresolved_todo_in_data(data), (
        f"パース後のデータに <TODO: が残っています:\n{data}"
    )


def test_profile_config_template_parses_via_hokusai_loaders():
    """config template のプレースホルダ置換後、hokusai の各種 config parser で
    各セクションが正しく解釈されることを保証する。

    `_parse_notion_dashboard_config` / `_parse_cross_review_config` 等の実装に
    通すことで、enum 値（cross_review.provider）や数値範囲、env 変数名の妥当性も
    実環境と同じルールで検証される。
    Copilot レビュー 1 回目 #1 対応。
    """
    from hokusai.config.loaders import (
        _parse_cross_review_config,
        _parse_notion_dashboard_config,
    )

    content = PROFILE_CONFIG_TEMPLATE.read_text(encoding="utf-8")
    substituted = _substitute_placeholders(
        content,
        {
            "project_root の絶対パス": "/repo/demo",
            "base_branch": "main",
            "build コマンド": "echo build",
            "test コマンド": "echo test",
            "lint コマンド": "echo lint",
            "notion または github_issue": "notion",
            "github または gitlab": "github",
            "owner/repo": "demo/demo",
            "Notion API token env 変数名": "DEMO_NOTION_API_TOKEN",
            "Workflows DB id env 変数名": "DEMO_NOTION_WORKFLOWS_DB_ID",
            "PR DB id env 変数名": "DEMO_NOTION_PR_DB_ID",
            "Review Issues DB id env 変数名": "DEMO_NOTION_REVIEW_ISSUES_DB_ID",
            "Figma API token env 変数名": "DEMO_FIGMA_API_TOKEN",
            "Miro API token env 変数名": "DEMO_MIRO_API_TOKEN",
            "Miro team id env 変数名": "DEMO_MIRO_TEAM_ID",
            "Slack webhook URL env 変数名": "DEMO_SLACK_WEBHOOK_URL",
            "Console BASIC 認証ユーザ env 変数名": "DEMO_HOKUSAI_OPS_USERNAME",
            "Console BASIC 認証パスワード env 変数名": "DEMO_HOKUSAI_OPS_PASSWORD",
            "cross-review モデル": "codex-mini-latest",
        },
    )
    data = yaml.safe_load(substituted)
    assert not _has_unresolved_todo_in_data(data), (
        f"パース後のデータに <TODO: が残っています:\n{data}"
    )

    # 各セクションを実 parser に通す（v0.4.6 で導入された provider 検証も含む）
    nd = _parse_notion_dashboard_config(data)
    assert nd.enabled is True
    assert nd.api_token_env == "DEMO_NOTION_API_TOKEN"
    # PR #37 Copilot 4 回目指摘: template の placeholder key にタイポがあると
    # 既定値（HOKUSAI_NOTION_REVIEW_ISSUES_DB_ID）にサイレントにフォール
    # バックしてもこのテストが pass してしまう。実 parser が期待値で
    # consume したことを明示的に検証する。
    assert nd.workflows_db_id_env == "DEMO_NOTION_WORKFLOWS_DB_ID"
    assert nd.pull_requests_db_id_env == "DEMO_NOTION_PR_DB_ID"
    assert nd.review_issues_db_id_env == "DEMO_NOTION_REVIEW_ISSUES_DB_ID"

    cr = _parse_cross_review_config(data)
    assert cr.provider == "codex"  # template の default
    assert cr.model == "codex-mini-latest"
    assert cr.enabled is False
    assert cr.on_failure == "warn"

    # トップレベル値の確認
    assert data["project_root"] == "/repo/demo"
    assert data["task_backend"]["type"] == "notion"
    assert data["git_hosting"]["type"] == "github"
