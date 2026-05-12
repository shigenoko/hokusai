"""HOKUSAI Operations Console (Dashboard) entry point.

実装計画書 §9.2 に従い、`scripts/dashboard.py` を薄いラッパとして残しつつ、
このモジュールが profile 解決後の dashboard 起動エントリを提供する。

将来的にここへ HTTP server / rendering / API ハンドラを移行する（フォローアップ PR）。
現状は profile config から PORT / DB_PATH / profile name を環境変数に流し込み、
scripts/dashboard.py の `main()` を呼ぶシム。
"""

from __future__ import annotations

import errno
import os
import socket
from pathlib import Path

from ..config import WorkflowConfig


class DashboardPortInUseError(Exception):
    """指定された port が既に listen 済み"""


def _port_in_use(port: int, host: str = "localhost") -> bool:
    """指定 port が既に listen されているか確認

    `errno.EADDRINUSE` のみを「使用中」と判定し、それ以外の OSError
    （EACCES: 特権ポートへの bind 権限不足、EAFNOSUPPORT 等）は呼び出し側に
    伝搬する。これにより「使用中」と「アクセス不可」を区別できる。

    Raises:
        OSError: EADDRINUSE 以外の OS エラー（権限不足など）
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.bind((host, port))
            return False
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return True
            raise


def prepare_dashboard_env(
    config: WorkflowConfig,
    *,
    profile_name: str | None = None,
    port: int | None = None,
) -> dict[str, str]:
    """dashboard 起動前に環境変数を組み立てる。

    `scripts/dashboard.py` は import 時に以下の env を参照する:
      - HOKUSAI_DASHBOARD_PORT
      - HOKUSAI_DASHBOARD_DB_PATH
      - HOKUSAI_DASHBOARD_CHECKPOINT_DB_PATH
      - HOKUSAI_DASHBOARD_PROFILE

    Returns:
        セットすべき env var の dict（呼び出し側で os.environ.update する）
    """
    env: dict[str, str] = {}

    if port is not None:
        env["HOKUSAI_DASHBOARD_PORT"] = str(port)
    if config.database_path:
        env["HOKUSAI_DASHBOARD_DB_PATH"] = str(Path(config.database_path).expanduser())
    if config.checkpoint_db_path:
        env["HOKUSAI_DASHBOARD_CHECKPOINT_DB_PATH"] = str(
            Path(config.checkpoint_db_path).expanduser()
        )
    if profile_name:
        env["HOKUSAI_DASHBOARD_PROFILE"] = profile_name

    return env


def start_dashboard(
    config: WorkflowConfig,
    *,
    profile_name: str | None = None,
    port: int | None = None,
) -> int:
    """profile 解決済み config で dashboard を起動する。

    Args:
        config: WorkflowConfig（profile 解決後のもの）
        profile_name: ヘッダ表示用の profile 名（指定時のみバッジ表示）
        port: listen port（省略時は scripts/dashboard.py のデフォルト 8765）

    Returns:
        プロセスの exit code（通常は KeyboardInterrupt で 0 だが、port 衝突等で 1）

    Raises:
        DashboardPortInUseError: 指定 port が既に listen 済み
    """
    # port 衝突を起動前に検出（実装計画書 §9.1）
    # EADDRINUSE 以外の OSError（権限不足等）はそのまま伝搬させて、
    # 「使用中」誤判定を避ける
    if port is not None and _port_in_use(port):
        raise DashboardPortInUseError(
            f"port {port} は既に使用中です。"
            "別 profile の dashboard が動作しているか、profile registry の "
            "dashboard.port を確認してください。"
        )

    env = prepare_dashboard_env(config, profile_name=profile_name, port=port)
    os.environ.update(env)

    # scripts/dashboard.py を import して main() を呼ぶ
    # （import 時に PORT / DB_PATH が env から再評価される）
    # scripts は pyproject.toml の wheel packages に含めているため、
    # pip インストール環境でも import 可能。
    from scripts import dashboard as dashboard_module

    # 既存の scripts.dashboard.main() を呼ぶ
    dashboard_module.main()
    return 0


__all__ = [
    "DashboardPortInUseError",
    "prepare_dashboard_env",
    "start_dashboard",
]
