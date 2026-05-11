"""
Figma / Miro レスポンスのキャッシュ層。

SQLiteStore の `figma_file_cache` / `miro_board_cache` をラップし、
TTL と cache_key 規約を一箇所に集約する。

cache_key 規約:
- figma:  "figma:<file_key>:<node_id_or_root>"
- miro:   "miro:<board_id>"

設計方針:
- TTL を超えたものは get で None を返す（呼び出し側で API 叩く）。
- put では `expires_at = now + ttl_seconds` を計算して保存する。
- 例外を投げない（キャッシュ層の失敗で本処理を止めない）。
  失敗時は logger.warning で記録するのみ。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ...logging_config import get_logger
from ...persistence.sqlite_store import SQLiteStore

logger = get_logger("integrations.design.cache")


def _figma_cache_key(file_key: str, node_id: str | None) -> str:
    return f"figma:{file_key}:{node_id or 'root'}"


def _miro_cache_key(board_id: str) -> str:
    return f"miro:{board_id}"


def _expires_at(ttl_seconds: int) -> str:
    """SQLiteStore 側の比較は naive local の `datetime.now().isoformat()` を使うため、
    expires_at も同じ表記で保存する（タイムゾーン不整合で永久 expired にしないため）。"""
    return (datetime.now() + timedelta(seconds=ttl_seconds)).isoformat()


class DesignCache:
    """Figma / Miro 用 SQLite キャッシュ。

    TTL は config から渡し、本クラスは保存と取得のみ責任を持つ。
    """

    def __init__(self, store: SQLiteStore | None = None):
        # store 未指定時は WorkflowConfig.database_path を使って SQLiteStore を
        # 生成する。SQLiteStore() の素のデフォルト（~/.hokusai/workflow.db）に
        # 落とすと、database_path をカスタムした環境で design キャッシュだけ
        # 別 DB に書かれてしまい、ダッシュボード経由の clear_*_cache() が
        # 効かなくなる問題を回避する。
        if store is None:
            try:
                from ...config import get_config

                cfg = get_config()
                store = SQLiteStore(cfg.database_path)
            except Exception as exc:  # config 読み込み失敗時はデフォルトで継続
                logger.warning(
                    "design cache: config 取得失敗のためデフォルト DB を使用: %s", exc
                )
                store = SQLiteStore()
        self._store = store

    # ------- Figma -------

    def get_figma(self, file_key: str, node_id: str | None) -> dict[str, Any] | None:
        cache_key = _figma_cache_key(file_key, node_id)
        try:
            row = self._store.get_figma_cache(cache_key)
        except Exception as exc:
            logger.warning("figma cache get failed (cache_key=%s): %s", cache_key, exc)
            return None
        if row:
            logger.debug("figma cache hit (cache_key=%s)", cache_key)
            return row["payload"]
        logger.debug("figma cache miss (cache_key=%s)", cache_key)
        return None

    def put_figma(
        self,
        file_key: str,
        node_id: str | None,
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        cache_key = _figma_cache_key(file_key, node_id)
        try:
            self._store.put_figma_cache(
                cache_key=cache_key,
                file_key=file_key,
                node_id=node_id,
                payload=payload,
                expires_at=_expires_at(ttl_seconds),
            )
        except Exception as exc:
            logger.warning("figma cache put failed (cache_key=%s): %s", cache_key, exc)

    # ------- Miro -------

    def get_miro(self, board_id: str) -> dict[str, Any] | None:
        cache_key = _miro_cache_key(board_id)
        try:
            row = self._store.get_miro_cache(cache_key)
        except Exception as exc:
            logger.warning("miro cache get failed (cache_key=%s): %s", cache_key, exc)
            return None
        if row:
            logger.debug("miro cache hit (cache_key=%s)", cache_key)
            return row["payload"]
        logger.debug("miro cache miss (cache_key=%s)", cache_key)
        return None

    def put_miro(
        self,
        board_id: str,
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        cache_key = _miro_cache_key(board_id)
        try:
            self._store.put_miro_cache(
                cache_key=cache_key,
                board_id=board_id,
                payload=payload,
                expires_at=_expires_at(ttl_seconds),
            )
        except Exception as exc:
            logger.warning("miro cache put failed (cache_key=%s): %s", cache_key, exc)
