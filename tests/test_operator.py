"""Operator 名解決ロジックのテスト（Issue #21 / v0.4.8）

`resolve_operator_name()` が以下の順序で値を解決することを保証する:

1. `HOKUSAI_OPERATOR_NAME` 環境変数（空白以外）
2. `whoami` コマンドの出力
3. fallback として `"(unknown)"`
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from hokusai.integrations.notion_dashboard.operator import (
    UNKNOWN_OPERATOR,
    resolve_operator_name,
)

# ---------------------------------------------------------------------------
# env が最優先
# ---------------------------------------------------------------------------


def test_resolves_from_env_when_set(monkeypatch):
    monkeypatch.setenv("HOKUSAI_OPERATOR_NAME", "alice")
    assert resolve_operator_name() == "alice"


def test_strips_env_value(monkeypatch):
    """env 値の前後空白はトリムされる。"""
    monkeypatch.setenv("HOKUSAI_OPERATOR_NAME", "  bob  ")
    assert resolve_operator_name() == "bob"


def test_env_blank_falls_through_to_whoami(monkeypatch):
    """env が空白のみの場合は採用せず whoami / fallback に進む。"""
    monkeypatch.setenv("HOKUSAI_OPERATOR_NAME", "   ")
    mock_result = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="charlie\n", stderr=""
    )
    with (
        patch(
            "hokusai.integrations.notion_dashboard.operator.shutil.which",
            return_value="/usr/bin/whoami",
        ),
        patch(
            "hokusai.integrations.notion_dashboard.operator.subprocess.run",
            return_value=mock_result,
        ),
    ):
        assert resolve_operator_name() == "charlie"


def test_env_empty_string_falls_through_to_whoami(monkeypatch):
    """env が空文字列の場合も whoami に進む。"""
    monkeypatch.setenv("HOKUSAI_OPERATOR_NAME", "")
    mock_result = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="dave\n", stderr=""
    )
    with (
        patch(
            "hokusai.integrations.notion_dashboard.operator.shutil.which",
            return_value="/usr/bin/whoami",
        ),
        patch(
            "hokusai.integrations.notion_dashboard.operator.subprocess.run",
            return_value=mock_result,
        ),
    ):
        assert resolve_operator_name() == "dave"


# ---------------------------------------------------------------------------
# whoami fallback
# ---------------------------------------------------------------------------


def test_whoami_success_returns_username(monkeypatch):
    monkeypatch.delenv("HOKUSAI_OPERATOR_NAME", raising=False)
    mock_result = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="eve\n", stderr=""
    )
    with (
        patch(
            "hokusai.integrations.notion_dashboard.operator.shutil.which",
            return_value="/usr/bin/whoami",
        ),
        patch(
            "hokusai.integrations.notion_dashboard.operator.subprocess.run",
            return_value=mock_result,
        ),
    ):
        assert resolve_operator_name() == "eve"


def test_whoami_not_in_path_returns_unknown(monkeypatch):
    """whoami が PATH に無い場合は fallback。"""
    monkeypatch.delenv("HOKUSAI_OPERATOR_NAME", raising=False)
    with patch(
        "hokusai.integrations.notion_dashboard.operator.shutil.which",
        return_value=None,
    ):
        assert resolve_operator_name() == UNKNOWN_OPERATOR


def test_whoami_timeout_returns_unknown(monkeypatch):
    """whoami がタイムアウトしたら fallback。"""
    monkeypatch.delenv("HOKUSAI_OPERATOR_NAME", raising=False)
    with (
        patch(
            "hokusai.integrations.notion_dashboard.operator.shutil.which",
            return_value="/usr/bin/whoami",
        ),
        patch(
            "hokusai.integrations.notion_dashboard.operator.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["whoami"], timeout=3.0),
        ),
    ):
        assert resolve_operator_name() == UNKNOWN_OPERATOR


def test_whoami_nonzero_exit_returns_unknown(monkeypatch):
    """whoami が非ゼロ exit code を返したら fallback。"""
    monkeypatch.delenv("HOKUSAI_OPERATOR_NAME", raising=False)
    mock_result = subprocess.CompletedProcess(
        args=["whoami"], returncode=1, stdout="", stderr="error"
    )
    with (
        patch(
            "hokusai.integrations.notion_dashboard.operator.shutil.which",
            return_value="/usr/bin/whoami",
        ),
        patch(
            "hokusai.integrations.notion_dashboard.operator.subprocess.run",
            return_value=mock_result,
        ),
    ):
        assert resolve_operator_name() == UNKNOWN_OPERATOR


def test_whoami_empty_stdout_returns_unknown(monkeypatch):
    """whoami が空文字列を返したら fallback。"""
    monkeypatch.delenv("HOKUSAI_OPERATOR_NAME", raising=False)
    mock_result = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="\n", stderr=""
    )
    with (
        patch(
            "hokusai.integrations.notion_dashboard.operator.shutil.which",
            return_value="/usr/bin/whoami",
        ),
        patch(
            "hokusai.integrations.notion_dashboard.operator.subprocess.run",
            return_value=mock_result,
        ),
    ):
        assert resolve_operator_name() == UNKNOWN_OPERATOR


def test_whoami_oserror_returns_unknown(monkeypatch):
    """whoami 実行で OSError が発生したら fallback。"""
    monkeypatch.delenv("HOKUSAI_OPERATOR_NAME", raising=False)
    with (
        patch(
            "hokusai.integrations.notion_dashboard.operator.shutil.which",
            return_value="/usr/bin/whoami",
        ),
        patch(
            "hokusai.integrations.notion_dashboard.operator.subprocess.run",
            side_effect=OSError("permission denied"),
        ),
    ):
        assert resolve_operator_name() == UNKNOWN_OPERATOR


# ---------------------------------------------------------------------------
# fallback 定数
# ---------------------------------------------------------------------------


def test_unknown_operator_constant():
    """UNKNOWN_OPERATOR 定数は Notion 表示で「未設定」を即座に識別できる値。"""
    assert UNKNOWN_OPERATOR == "(unknown)"
