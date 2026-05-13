"""Operations Console の「どの Notion か」識別表示用ヘルパ。

dashboard.render_notion_dashboard_panel() から使う想定。
Notion API は workspace 名を直接返さないため、profile 名 / env 変数名 /
DB ID（マスク済み）/ DB URL / bot user name の組み合わせで識別する。

Issue: https://github.com/shigenoko/hokusai/issues/19
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient, NotionAPIError, NotionRateLimitError

logger = get_logger("notion_dashboard.identification")


def mask_db_id(db_id: str | None) -> str:
    """Notion DB ID をマスクして表示用文字列にする。

    `<先頭 8 桁>...<末尾 4 桁>` 形式。完全 ID は HTML 属性経由で持たせる想定。

    Args:
        db_id: Notion DB ID（ダッシュ有無問わず）

    Returns:
        マスク済み文字列。`None` / 短すぎる値は `(unknown)`。
    """
    if not db_id or not isinstance(db_id, str) or len(db_id) < 12:
        return "(unknown)"
    return f"{db_id[:8]}...{db_id[-4:]}"


def notion_db_url(db_id: str | None) -> str:
    """Notion DB の Web URL を生成する。

    Notion の DB URL は `https://www.notion.so/<id_without_dashes>` 形式。
    `None` / 空の場合は空文字を返す。
    """
    if not db_id or not isinstance(db_id, str):
        return ""
    return f"https://www.notion.so/{db_id.replace('-', '')}"


# ---------------------------------------------------------------------------
# Bot info の取得とキャッシュ
# ---------------------------------------------------------------------------

# process memory cache: api_token (or env name) をキーに、bot info と取得時刻を保持。
# Operations Console は常駐 dashboard で同じ profile を見続ける想定のため、
# 短い TTL でも API 呼び出しを大幅に削減できる。
#
# キャッシュ値は `dict | None`:
#   - dict: API 呼び出し成功時の戻り値
#   - None: 失敗（negative caching）。同 TTL 内は再試行せずログ抑制
#
# dashboard は HTTPServer で複数スレッドからレンダリングされ得るため、
# キャッシュアクセスは threading.Lock で保護する（thundering herd 抑止と
# 内部 dict の競合書き込み防止）。
_BOT_INFO_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_BOT_INFO_CACHE_LOCK = threading.Lock()
_BOT_INFO_CACHE_TTL_SECONDS = 300  # 5 分


def _now() -> float:
    """time.time() のラッパ（テストで monkeypatch しやすくするため）。"""
    return time.time()


def clear_bot_info_cache() -> None:
    """テスト用 / 設定変更時用にキャッシュをクリアする。"""
    with _BOT_INFO_CACHE_LOCK:
        _BOT_INFO_CACHE.clear()


def get_bot_info(
    api_token: str,
    *,
    cache_key: str | None = None,
    ttl_seconds: int = _BOT_INFO_CACHE_TTL_SECONDS,
) -> dict[str, Any] | None:
    """Notion API GET /users/me を呼んで bot info を取得（キャッシュつき）。

    並行性:
        dashboard は HTTPServer 経由で複数スレッドからレンダされ得るため、
        キャッシュ確認 → API 呼び出し → キャッシュ書き込みの全工程を
        単一 Lock 内で実行する（thundering herd 抑止）。
        Notion API call は dashboard のレンダー単位（人手操作）でしか走らず、
        シリアル化のコストは無視できる。

    Negative caching:
        Notion API 呼び出しが失敗した場合も `None` を TTL 付きで保存する。
        これにより token が無効 / Notion が停止中等の状況で、dashboard を
        開くたびに API を叩いて警告ログが連発するのを抑える。

    Args:
        api_token: Notion Internal Integration Token
        cache_key: キャッシュキー。省略時は token そのもの。Operations Console
            では env 変数名 + token のハッシュ等を使うのが安全。
        ttl_seconds: キャッシュ TTL（秒）。成功・失敗ともこの TTL で保持。

    Returns:
        Notion API のレスポンス dict。失敗時は `None`（呼び出し側で
        `(unable to fetch)` 等の degrade 表示にする想定）。
    """
    if not api_token:
        return None

    key = cache_key or api_token
    with _BOT_INFO_CACHE_LOCK:
        now = _now()
        cached = _BOT_INFO_CACHE.get(key)
        if cached is not None:
            cached_at, value = cached
            if now - cached_at < ttl_seconds:
                return value

        # キャッシュミス / 期限切れ → API 呼び出し
        bot_info: dict[str, Any] | None
        try:
            client = NotionAPIClient(api_token=api_token)
            bot_info = client.get_bot_info()
        except (NotionAPIError, NotionRateLimitError) as e:
            # 認証エラーや rate limit はパネル落とさず graceful degrade
            logger.warning(
                "Notion bot info fetch failed (%s): %s",
                type(e).__name__, str(e),
            )
            bot_info = None
        except Exception as e:
            # ネットワーク / 例外は型名のみログに残す（token 漏洩防止）
            logger.warning("Notion bot info fetch error: %s", type(e).__name__)
            bot_info = None

        # 成功・失敗ともキャッシュ（negative caching でログ抑制）
        _BOT_INFO_CACHE[key] = (now, bot_info)
        return bot_info


def get_bot_display_name(bot_info: dict[str, Any] | None) -> str:
    """bot info から表示用の名前を組み立てる。

    Args:
        bot_info: `get_bot_info()` の戻り値

    Returns:
        表示文字列。
        - `bot_info` が `None` または dict 以外 → `(unable to fetch)`（取得失敗）
        - dict だが name が無い → `(no name)`（API 応答に name が含まれない）
        - name + type=bot → `<name> (bot)`
        - name のみ → `<name>`
    """
    if bot_info is None or not isinstance(bot_info, dict):
        return "(unable to fetch)"
    name = bot_info.get("name", "")
    type_ = bot_info.get("type", "")
    if name and type_ == "bot":
        return f"{name} (bot)"
    if name:
        return name
    return "(no name)"


# ---------------------------------------------------------------------------
# 統合: panel 用 identification dict
# ---------------------------------------------------------------------------


def build_notion_identification(
    *,
    profile_name: str | None,
    api_token_env: str,
    workflows_db_id_env: str,
    pull_requests_db_id_env: str,
) -> dict[str, Any]:
    """dashboard panel で表示する identification dict を組み立てる。

    Args:
        profile_name: 現在 active な profile 名（無ければ `None`）
        api_token_env: token の env 変数名
        workflows_db_id_env: workflows DB ID の env 変数名
        pull_requests_db_id_env: PR DB ID の env 変数名

    Returns:
        {
            "profile_name": str | None,
            "api_token_env": str,
            "workflows_db_id_full": str,        # 完全 ID（HTML 属性用）
            "workflows_db_id_masked": str,
            "workflows_db_url": str,
            "pull_requests_db_id_full": str,
            "pull_requests_db_id_masked": str,
            "pull_requests_db_url": str,
            "bot_display_name": str,
        }
    """
    api_token = os.environ.get(api_token_env, "").strip()
    workflows_db_id = os.environ.get(workflows_db_id_env, "").strip()
    pull_requests_db_id = os.environ.get(pull_requests_db_id_env, "").strip()

    bot_info = get_bot_info(api_token, cache_key=api_token_env) if api_token else None

    return {
        "profile_name": profile_name,
        "api_token_env": api_token_env,
        "workflows_db_id_full": workflows_db_id,
        "workflows_db_id_masked": mask_db_id(workflows_db_id),
        "workflows_db_url": notion_db_url(workflows_db_id),
        "pull_requests_db_id_full": pull_requests_db_id,
        "pull_requests_db_id_masked": mask_db_id(pull_requests_db_id),
        "pull_requests_db_url": notion_db_url(pull_requests_db_id),
        "bot_display_name": get_bot_display_name(bot_info),
    }
