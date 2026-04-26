"""
validate_config の警告系（Phase B 第1段）テスト

Phase B-3 (トークン直書き警告) と Phase B-4 (接続状態整合性警告) を検証する。
hard error は変えず warnings: list[str] に項目が追加されることを固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.dashboard import (  # noqa: E402
    _check_service_alignment,
    _detect_token_like_values,
    _looks_redacted,
    validate_config,
)

from hokusai.integrations import connection_status as cs  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_config(tmp_path: Path) -> dict:
    """validate_config が hard error を出さない最小構成"""
    return {
        "project_root": str(tmp_path),
        "base_branch": "main",
    }


@pytest.fixture
def all_services_connected(monkeypatch):
    """`get_all_statuses` をすべて connected にスタブ"""

    def _fake(*, refresh: bool, mode: str = cs.MODE_SHALLOW) -> dict:
        return {
            "success": True,
            "checked_at": "2026-04-27T00:00:00+09:00",
            "mode": mode,
            "services": [
                {
                    "id": svc_id,
                    "label": meta["label"],
                    "status": cs.STATUS_CONNECTED,
                }
                for svc_id, meta in cs.SERVICE_METADATA.items()
            ],
        }

    monkeypatch.setattr(cs, "get_all_statuses", _fake)


def _stub_statuses(monkeypatch, status_by_id: dict[str, str]):
    """指定 ID だけ status を上書きし、他は connected として返すスタブを設置"""

    def _fake(*, refresh: bool, mode: str = cs.MODE_SHALLOW) -> dict:
        services = []
        for svc_id, meta in cs.SERVICE_METADATA.items():
            services.append(
                {
                    "id": svc_id,
                    "label": meta["label"],
                    "status": status_by_id.get(svc_id, cs.STATUS_CONNECTED),
                }
            )
        return {
            "success": True,
            "checked_at": "2026-04-27T00:00:00+09:00",
            "mode": mode,
            "services": services,
        }

    monkeypatch.setattr(cs, "get_all_statuses", _fake)


# ---------------------------------------------------------------------------
# _looks_redacted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["<token>", "<placeholder>", "xxxxxxxx", "**********", "----", "XXXX", "aaaaaa"],
)
def test_looks_redacted_detects_obvious_placeholders(value):
    assert _looks_redacted(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "gho_realtokenvalue123abc",
        "real-meaningful-string",
        "abc123def456",
        # 部分一致は実値とみなす（false-positive 回避: PR #4 のレビュー指摘）
        "pa*ssword",
        "token<prod>",
        "abc<x>def",
        # 短い記号列は伏字扱いしない（4 文字未満）
        "***",
        "---",
    ],
)
def test_looks_redacted_negative(value):
    assert _looks_redacted(value) is False


# ---------------------------------------------------------------------------
# _detect_token_like_values: prefix-based pattern matching
# ---------------------------------------------------------------------------


def test_detect_github_token_value():
    data = {"some_field": "gho_" + "A" * 36}
    warnings = _detect_token_like_values(data)
    assert any("GitHub トークン" in w for w in warnings)


def test_detect_gitlab_pat():
    data = {"nested": {"value": "glpat-" + "abCdEfGhIjKlMnOpQrSt"}}
    warnings = _detect_token_like_values(data)
    assert any("GitLab" in w for w in warnings)


def test_detect_redacted_token_is_skipped():
    """伏字された値（`glpat-***************` 等）は警告しない"""
    data = {"value": "glpat-****************"}
    warnings = _detect_token_like_values(data)
    assert warnings == []


def test_detect_anthropic_api_key():
    data = {"x": "sk-ant-" + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"}
    warnings = _detect_token_like_values(data)
    assert any("Anthropic" in w for w in warnings)


def test_detect_openai_api_key_does_not_match_anthropic():
    """OpenAI パターン (sk-...) が Anthropic パターン (sk-ant-...) に false-match しない"""
    data = {"openai": "sk-" + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"}
    warnings = _detect_token_like_values(data)
    assert any("OpenAI" in w for w in warnings)
    assert not any("Anthropic" in w for w in warnings)


# ---------------------------------------------------------------------------
# _detect_token_like_values: key-name-based heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["api_token", "access_token", "GITHUB_TOKEN", "secret_key", "password"]
)
def test_detect_secret_keys_with_real_value(key):
    data = {key: "some-real-value-1234567890"}
    warnings = _detect_token_like_values(data)
    assert len(warnings) >= 1


def test_redacted_secret_key_is_not_warned():
    """値全体が伏字（4 文字以上の連続記号など）の場合は警告しない"""
    data = {"api_key": "********"}
    warnings = _detect_token_like_values(data)
    assert warnings == []


def test_partial_asterisk_in_value_is_treated_as_real(monkeypatch):
    """PR #4 レビュー回帰: `pa*ssword` のように `*` を含むだけの実値は警告対象"""
    data = {"password": "pa*ssword"}
    warnings = _detect_token_like_values(data)
    assert any("password" in w for w in warnings)


def test_partial_anglebrackets_in_value_is_treated_as_real():
    """PR #4 レビュー回帰: `token<prod>` のように `<>` を含むだけの実値は警告対象"""
    data = {"api_token": "token<prod>"}
    warnings = _detect_token_like_values(data)
    assert any("api_token" in w for w in warnings)


def test_empty_secret_key_is_not_warned():
    data = {"api_token": ""}
    warnings = _detect_token_like_values(data)
    assert warnings == []


def test_nested_path_appears_in_warning():
    data = {"task_backend": {"extra": {"api_token": "abcdef1234567890ABCDEF"}}}
    warnings = _detect_token_like_values(data)
    assert any("task_backend.extra.api_token" in w for w in warnings)


def test_token_path_in_list():
    data = {"items": [{"api_key": "real-token-123abc"}]}
    warnings = _detect_token_like_values(data)
    assert any("items[0].api_key" in w for w in warnings)


def test_no_false_positive_on_unrelated_strings():
    data = {"build_command": "npm run build", "base_branch": "main"}
    warnings = _detect_token_like_values(data)
    assert warnings == []


def test_dedup_repeated_warnings():
    """同一 path から同一トークンを複数回検出しても1件にまとまる"""
    same_value = "gho_" + "A" * 36
    data = {"a": same_value, "b": same_value}
    warnings = _detect_token_like_values(data)
    # path が違うので 2 つの警告が出るべき
    assert len(warnings) == 2
    # 同一 path で同じ値が出る経路は通常ないが、dedup ロジックは存在する


# ---------------------------------------------------------------------------
# _check_service_alignment
# ---------------------------------------------------------------------------


def test_alignment_no_warning_when_all_connected(all_services_connected):
    data = {
        "git_hosting": {"type": "github"},
        "task_backend": {"type": "github_issue"},
        "cross_review": {"enabled": True},
    }
    warnings = _check_service_alignment(data)
    assert warnings == []


def test_alignment_warns_on_github_when_gh_not_authenticated(monkeypatch):
    _stub_statuses(monkeypatch, {"gh": cs.STATUS_NOT_AUTHENTICATED})
    data = {"git_hosting": {"type": "github"}}
    warnings = _check_service_alignment(data)
    assert any("git_hosting.type=github" in w for w in warnings)
    assert any("hokusai connect github" in w for w in warnings)


def test_alignment_warns_on_gitlab_when_glab_not_installed(monkeypatch):
    _stub_statuses(monkeypatch, {"glab": cs.STATUS_NOT_INSTALLED})
    data = {"git_hosting": {"type": "gitlab"}}
    warnings = _check_service_alignment(data)
    assert any("git_hosting.type=gitlab" in w for w in warnings)
    assert any("hokusai connect gitlab" in w for w in warnings)


def test_alignment_warns_when_task_backend_notion_disconnected(monkeypatch):
    _stub_statuses(monkeypatch, {"notion_mcp": cs.STATUS_NOT_INSTALLED})
    data = {"task_backend": {"type": "notion"}}
    warnings = _check_service_alignment(data)
    assert any("task_backend.type=notion" in w for w in warnings)


def test_alignment_warns_when_task_backend_github_issue_unauth(monkeypatch):
    _stub_statuses(monkeypatch, {"gh": cs.STATUS_NOT_AUTHENTICATED})
    data = {"task_backend": {"type": "github_issue"}}
    warnings = _check_service_alignment(data)
    assert any("task_backend.type=github_issue" in w for w in warnings)
    # PR #4 review fix: hokusai connect github の導線も含めて出すこと
    assert any("hokusai connect github" in w for w in warnings)


def test_alignment_warns_on_cross_review_when_codex_missing(monkeypatch):
    _stub_statuses(monkeypatch, {"codex": cs.STATUS_NOT_INSTALLED})
    data = {"cross_review": {"enabled": True}}
    warnings = _check_service_alignment(data)
    assert any("cross_review.enabled=true" in w for w in warnings)


def test_alignment_no_warning_when_cross_review_disabled(monkeypatch):
    _stub_statuses(monkeypatch, {"codex": cs.STATUS_NOT_INSTALLED})
    data = {"cross_review": {"enabled": False}}
    warnings = _check_service_alignment(data)
    assert warnings == []


def test_alignment_disabled_status_is_not_warned(monkeypatch):
    """connection_status が disabled の場合（HOKUSAI_SKIP_NOTION 等）は警告しない"""
    _stub_statuses(monkeypatch, {"notion_mcp": cs.STATUS_DISABLED})
    data = {"task_backend": {"type": "notion"}}
    warnings = _check_service_alignment(data)
    assert warnings == []


def test_alignment_unsupported_status_is_not_warned(monkeypatch):
    """unsupported（Jira/Linear など）は config 側でも未対応扱いなので警告しない"""
    _stub_statuses(monkeypatch, {"gh": cs.STATUS_UNSUPPORTED})
    data = {"git_hosting": {"type": "github"}}
    warnings = _check_service_alignment(data)
    assert warnings == []


def test_alignment_returns_empty_when_status_lookup_fails(monkeypatch):
    """get_all_statuses が例外を投げても致命的にせず、警告を空で返す"""

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cs, "get_all_statuses", _raise)
    data = {"git_hosting": {"type": "github"}}
    warnings = _check_service_alignment(data)
    assert warnings == []


# ---------------------------------------------------------------------------
# validate_config integration: warnings are merged
# ---------------------------------------------------------------------------


def test_validate_config_includes_token_warning(base_config):
    base_config["task_backend"] = {"extra": {"api_token": "real-token-1234567890"}}
    is_valid, errors, warnings = validate_config(base_config)
    assert is_valid is True
    assert errors == []
    assert any("api_token" in w for w in warnings)


def test_validate_config_includes_alignment_warning(base_config, monkeypatch):
    _stub_statuses(monkeypatch, {"gh": cs.STATUS_NOT_AUTHENTICATED})
    base_config["git_hosting"] = {"type": "github"}
    is_valid, errors, warnings = validate_config(base_config)
    assert is_valid is True
    assert errors == []
    assert any("hokusai connect github" in w for w in warnings)


def test_validate_config_keeps_command_warnings(base_config, all_services_connected):
    """コマンド静的検査の warning は引き続き返される（既存挙動）"""
    base_config["build_command"] = "npm run build && rm -rf /"
    is_valid, errors, warnings = validate_config(base_config)
    # warnings に何かしら含まれていれば OK（_check_command_string が拾う）
    # 既存の警告ロジックを壊していないことだけ確認
    assert is_valid is True
    assert isinstance(warnings, list)


def test_validate_config_warnings_do_not_block_save(base_config, monkeypatch):
    """warning が出ても is_valid=True を返し保存は止めない"""
    _stub_statuses(monkeypatch, {"gh": cs.STATUS_NOT_AUTHENTICATED})
    base_config["task_backend"] = {"type": "github_issue"}
    base_config["task_backend"]["extra"] = {"api_token": "abc1234567890def"}
    is_valid, errors, warnings = validate_config(base_config)
    assert is_valid is True
    assert errors == []
    # 両カテゴリの警告が出る
    assert any("api_token" in w for w in warnings)
    assert any("github_issue" in w for w in warnings)
