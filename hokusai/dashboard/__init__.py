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

# scripts/dashboard.py のデフォルト port と一致させる。
# CLI / start_dashboard() で port が未指定（None）の場合、必ずこの値に解決して
# _port_in_use チェック / race condition 変換が走るようにする（port=None で
# 検証がスキップされる buggy パスを排除）。
DEFAULT_DASHBOARD_PORT = 8765


class DashboardPortInUseError(Exception):
    """指定された port が既に listen 済み"""


def _port_in_use(port: int, host: str = "localhost") -> bool:
    """指定 port が既に listen されているか確認

    `errno.EADDRINUSE` のみを「使用中」と判定し、それ以外の OSError
    （EACCES: 特権ポートへの bind 権限不足、EAFNOSUPPORT 等）は呼び出し側に
    伝搬する。これにより「使用中」と「アクセス不可」を区別できる。

    port の事前バリデーション:
    - bool 型は明示的に拒否（Python では bool が int サブクラス）
    - 1..65535 の範囲外は ValueError（socket.bind の OverflowError /
      OSError(EINVAL) を分かりやすい例外にする）

    Raises:
        ValueError: port が 1..65535 範囲外、または bool / int 以外の型
        OSError: EADDRINUSE 以外の OS エラー（権限不足など）
    """
    # bool は Python では int サブクラスなので明示的に除外
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError(f"port は int である必要があります: got {type(port).__name__}")
    if not (1 <= port <= 65535):
        raise ValueError(f"port は 1..65535 の範囲である必要があります: got {port}")

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
        port: listen port。None の場合は DEFAULT_DASHBOARD_PORT (8765) に解決する。
            これにより _port_in_use チェックと race condition 変換が常に動作し、
            エラーメッセージにも実効 port 番号が含まれる

    Returns:
        プロセスの exit code（通常は KeyboardInterrupt で 0 だが、port 衝突等で 1）

    Raises:
        DashboardPortInUseError: 指定 port が既に listen 済み（事前検出 or race condition）
        ValueError: port が範囲外（_port_in_use のバリデーションで raise）
    """
    # port が未指定（None）でも、scripts/dashboard.py 内部で 8765 にフォールバック
    # するため、HOKUSAI 側でも同じ値に明示的に解決して、衝突検出 / エラー
    # メッセージ / race condition 変換すべてが正しい port で動くようにする
    if port is None:
        port = DEFAULT_DASHBOARD_PORT

    # port 衝突を起動前に検出（実装計画書 §9.1）
    # EADDRINUSE 以外の OSError（権限不足等）はそのまま伝搬させて、
    # 「使用中」誤判定を避ける
    if _port_in_use(port):
        raise DashboardPortInUseError(
            f"port {port} は既に使用中です。"
            "別 profile の dashboard が動作しているか、profile registry の "
            "dashboard.port を確認してください。"
        )

    env = prepare_dashboard_env(config, profile_name=profile_name, port=port)
    os.environ.update(env)

    # scripts/dashboard.py を import して main() を呼ぶ。
    # 重要: モジュールが既に import 済み（テスト経由等）の場合、import 時の env
    # 読み取り（PORT / DB_PATH / HOKUSAI_PROFILE_NAME 等）は **再評価されない**。
    # 挙動を import 順序に依存させないため、env 更新後に明示的に
    # `refresh_from_env()` を呼んで module 変数を再評価する。
    # （importlib.reload は monkeypatch を消すためテスト互換性の観点で不採用）
    # scripts は pyproject.toml の wheel packages に含めているため、pip
    # インストール環境でも import 可能。
    from scripts import dashboard as dashboard_module

    dashboard_module.refresh_from_env()

    # 既存の scripts.dashboard.main() を呼ぶ
    # Race condition 対策: _port_in_use() チェック後〜実 bind までの間に
    # 他プロセスが port を取得した場合、ThreadingHTTPServer が EADDRINUSE を
    # 投げる。これを DashboardPortInUseError に変換して呼び出し側に統一形式で
    # 通知する（CLI 側で同じ except 句で扱える）。
    try:
        dashboard_module.main()
    except OSError as e:
        # port は冒頭で必ず int に解決済みなので、None ガードは不要
        if e.errno == errno.EADDRINUSE:
            raise DashboardPortInUseError(
                f"port {port} の bind に失敗しました（_port_in_use チェック後に"
                f"他プロセスが取得した可能性）: {e}"
            ) from e
        raise
    return 0


__all__ = [
    "DashboardPortInUseError",
    "prepare_dashboard_env",
    "start_dashboard",
]
