"""
hokusai.integrations.connection_status のテスト

各サービスのチェック関数 (`_check_*`)、レジストリ経由の取得 (`get_service_status` /
`get_all_statuses`)、TTL キャッシュ、想定外エラーのフォールバックを検証する。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from hokusai.integrations import connection_status as cs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """テスト間でキャッシュ汚染が起きないようにする"""
    cs.clear_cache()
    yield
    cs.clear_cache()


@pytest.fixture(autouse=True)
def _isolate_notion_env(monkeypatch):
    """テスト中は実環境の HOKUSAI_SKIP_NOTION を無視する"""
    monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)


def _stub_run(returncode: int, stdout: str = "", stderr: str = ""):
    """subprocess.run の戻り値を作る簡易ファクトリ"""

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=returncode, stdout=stdout, stderr=stderr)

    return _run


def _make_which(mapping: dict[str, str | None]):
    """shutil.which の差し替え。未指定のコマンドは None を返す。"""

    def _which(cmd: str) -> str | None:
        return mapping.get(cmd)

    return _which


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------


def test_claude_connected(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({"claude": "/usr/local/bin/claude"}))
    monkeypatch.setattr(cs.subprocess, "run", _stub_run(0, stdout="2.1.112 (Claude Code)\n"))

    result = cs.get_service_status("claude")

    assert result["status"] == cs.STATUS_CONNECTED
    assert result["severity"] == "ok"
    assert result["category"] == cs.CategoryLLMAgent
    assert result["required_for"] == ["implementation"]
    assert result["message_key"] == "connection.claude.connected"
    assert "2.1.112" in result["detail"]
    assert result["next_action"] is None


def test_claude_not_installed(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({}))

    result = cs.get_service_status("claude")

    assert result["status"] == cs.STATUS_NOT_INSTALLED
    assert result["severity"] == "error"
    assert result["next_action"]["type"] == "docs"
    assert result["next_action"]["docs_url"] == "https://claude.com/claude-code"


def test_claude_timeout(monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=3.0)

    monkeypatch.setattr(cs.shutil, "which", _make_which({"claude": "/usr/local/bin/claude"}))
    monkeypatch.setattr(cs.subprocess, "run", _raise_timeout)

    result = cs.get_service_status("claude")

    assert result["status"] == cs.STATUS_TIMEOUT
    assert result["severity"] == "warn"
    # timeout は短い TTL を使う
    assert result["cache_ttl_seconds"] == int(cs.TIMEOUT_TTL_SECONDS)


def test_claude_unknown_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({"claude": "/usr/local/bin/claude"}))
    monkeypatch.setattr(cs.subprocess, "run", _stub_run(2, stderr="something broke"))

    result = cs.get_service_status("claude")

    assert result["status"] == cs.STATUS_UNKNOWN
    assert result["severity"] == "error"
    assert "something broke" in result["detail"]


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------


def test_codex_connected(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({"codex": "/usr/local/bin/codex"}))
    monkeypatch.setattr(cs.subprocess, "run", _stub_run(0, stdout="codex-cli 0.125.0\n"))

    result = cs.get_service_status("codex")

    assert result["status"] == cs.STATUS_CONNECTED
    assert result["required_for"] == ["cross_review"]
    assert "codex-cli 0.125.0" in result["detail"]


def test_codex_not_installed(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({}))

    result = cs.get_service_status("codex")

    assert result["status"] == cs.STATUS_NOT_INSTALLED
    assert result["next_action"]["type"] == "docs"
    # type=docs で docs_url が None だと UI で導線が切れるため、有効な URL を持つこと
    assert result["next_action"]["docs_url"]
    assert result["docs_url"]


# ---------------------------------------------------------------------------
# gh
# ---------------------------------------------------------------------------


def test_gh_connected(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({"gh": "/opt/homebrew/bin/gh"}))
    monkeypatch.setattr(
        cs.subprocess,
        "run",
        _stub_run(0, stderr="github.com\n  ✓ Logged in to github.com account user"),
    )

    result = cs.get_service_status("gh")

    assert result["status"] == cs.STATUS_CONNECTED
    assert result["required_for"] == ["git_hosting", "pr_creation", "review_comment_reply"]
    assert "Logged in" in result["detail"]


def test_gh_not_authenticated(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({"gh": "/opt/homebrew/bin/gh"}))
    monkeypatch.setattr(
        cs.subprocess,
        "run",
        _stub_run(1, stderr="You are not logged into any GitHub hosts."),
    )

    result = cs.get_service_status("gh")

    assert result["status"] == cs.STATUS_NOT_AUTHENTICATED
    assert result["severity"] == "warn"
    assert result["next_action"] == {
        "type": "command",
        "label": "GitHub に接続",
        "command": "gh auth login",
        "docs_url": None,
    }


def test_gh_not_installed(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({}))

    result = cs.get_service_status("gh")

    assert result["status"] == cs.STATUS_NOT_INSTALLED
    assert result["next_action"]["docs_url"] == "https://cli.github.com/"


# ---------------------------------------------------------------------------
# glab
# ---------------------------------------------------------------------------


def test_glab_not_installed(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({}))

    result = cs.get_service_status("glab")

    assert result["status"] == cs.STATUS_NOT_INSTALLED
    assert result["category"] == cs.CategoryGitHosting
    assert result["next_action"]["docs_url"] == "https://gitlab.com/gitlab-org/cli"


def test_glab_not_authenticated(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({"glab": "/usr/local/bin/glab"}))
    monkeypatch.setattr(cs.subprocess, "run", _stub_run(1, stderr="not signed in"))

    result = cs.get_service_status("glab")

    assert result["status"] == cs.STATUS_NOT_AUTHENTICATED
    assert result["next_action"]["command"] == "glab auth login"


def test_glab_connected(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({"glab": "/usr/local/bin/glab"}))
    monkeypatch.setattr(cs.subprocess, "run", _stub_run(0, stderr="Logged in to gitlab.com as user"))

    result = cs.get_service_status("glab")

    assert result["status"] == cs.STATUS_CONNECTED


# ---------------------------------------------------------------------------
# notion_mcp
# ---------------------------------------------------------------------------


def test_notion_mcp_disabled_by_env(monkeypatch):
    monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

    result = cs.get_service_status("notion_mcp")

    assert result["status"] == cs.STATUS_DISABLED
    assert result["severity"] == "info"


def test_notion_mcp_connected_via_config(monkeypatch):
    monkeypatch.setattr(
        cs,
        "_notion_mcp_configured",
        lambda: (True, "/Users/test/.claude.json"),
    )

    result = cs.get_service_status("notion_mcp")

    assert result["status"] == cs.STATUS_CONNECTED
    assert result["required_for"] == ["notion_sync", "task_backend"]
    assert "/Users/test/.claude.json" in result["detail"]


def test_notion_mcp_not_installed(monkeypatch):
    monkeypatch.setattr(cs, "_notion_mcp_configured", lambda: (False, None))

    result = cs.get_service_status("notion_mcp")

    assert result["status"] == cs.STATUS_NOT_INSTALLED
    assert result["next_action"]["type"] == "command"
    assert "claude mcp add" in result["next_action"]["command"]


def test_notion_mcp_deep_mode_does_not_invoke_claude(monkeypatch):
    """deep モードでも Claude を介した重い実行はしない（拡張ポイントだが現状は設定確認のみ）"""
    monkeypatch.setattr(
        cs,
        "_notion_mcp_configured",
        lambda: (True, "/tmp/mcp.json"),
    )
    # subprocess.run が呼ばれたら失敗させる
    def _fail(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called for notion_mcp deep check")

    monkeypatch.setattr(cs.subprocess, "run", _fail)

    result = cs.get_service_status("notion_mcp", mode="deep")

    assert result["status"] == cs.STATUS_CONNECTED
    assert result["mode"] == "deep"


def test_extract_mcp_servers_walks_nested_structure():
    """`mcpServers` が複数階層に存在する Claude Code の `~/.claude.json` 形式に対応する"""
    data = {
        "projects": {
            "/repo/a": {"mcpServers": {"notion": {}, "github": {}}},
            "/repo/b": {"mcpServers": {"slack": {}}},
        },
        "mcpServers": {"global-server": {}},
    }
    servers = cs._extract_mcp_servers(data)
    assert set(servers) == {"notion", "github", "slack", "global-server"}


# ---------------------------------------------------------------------------
# jira / linear
# ---------------------------------------------------------------------------


def test_jira_unsupported():
    result = cs.get_service_status("jira")

    assert result["status"] == cs.STATUS_UNSUPPORTED
    assert result["severity"] == "info"
    assert result["category"] == cs.CategoryTaskBackend


def test_linear_unsupported():
    result = cs.get_service_status("linear")

    assert result["status"] == cs.STATUS_UNSUPPORTED
    assert result["category"] == cs.CategoryTaskBackend


# ---------------------------------------------------------------------------
# Registry behaviour
# ---------------------------------------------------------------------------


def test_unknown_service_returns_none():
    assert cs.get_service_status("does_not_exist") is None


def test_get_all_statuses_returns_full_registry(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({}))
    monkeypatch.setattr(cs, "_notion_mcp_configured", lambda: (False, None))

    bundle = cs.get_all_statuses()

    assert bundle["success"] is True
    assert bundle["mode"] == "shallow"
    ids = [s["id"] for s in bundle["services"]]
    assert ids == cs.SERVICE_ORDER


def test_check_exception_falls_back_to_unknown(monkeypatch):
    """チェック関数が想定外の例外を出した場合、unknown ステータスにフォールバックする"""

    def _boom(mode):
        raise RuntimeError("boom")

    monkeypatch.setitem(cs.SERVICE_REGISTRY, "claude", _boom)

    result = cs.get_service_status("claude")

    assert result["status"] == cs.STATUS_UNKNOWN
    assert result["severity"] == "error"
    assert "boom" in result["detail"]


@pytest.mark.parametrize(
    "service_id,expected_category,expected_label",
    [
        ("gh", cs.CategoryGitHosting, "GitHub CLI"),
        ("glab", cs.CategoryGitHosting, "GitLab CLI"),
        ("notion_mcp", cs.CategoryMCP, "Notion MCP"),
        ("jira", cs.CategoryTaskBackend, "Jira"),
        ("linear", cs.CategoryTaskBackend, "Linear"),
        ("codex", cs.CategoryLLMAgent, "OpenAI Codex"),
    ],
)
def test_exception_fallback_uses_correct_metadata(
    monkeypatch, service_id, expected_category, expected_label
):
    """例外フォールバック時、サービスの category / label / required_for が正しい値になる"""

    def _boom(mode):
        raise RuntimeError("boom")

    monkeypatch.setitem(cs.SERVICE_REGISTRY, service_id, _boom)

    result = cs.get_service_status(service_id)

    assert result["status"] == cs.STATUS_UNKNOWN
    assert result["category"] == expected_category
    assert result["label"] == expected_label
    assert result["required_for"] == cs.SERVICE_METADATA[service_id]["required_for"]


def test_service_metadata_covers_registry():
    """SERVICE_REGISTRY と SERVICE_METADATA の service_id が一致している（ドリフト防止）"""
    assert set(cs.SERVICE_METADATA.keys()) == set(cs.SERVICE_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_recheck(monkeypatch):
    calls = {"n": 0}

    def _counting_check(mode: str) -> dict[str, Any]:
        calls["n"] += 1
        return cs._build_result(
            service_id="claude",
            label="Claude Code",
            category=cs.CategoryLLMAgent,
            status=cs.STATUS_CONNECTED,
            summary="ok",
            detail=None,
            required_for=[],
            message_key="x",
            mode=mode,
        )

    monkeypatch.setitem(cs.SERVICE_REGISTRY, "claude", _counting_check)

    cs.get_service_status("claude")
    cs.get_service_status("claude")

    assert calls["n"] == 1


def test_cache_refresh_forces_recheck(monkeypatch):
    calls = {"n": 0}

    def _counting_check(mode: str) -> dict[str, Any]:
        calls["n"] += 1
        return cs._build_result(
            service_id="claude",
            label="Claude Code",
            category=cs.CategoryLLMAgent,
            status=cs.STATUS_CONNECTED,
            summary="ok",
            detail=None,
            required_for=[],
            message_key="x",
            mode=mode,
        )

    monkeypatch.setitem(cs.SERVICE_REGISTRY, "claude", _counting_check)

    cs.get_service_status("claude")
    cs.get_service_status("claude", refresh=True)

    assert calls["n"] == 2


def test_unknown_mode_falls_back_to_shallow(monkeypatch):
    """未知の mode はキャッシュキー肥大を避けるため shallow にフォールバックする"""
    captured: list[str] = []

    def _capturing_check(mode: str) -> dict[str, Any]:
        captured.append(mode)
        return cs._build_result(
            service_id="claude",
            label="Claude Code",
            category=cs.CategoryLLMAgent,
            status=cs.STATUS_CONNECTED,
            summary="ok",
            detail=None,
            required_for=[],
            message_key="x",
            mode=mode,
        )

    monkeypatch.setitem(cs.SERVICE_REGISTRY, "claude", _capturing_check)

    result = cs.get_service_status("claude", mode="bogus")

    assert captured == [cs.MODE_SHALLOW]
    assert result["mode"] == cs.MODE_SHALLOW


def test_get_all_statuses_normalizes_mode(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", _make_which({}))
    monkeypatch.setattr(cs, "_notion_mcp_configured", lambda: (False, None))

    bundle = cs.get_all_statuses(mode="weird-value")

    assert bundle["mode"] == cs.MODE_SHALLOW
    for svc in bundle["services"]:
        assert svc["mode"] == cs.MODE_SHALLOW


def test_cache_separates_shallow_and_deep(monkeypatch):
    """shallow と deep は別キーでキャッシュされる"""
    calls = []

    def _counting_check(mode: str) -> dict[str, Any]:
        calls.append(mode)
        return cs._build_result(
            service_id="notion_mcp",
            label="Notion MCP",
            category=cs.CategoryMCP,
            status=cs.STATUS_CONNECTED,
            summary="ok",
            detail=None,
            required_for=[],
            message_key="x",
            mode=mode,
        )

    monkeypatch.setitem(cs.SERVICE_REGISTRY, "notion_mcp", _counting_check)

    cs.get_service_status("notion_mcp", mode="shallow")
    cs.get_service_status("notion_mcp", mode="deep")
    cs.get_service_status("notion_mcp", mode="shallow")  # cached
    cs.get_service_status("notion_mcp", mode="deep")  # cached

    assert calls == ["shallow", "deep"]


# ---------------------------------------------------------------------------
# Severity mapping completeness
# ---------------------------------------------------------------------------


def test_severity_mapping_covers_all_status_constants():
    all_statuses = {
        cs.STATUS_CONNECTED,
        cs.STATUS_NOT_INSTALLED,
        cs.STATUS_NOT_AUTHENTICATED,
        cs.STATUS_TIMEOUT,
        cs.STATUS_UNSUPPORTED,
        cs.STATUS_DISABLED,
        cs.STATUS_UNKNOWN,
    }
    assert all_statuses <= set(cs.SEVERITY_BY_STATUS.keys())
