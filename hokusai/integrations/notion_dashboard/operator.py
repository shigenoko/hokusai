"""Operator（実行者）解決ユーティリティ

複数エンジニアが同じ profile を共有運用する場合、Notion Workflows DB に
「誰が hokusai start を叩いたか」を記録するために使う。

解決順序:
1. 環境変数 `HOKUSAI_OPERATOR_NAME`（空白以外の値が設定されていればそれ）
2. `whoami` コマンドの出力
3. いずれも失敗したら "(unknown)"

Issue #21 / v0.4.8。
"""

from __future__ import annotations

import os
import shutil
import subprocess

from ...logging_config import get_logger

logger = get_logger("integrations.notion_dashboard.operator")

# `whoami` 実行のタイムアウト（秒）。コマンドが詰まっても workflow 開始を
# ブロックしないよう短めに設定する。
_WHOAMI_TIMEOUT_SECONDS = 3.0

# 解決できないときの fallback 値。Notion 上で「未設定」を一目で識別できるよう、
# 括弧 `()` で囲った形にする（実値は `(unknown)`、リテラルにクォートは含まない）。
UNKNOWN_OPERATOR = "(unknown)"


def resolve_operator_name() -> str:
    """現在実行中の operator 名を解決して返す。

    Returns:
        operator 名の文字列。解決失敗時は :data:`UNKNOWN_OPERATOR`（"(unknown)"）。

    Notes:
        - 環境変数 `HOKUSAI_OPERATOR_NAME` が設定されていれば最優先。
          空白のみの値は採用しない（フォールバックに進む）。
        - whoami は POSIX 環境で利用可能なコマンド。実行時に PATH に無い場合や
          タイムアウトした場合は、例外を呑んで fallback に進む。
        - 戻り値はトリム済み（前後の空白を除去）。改行も除去する。
    """
    env_value = os.environ.get("HOKUSAI_OPERATOR_NAME")
    if env_value and env_value.strip():
        return env_value.strip()

    whoami_path = shutil.which("whoami")
    if whoami_path is None:
        logger.debug("whoami コマンドが PATH に見つからないため fallback を返します")
        return UNKNOWN_OPERATOR

    try:
        result = subprocess.run(  # noqa: S603
            [whoami_path],
            capture_output=True,
            text=True,
            timeout=_WHOAMI_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "whoami がタイムアウトしました（%.1fs）。fallback を返します",
            _WHOAMI_TIMEOUT_SECONDS,
        )
        return UNKNOWN_OPERATOR
    except OSError as e:
        logger.warning("whoami 実行に失敗: %s: %s", type(e).__name__, e)
        return UNKNOWN_OPERATOR

    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    logger.debug(
        "whoami が空 / 非ゼロ exit code を返しました (returncode=%s, stdout=%r)",
        result.returncode, result.stdout,
    )
    return UNKNOWN_OPERATOR
