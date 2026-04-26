"""
hokusai connect <service> CLI commands

外部サービスへの認証導線を提供する。Phase C 最小スコープ:

- ``hokusai connect github`` — gh の存在/認証確認 → 未認証なら gh auth login へ誘導
- ``hokusai connect gitlab`` — glab の存在/認証確認 → 未認証なら glab auth login へ誘導
- ``hokusai connect --status`` — connection_status の内容を CLI で表示

Linear / Jira / Notion トークンの保存は本コマンドの対象外。シークレット入力は
Web UI に持たせず、既存 CLI（gh / glab）の責務に委譲する。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any

from ...integrations import connection_status as cs

SUPPORTED_SERVICES: dict[str, dict[str, Any]] = {
    "github": {
        "label": "GitHub CLI",
        "cli": "gh",
        "status_command": ["gh", "auth", "status"],
        "auth_command": ["gh", "auth", "login"],
        "install_url": "https://cli.github.com/",
    },
    "gitlab": {
        "label": "GitLab CLI",
        "cli": "glab",
        "status_command": ["glab", "auth", "status"],
        "auth_command": ["glab", "auth", "login"],
        "install_url": "https://gitlab.com/gitlab-org/cli",
    },
}


STATUS_LABEL: dict[str, str] = {
    cs.STATUS_CONNECTED: "接続済み",
    cs.STATUS_NOT_INSTALLED: "未インストール",
    cs.STATUS_NOT_AUTHENTICATED: "未認証",
    cs.STATUS_TIMEOUT: "タイムアウト",
    cs.STATUS_UNSUPPORTED: "未対応",
    cs.STATUS_DISABLED: "無効化",
    cs.STATUS_UNKNOWN: "エラー",
}

SEVERITY_ICON: dict[str, str] = {
    "ok": "✓",
    "warn": "⚠",
    "error": "✗",
    "info": "○",
}


def is_interactive_session() -> bool:
    """stdin / stdout が両方 TTY のときのみ対話可能とみなす。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def connect_service(
    service_name: str,
    *,
    no_interactive: bool = False,
    force: bool = False,
) -> int:
    """サービスへ接続する。終了コードを返す。

    - 0: 成功 / 既に認証済み / 表示のみで完了
    - 1: CLI 未インストール / 認証状態確認のタイムアウト
    - 2: 未対応サービス
    - 130: 中断
    - その他: 認証コマンドの非ゼロ終了
    """
    spec = SUPPORTED_SERVICES.get(service_name)
    if spec is None:
        print(f"✗ 未対応のサービス: {service_name}", file=sys.stderr)
        print(
            f"  対応サービス: {', '.join(SUPPORTED_SERVICES.keys())}",
            file=sys.stderr,
        )
        return 2

    cli_name = spec["cli"]
    label = spec["label"]

    if not shutil.which(cli_name):
        print(f"✗ {label} ({cli_name}) が PATH に見つかりません。")
        print(f"  インストール: {spec['install_url']}")
        return 1

    print(f"→ {label} の認証状態を確認しています…")
    try:
        status_result = subprocess.run(
            spec["status_command"], capture_output=True, text=True, timeout=5.0
        )
    except subprocess.TimeoutExpired:
        print(
            f"✗ `{' '.join(spec['status_command'])}` が 5 秒以内に応答しませんでした。"
        )
        return 1

    output = (status_result.stderr or status_result.stdout or "").strip()
    already_authenticated = status_result.returncode == 0

    if already_authenticated and not force:
        print(f"✓ {label} は既に認証済みです。")
        if output:
            for line in output.splitlines():
                print(f"  {line}")
        return 0

    if already_authenticated:
        print(f"✓ {label} は認証済みですが、--force が指定されたため再認証します。")
    else:
        print(f"⚠ {label} は未認証です。")
        if output:
            for line in output.splitlines()[:10]:
                print(f"  {line}")

    auth_cmd_str = " ".join(spec["auth_command"])
    interactive = not no_interactive and is_interactive_session()

    if not interactive:
        print()
        print("以下のコマンドを実行して認証してください:")
        print(f"  {auth_cmd_str}")
        return 0

    print()
    try:
        answer = input(f"`{auth_cmd_str}` を実行しますか? [y/N]: ").strip().lower()
    except EOFError:
        print()
        print("以下のコマンドを実行して認証してください:")
        print(f"  {auth_cmd_str}")
        return 0

    if answer not in ("y", "yes"):
        print(f"キャンセルしました。手動で実行する場合: {auth_cmd_str}")
        return 0

    # 認証実行後にダッシュボードのキャッシュを汚さないよう、ここで連携モジュールの
    # キャッシュをクリアしておく（同一プロセス内で続けて status を呼んだ場合に古い
    # 「未認証」状態が返らないようにする）。
    cs.clear_cache()

    try:
        result = subprocess.run(spec["auth_command"])
    except KeyboardInterrupt:
        print()
        print("中断しました。")
        return 130

    return result.returncode


def show_status(*, refresh: bool = True) -> int:
    """全サービスの接続状態を CLI に表示する。

    CLI から手動で呼ばれた場合はキャッシュより最新性の方が重要なため、デフォルトで
    `refresh=True` にしてダッシュボードキャッシュを無視して再チェックする。
    """
    bundle = cs.get_all_statuses(refresh=refresh)
    services = bundle["services"]
    print(f"# サービス接続状態（{bundle['checked_at']}）")
    print()
    if not services:
        print("（登録されているサービスがありません）")
        return 0

    label_width = max(len(svc["label"]) for svc in services)
    for svc in services:
        icon = SEVERITY_ICON.get(svc["severity"], "?")
        label = svc["label"].ljust(label_width)
        status_text = STATUS_LABEL.get(svc["status"], svc["status"])
        print(f"  {icon} {label}  {status_text}")
        if svc.get("summary"):
            print(f"      {svc['summary']}")
        next_action = svc.get("next_action")
        if next_action:
            command = next_action.get("command")
            docs_url = next_action.get("docs_url")
            if command:
                print(f"      → {command}")
            elif docs_url:
                print(f"      → {docs_url}")
    return 0
