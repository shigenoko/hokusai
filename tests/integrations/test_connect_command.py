"""
hokusai/cli/commands/connect.py のテスト

`connect_service` の各分岐（未対応サービス / 未インストール / 認証済み /
未認証 + TTY 自動実行 / 未認証 + 非 TTY 表示のみ / ユーザーキャンセル / 強制再認証 /
タイムアウト）と、`show_status` の出力フォーマットを固定する。

subprocess / TTY / input は monkeypatch でスタブ化する。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from hokusai.cli.commands import connect as connect_mod
from hokusai.integrations import connection_status as cs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    cs.clear_cache()
    yield
    cs.clear_cache()


def _set_which(monkeypatch, mapping: dict[str, str | None]):
    monkeypatch.setattr(
        connect_mod.shutil, "which", lambda cmd: mapping.get(cmd)
    )


def _stub_run(returncode: int, stdout: str = "", stderr: str = ""):
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _run


# ---------------------------------------------------------------------------
# connect_service
# ---------------------------------------------------------------------------


def test_unknown_service_returns_exit_2(capsys):
    rc = connect_mod.connect_service("bogus")
    captured = capsys.readouterr()
    assert rc == 2
    assert "bogus" in captured.err


def test_github_not_installed_returns_exit_1(monkeypatch, capsys):
    _set_which(monkeypatch, {})

    rc = connect_mod.connect_service("github")

    captured = capsys.readouterr()
    assert rc == 1
    assert "PATH に見つかりません" in captured.out
    assert "https://cli.github.com/" in captured.out


def test_github_already_authenticated_returns_zero(monkeypatch, capsys):
    _set_which(monkeypatch, {"gh": "/opt/homebrew/bin/gh"})
    monkeypatch.setattr(
        connect_mod.subprocess,
        "run",
        _stub_run(0, stderr="github.com\n  ✓ Logged in"),
    )

    rc = connect_mod.connect_service("github")

    captured = capsys.readouterr()
    assert rc == 0
    assert "既に認証済みです" in captured.out
    # status 出力が表示されること
    assert "Logged in" in captured.out


def test_github_not_authenticated_non_interactive_displays_command(
    monkeypatch, capsys
):
    """非対話環境では認証コマンドを実行せず表示のみ"""
    _set_which(monkeypatch, {"gh": "/opt/homebrew/bin/gh"})
    monkeypatch.setattr(
        connect_mod.subprocess,
        "run",
        _stub_run(1, stderr="not signed in"),
    )

    rc = connect_mod.connect_service("github", no_interactive=True)

    captured = capsys.readouterr()
    assert rc == 0
    assert "未認証" in captured.out
    assert "gh auth login" in captured.out
    assert "実行しますか" not in captured.out  # 確認プロンプトは出さない


def test_github_not_authenticated_tty_user_accepts_runs_auth(
    monkeypatch, capsys
):
    """TTY で y を入力すると gh auth login を実行する"""
    _set_which(monkeypatch, {"gh": "/opt/homebrew/bin/gh"})

    captured_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        captured_calls.append(list(cmd))
        # status_command (capture_output=True) と auth_command (no capture) を区別
        if kwargs.get("capture_output"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="not signed in"
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(connect_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(connect_mod, "is_interactive_session", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    rc = connect_mod.connect_service("github")

    assert rc == 0
    # status_command と auth_command の両方が呼ばれている
    assert ["gh", "auth", "status"] in captured_calls
    assert ["gh", "auth", "login"] in captured_calls


def test_github_not_authenticated_tty_user_declines(monkeypatch, capsys):
    _set_which(monkeypatch, {"gh": "/opt/homebrew/bin/gh"})

    captured_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        captured_calls.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="not signed in"
        )

    monkeypatch.setattr(connect_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(connect_mod, "is_interactive_session", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    rc = connect_mod.connect_service("github")

    captured = capsys.readouterr()
    assert rc == 0
    assert "キャンセル" in captured.out
    # auth_command は呼ばれていない（status_command のみ）
    assert ["gh", "auth", "login"] not in captured_calls


def test_github_eof_on_prompt_falls_back_to_displaying_command(
    monkeypatch, capsys
):
    """input() が EOFError を投げた場合（パイプ等）は表示のみにフォールバック"""
    _set_which(monkeypatch, {"gh": "/opt/homebrew/bin/gh"})

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="not signed in"
        )

    def _raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr(connect_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(connect_mod, "is_interactive_session", lambda: True)
    monkeypatch.setattr("builtins.input", _raise_eof)

    rc = connect_mod.connect_service("github")

    captured = capsys.readouterr()
    assert rc == 0
    assert "gh auth login" in captured.out


def test_github_force_runs_auth_even_when_authenticated(monkeypatch):
    _set_which(monkeypatch, {"gh": "/opt/homebrew/bin/gh"})

    captured_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        captured_calls.append(list(cmd))
        if kwargs.get("capture_output"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr="Logged in"
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(connect_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(connect_mod, "is_interactive_session", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    rc = connect_mod.connect_service("github", force=True)

    assert rc == 0
    assert ["gh", "auth", "login"] in captured_calls


def test_github_status_check_timeout(monkeypatch, capsys):
    _set_which(monkeypatch, {"gh": "/opt/homebrew/bin/gh"})

    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)

    monkeypatch.setattr(connect_mod.subprocess, "run", _raise_timeout)

    rc = connect_mod.connect_service("github", no_interactive=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "5 秒以内に応答" in captured.out


def test_gitlab_uses_glab_commands(monkeypatch, capsys):
    """gitlab 選択時は glab を呼ぶ"""
    _set_which(monkeypatch, {"glab": "/usr/local/bin/glab"})

    captured_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        captured_calls.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="Logged in to gitlab.com"
        )

    monkeypatch.setattr(connect_mod.subprocess, "run", _fake_run)

    rc = connect_mod.connect_service("gitlab")

    assert rc == 0
    assert ["glab", "auth", "status"] in captured_calls


def test_authenticated_after_run_clears_cache(monkeypatch):
    """認証コマンド実行前に connection_status のキャッシュがクリアされる"""
    _set_which(monkeypatch, {"gh": "/opt/homebrew/bin/gh"})

    # キャッシュに古い結果を仕込む
    cs._cache[("gh", cs.MODE_SHALLOW)] = (
        {"id": "gh", "status": cs.STATUS_NOT_AUTHENTICATED, "cache_ttl_seconds": 30},
        9999.0,
    )

    def _fake_run(cmd, **kwargs):
        if kwargs.get("capture_output"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="not signed in"
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(connect_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(connect_mod, "is_interactive_session", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    connect_mod.connect_service("github")

    # キャッシュがクリアされていること
    assert cs._cache == {}


# ---------------------------------------------------------------------------
# show_status
# ---------------------------------------------------------------------------


def test_show_status_lists_all_services(monkeypatch, capsys):
    """`hokusai connect --status` が全サービスを表示する"""
    monkeypatch.setattr(
        cs,
        "get_all_statuses",
        lambda *, refresh, mode=cs.MODE_SHALLOW: {
            "success": True,
            "checked_at": "2026-04-26T00:00:00+09:00",
            "mode": "shallow",
            "services": [
                cs._build_result(
                    service_id="claude",
                    label="Claude Code",
                    category=cs.CategoryLLMAgent,
                    status=cs.STATUS_CONNECTED,
                    summary="Claude Code が利用可能です",
                    detail=None,
                    required_for=[],
                    message_key="x",
                ),
                cs._build_result(
                    service_id="gh",
                    label="GitHub CLI",
                    category=cs.CategoryGitHosting,
                    status=cs.STATUS_NOT_AUTHENTICATED,
                    summary="GitHub CLI が未認証です",
                    detail=None,
                    required_for=[],
                    message_key="y",
                    next_action={
                        "type": "command",
                        "label": "GitHub に接続",
                        "command": "gh auth login",
                        "docs_url": None,
                    },
                ),
            ],
        },
    )

    rc = connect_mod.show_status()

    captured = capsys.readouterr()
    assert rc == 0
    assert "Claude Code" in captured.out
    assert "GitHub CLI" in captured.out
    assert "接続済み" in captured.out
    assert "未認証" in captured.out
    # next_action.command が表示される
    assert "gh auth login" in captured.out


def test_show_status_uses_refresh_by_default(monkeypatch):
    received: dict[str, Any] = {}

    def _fake_get_all(*, refresh: bool, mode: str = cs.MODE_SHALLOW) -> dict:
        received["refresh"] = refresh
        return {
            "success": True,
            "checked_at": "2026-04-26T00:00:00+09:00",
            "mode": mode,
            "services": [],
        }

    monkeypatch.setattr(cs, "get_all_statuses", _fake_get_all)

    connect_mod.show_status()

    assert received["refresh"] is True


def test_show_status_empty_registry(monkeypatch, capsys):
    monkeypatch.setattr(
        cs,
        "get_all_statuses",
        lambda *, refresh, mode=cs.MODE_SHALLOW: {
            "success": True,
            "checked_at": "2026-04-26T00:00:00+09:00",
            "mode": "shallow",
            "services": [],
        },
    )

    rc = connect_mod.show_status()

    captured = capsys.readouterr()
    assert rc == 0
    assert "登録されているサービスがありません" in captured.out


# ---------------------------------------------------------------------------
# is_interactive_session
# ---------------------------------------------------------------------------


def test_is_interactive_requires_both_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert connect_mod.is_interactive_session() is True

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert connect_mod.is_interactive_session() is False

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert connect_mod.is_interactive_session() is False
