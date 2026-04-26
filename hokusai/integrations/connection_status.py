"""
Connection Status Service

各サービス（Claude Code / Codex / gh / glab / Notion MCP / Jira / Linear）の
接続状態を判定するためのサービスレジストリと、結果のキャッシュを提供する。

このモジュールは副作用のないヘルスチェックのみを行い、認証や設定変更は行わない。
シークレットを保存・送信せず、検出可能な範囲（CLI 存在、認証コマンド成否、
MCP 設定ファイルの記述）から状態を判定する。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..logging_config import get_logger

logger = get_logger("connection_status")


STATUS_CONNECTED = "connected"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_NOT_AUTHENTICATED = "not_authenticated"
STATUS_TIMEOUT = "timeout"
STATUS_UNSUPPORTED = "unsupported"
STATUS_DISABLED = "disabled"
STATUS_UNKNOWN = "unknown"

SEVERITY_BY_STATUS: dict[str, str] = {
    STATUS_CONNECTED: "ok",
    STATUS_NOT_INSTALLED: "error",
    STATUS_NOT_AUTHENTICATED: "warn",
    STATUS_TIMEOUT: "warn",
    STATUS_UNSUPPORTED: "info",
    STATUS_DISABLED: "info",
    STATUS_UNKNOWN: "error",
}

DEFAULT_TTL_SECONDS = 30.0
TIMEOUT_TTL_SECONDS = 5.0

CategoryLLMAgent = "llm_agent"
CategoryGitHosting = "git_hosting"
CategoryTaskBackend = "task_backend"
CategoryMCP = "mcp"

MODE_SHALLOW = "shallow"
MODE_DEEP = "deep"
VALID_MODES: frozenset[str] = frozenset({MODE_SHALLOW, MODE_DEEP})


def _normalize_mode(mode: str) -> str:
    """未知の mode は shallow にフォールバックする。

    `get_service_status` のキャッシュキーに mode が含まれるため、任意文字列を
    受け入れるとキャッシュが無制限に増える。バリデーションして既知の値だけを
    通すことで、API 契約を安定させる。
    """
    return mode if mode in VALID_MODES else MODE_SHALLOW


# サービスごとの静的メタデータ。チェック関数の例外フォールバックなど、
# 動的なチェック結果を作れない場面で `label` / `category` / `required_for` を
# 解決するための単一情報源として参照する。
SERVICE_METADATA: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude Code",
        "category": CategoryLLMAgent,
        "required_for": ["implementation"],
    },
    "codex": {
        "label": "OpenAI Codex",
        "category": CategoryLLMAgent,
        "required_for": ["cross_review"],
    },
    "gh": {
        "label": "GitHub CLI",
        "category": CategoryGitHosting,
        "required_for": ["git_hosting", "pr_creation", "review_comment_reply"],
    },
    "glab": {
        "label": "GitLab CLI",
        "category": CategoryGitHosting,
        "required_for": ["git_hosting", "pr_creation"],
    },
    "notion_mcp": {
        "label": "Notion MCP",
        "category": CategoryMCP,
        "required_for": ["notion_sync", "task_backend"],
    },
    "jira": {
        "label": "Jira",
        "category": CategoryTaskBackend,
        "required_for": ["task_backend"],
    },
    "linear": {
        "label": "Linear",
        "category": CategoryTaskBackend,
        "required_for": ["task_backend"],
    },
}


_cache_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ttl_for_status(status: str) -> float:
    if status in (STATUS_TIMEOUT, STATUS_UNKNOWN):
        return TIMEOUT_TTL_SECONDS
    return DEFAULT_TTL_SECONDS


def _build_result(
    *,
    service_id: str,
    label: str,
    category: str,
    status: str,
    summary: str,
    detail: str | None,
    required_for: list[str],
    message_key: str,
    next_action: dict[str, Any] | None = None,
    docs_url: str | None = None,
    mode: str = "shallow",
) -> dict[str, Any]:
    return {
        "id": service_id,
        "label": label,
        "category": category,
        "status": status,
        "severity": SEVERITY_BY_STATUS.get(status, "error"),
        "required_for": required_for,
        "message_key": message_key,
        "summary": summary,
        "detail": detail,
        "next_action": next_action,
        "docs_url": docs_url,
        "checked_at": _now_iso(),
        "cache_ttl_seconds": int(_ttl_for_status(status)),
        "mode": mode,
    }


def _run_cli(cmd: list[str], timeout: float) -> tuple[int, str, str] | None:
    """Returns (returncode, stdout, stderr) or None on timeout."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return None


def _check_claude(mode: str) -> dict[str, Any]:
    service_id = "claude"
    label = "Claude Code"
    required_for = ["implementation"]
    if not shutil.which("claude"):
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryLLMAgent,
            status=STATUS_NOT_INSTALLED,
            summary="Claude Code CLI が見つかりません",
            detail="`claude` コマンドが PATH にありません",
            required_for=required_for,
            message_key="connection.claude.not_installed",
            next_action={
                "type": "docs",
                "label": "Claude Code をインストール",
                "command": None,
                "docs_url": "https://claude.com/claude-code",
            },
            docs_url="https://claude.com/claude-code",
            mode=mode,
        )
    res = _run_cli(["claude", "--version"], timeout=3.0)
    if res is None:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryLLMAgent,
            status=STATUS_TIMEOUT,
            summary="Claude Code のバージョン確認がタイムアウトしました",
            detail="`claude --version` が 3 秒以内に応答しませんでした",
            required_for=required_for,
            message_key="connection.claude.timeout",
            mode=mode,
        )
    code, stdout, stderr = res
    if code != 0:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryLLMAgent,
            status=STATUS_UNKNOWN,
            summary="Claude Code の状態確認に失敗しました",
            detail=stderr or stdout or f"exit={code}",
            required_for=required_for,
            message_key="connection.claude.unknown",
            mode=mode,
        )
    version_line = (stdout or stderr).splitlines()[0] if (stdout or stderr) else ""
    return _build_result(
        service_id=service_id,
        label=label,
        category=CategoryLLMAgent,
        status=STATUS_CONNECTED,
        summary="Claude Code が利用可能です",
        detail=f"claude --version → {version_line}",
        required_for=required_for,
        message_key="connection.claude.connected",
        mode=mode,
    )


def _check_codex(mode: str) -> dict[str, Any]:
    service_id = "codex"
    label = "OpenAI Codex"
    required_for = ["cross_review"]
    if not shutil.which("codex"):
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryLLMAgent,
            status=STATUS_NOT_INSTALLED,
            summary="Codex CLI が見つかりません",
            detail="`codex` コマンドが PATH にありません",
            required_for=required_for,
            message_key="connection.codex.not_installed",
            next_action={
                "type": "docs",
                "label": "Codex CLI のセットアップ手順",
                "command": None,
                "docs_url": "https://github.com/openai/codex",
            },
            docs_url="https://github.com/openai/codex",
            mode=mode,
        )
    res = _run_cli(["codex", "--version"], timeout=3.0)
    if res is None:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryLLMAgent,
            status=STATUS_TIMEOUT,
            summary="Codex のバージョン確認がタイムアウトしました",
            detail="`codex --version` が 3 秒以内に応答しませんでした",
            required_for=required_for,
            message_key="connection.codex.timeout",
            mode=mode,
        )
    code, stdout, stderr = res
    if code != 0:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryLLMAgent,
            status=STATUS_UNKNOWN,
            summary="Codex の状態確認に失敗しました",
            detail=stderr or stdout or f"exit={code}",
            required_for=required_for,
            message_key="connection.codex.unknown",
            mode=mode,
        )
    version_line = (stdout or stderr).splitlines()[0] if (stdout or stderr) else ""
    return _build_result(
        service_id=service_id,
        label=label,
        category=CategoryLLMAgent,
        status=STATUS_CONNECTED,
        summary="Codex CLI が利用可能です",
        detail=f"codex --version → {version_line}",
        required_for=required_for,
        message_key="connection.codex.connected",
        mode=mode,
    )


def _check_gh(mode: str) -> dict[str, Any]:
    service_id = "gh"
    label = "GitHub CLI"
    required_for = ["git_hosting", "pr_creation", "review_comment_reply"]
    if not shutil.which("gh"):
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryGitHosting,
            status=STATUS_NOT_INSTALLED,
            summary="gh CLI が見つかりません",
            detail="`gh` コマンドが PATH にありません",
            required_for=required_for,
            message_key="connection.gh.not_installed",
            next_action={
                "type": "docs",
                "label": "gh CLI をインストール",
                "command": None,
                "docs_url": "https://cli.github.com/",
            },
            docs_url="https://cli.github.com/",
            mode=mode,
        )
    res = _run_cli(["gh", "auth", "status"], timeout=5.0)
    if res is None:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryGitHosting,
            status=STATUS_TIMEOUT,
            summary="gh auth status がタイムアウトしました",
            detail="`gh auth status` が 5 秒以内に応答しませんでした",
            required_for=required_for,
            message_key="connection.gh.timeout",
            mode=mode,
        )
    code, stdout, stderr = res
    output = stderr or stdout
    if code == 0:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryGitHosting,
            status=STATUS_CONNECTED,
            summary="GitHub CLI は認証済みです",
            detail=output[:400] if output else "gh auth status が成功しました",
            required_for=required_for,
            message_key="connection.gh.connected",
            mode=mode,
        )
    return _build_result(
        service_id=service_id,
        label=label,
        category=CategoryGitHosting,
        status=STATUS_NOT_AUTHENTICATED,
        summary="GitHub CLI が未認証です",
        detail=output[:400] if output else f"exit={code}",
        required_for=required_for,
        message_key="connection.gh.not_authenticated",
        next_action={
            "type": "command",
            "label": "GitHub に接続",
            # `hokusai connect github` は内部で `gh auth login` を実行する
            # ラッパー。ダッシュボードからは hokusai 経由のコマンドを案内し、
            # CLI 状態クリアと TTY/非 TTY のフォールバックも一括して任せる。
            "command": "hokusai connect github",
            "docs_url": None,
        },
        mode=mode,
    )


def _check_glab(mode: str) -> dict[str, Any]:
    service_id = "glab"
    label = "GitLab CLI"
    required_for = ["git_hosting", "pr_creation"]
    if not shutil.which("glab"):
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryGitHosting,
            status=STATUS_NOT_INSTALLED,
            summary="glab CLI が見つかりません",
            detail="`glab` コマンドが PATH にありません",
            required_for=required_for,
            message_key="connection.glab.not_installed",
            next_action={
                "type": "docs",
                "label": "glab CLI をインストール",
                "command": None,
                "docs_url": "https://gitlab.com/gitlab-org/cli",
            },
            docs_url="https://gitlab.com/gitlab-org/cli",
            mode=mode,
        )
    res = _run_cli(["glab", "auth", "status"], timeout=5.0)
    if res is None:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryGitHosting,
            status=STATUS_TIMEOUT,
            summary="glab auth status がタイムアウトしました",
            detail="`glab auth status` が 5 秒以内に応答しませんでした",
            required_for=required_for,
            message_key="connection.glab.timeout",
            mode=mode,
        )
    code, stdout, stderr = res
    output = stderr or stdout
    if code == 0:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryGitHosting,
            status=STATUS_CONNECTED,
            summary="GitLab CLI は認証済みです",
            detail=output[:400] if output else "glab auth status が成功しました",
            required_for=required_for,
            message_key="connection.glab.connected",
            mode=mode,
        )
    return _build_result(
        service_id=service_id,
        label=label,
        category=CategoryGitHosting,
        status=STATUS_NOT_AUTHENTICATED,
        summary="GitLab CLI が未認証です",
        detail=output[:400] if output else f"exit={code}",
        required_for=required_for,
        message_key="connection.glab.not_authenticated",
        next_action={
            "type": "command",
            "label": "GitLab に接続",
            "command": "hokusai connect gitlab",
            "docs_url": None,
        },
        mode=mode,
    )


def _notion_mcp_configured() -> tuple[bool, str | None]:
    """MCP 設定ファイルに notion サーバが登録されているかを確認。

    Returns:
        (configured, source_path) — configured が True のとき source_path に
        記述されていたファイルパスを入れる。
    """
    candidates = [
        Path.home() / ".claude.json",
        Path.home() / ".claude" / "mcp.json",
        Path.cwd() / ".mcp.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue
        servers = _extract_mcp_servers(data)
        if any("notion" in name.lower() for name in servers):
            return True, str(path)
    return False, None


def _extract_mcp_servers(data: Any) -> list[str]:
    """MCP 設定 JSON からサーバ名のリストを抽出。

    Claude Code の `~/.claude.json` は `mcpServers` を複数箇所に持つことがあるため、
    再帰的に探索する。
    """
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            servers = node.get("mcpServers")
            if isinstance(servers, dict):
                found.extend(servers.keys())
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return found


def _check_notion_mcp(mode: str) -> dict[str, Any]:
    service_id = "notion_mcp"
    label = "Notion MCP"
    required_for = ["notion_sync", "task_backend"]

    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryMCP,
            status=STATUS_DISABLED,
            summary="HOKUSAI_SKIP_NOTION=1 により無効化されています",
            detail=None,
            required_for=required_for,
            message_key="connection.notion_mcp.disabled",
            mode=mode,
        )

    configured, source = _notion_mcp_configured()
    if not configured:
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryMCP,
            status=STATUS_NOT_INSTALLED,
            summary="Notion MCP サーバが MCP 設定に見つかりません",
            detail="`~/.claude.json` / `~/.claude/mcp.json` / `./.mcp.json` のいずれにも notion 関連サーバの記述がありません",
            required_for=required_for,
            message_key="connection.notion_mcp.not_installed",
            next_action={
                "type": "command",
                "label": "Notion MCP を追加",
                "command": "claude mcp add notion ...",
                "docs_url": "https://docs.claude.com/en/docs/claude-code/mcp",
            },
            mode=mode,
        )

    if mode == "deep":
        # deep モードでも Claude を介した実行は重いため、現段階では設定確認のみで OK を返す。
        # 将来 MCP サーバへの直接 ping を追加するための拡張ポイント。
        return _build_result(
            service_id=service_id,
            label=label,
            category=CategoryMCP,
            status=STATUS_CONNECTED,
            summary="Notion MCP サーバが MCP 設定に登録されています",
            detail=f"設定ファイル: {source}（deep ping は未実装）",
            required_for=required_for,
            message_key="connection.notion_mcp.connected",
            mode=mode,
        )

    return _build_result(
        service_id=service_id,
        label=label,
        category=CategoryMCP,
        status=STATUS_CONNECTED,
        summary="Notion MCP サーバが MCP 設定に登録されています",
        detail=f"設定ファイル: {source}",
        required_for=required_for,
        message_key="connection.notion_mcp.connected",
        mode=mode,
    )


def _check_jira(mode: str) -> dict[str, Any]:
    return _build_result(
        service_id="jira",
        label="Jira",
        category=CategoryTaskBackend,
        status=STATUS_UNSUPPORTED,
        summary="Jira 連携は実装中です",
        detail="クライアントはスケルトン実装のため、現時点では本番運用できません",
        required_for=["task_backend"],
        message_key="connection.jira.unsupported",
        mode=mode,
    )


def _check_linear(mode: str) -> dict[str, Any]:
    return _build_result(
        service_id="linear",
        label="Linear",
        category=CategoryTaskBackend,
        status=STATUS_UNSUPPORTED,
        summary="Linear 連携は実装中です",
        detail="クライアントはスケルトン実装のため、現時点では本番運用できません",
        required_for=["task_backend"],
        message_key="connection.linear.unsupported",
        mode=mode,
    )


SERVICE_REGISTRY: dict[str, Callable[[str], dict[str, Any]]] = {
    "claude": _check_claude,
    "codex": _check_codex,
    "gh": _check_gh,
    "glab": _check_glab,
    "notion_mcp": _check_notion_mcp,
    "jira": _check_jira,
    "linear": _check_linear,
}

SERVICE_ORDER: list[str] = [
    "claude",
    "codex",
    "gh",
    "glab",
    "notion_mcp",
    "jira",
    "linear",
]


def get_service_status(
    service_id: str, *, refresh: bool = False, mode: str = MODE_SHALLOW
) -> dict[str, Any] | None:
    """単一サービスの接続状態を取得。

    Args:
        service_id: サービス ID（SERVICE_REGISTRY のキー）
        refresh: True のときキャッシュを無視して再チェックする
        mode: "shallow" または "deep"。deep は将来的により詳細なチェックを行う。
            未知の値は "shallow" にフォールバックする（キャッシュキー肥大の防止）。

    Returns:
        サービスのステータス辞書。未知の service_id の場合は None。
    """
    check_fn = SERVICE_REGISTRY.get(service_id)
    if check_fn is None:
        return None

    mode = _normalize_mode(mode)
    cache_key = (service_id, mode)
    now = time.monotonic()

    if not refresh:
        with _cache_lock:
            cached = _cache.get(cache_key)
        if cached is not None:
            result, ts = cached
            ttl = result.get("cache_ttl_seconds", DEFAULT_TTL_SECONDS)
            if now - ts < ttl:
                return result

    try:
        result = check_fn(mode)
    except Exception as exc:  # 想定外エラーは unknown で返す
        logger.exception("connection check failed: %s", service_id)
        meta = SERVICE_METADATA.get(service_id, {})
        result = _build_result(
            service_id=service_id,
            label=meta.get("label", service_id),
            category=meta.get("category", CategoryLLMAgent),
            status=STATUS_UNKNOWN,
            summary=f"{meta.get('label', service_id)} の状態確認中にエラーが発生しました",
            detail=str(exc),
            required_for=meta.get("required_for", []),
            message_key=f"connection.{service_id}.unknown",
            mode=mode,
        )

    with _cache_lock:
        _cache[cache_key] = (result, now)
    return result


def get_all_statuses(*, refresh: bool = False, mode: str = MODE_SHALLOW) -> dict[str, Any]:
    """全サービスの接続状態をまとめて返す。"""
    mode = _normalize_mode(mode)
    services = []
    for service_id in SERVICE_ORDER:
        status = get_service_status(service_id, refresh=refresh, mode=mode)
        if status is not None:
            services.append(status)
    return {
        "success": True,
        "checked_at": _now_iso(),
        "mode": mode,
        "services": services,
    }


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
