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
        self._store = store or SQLiteStore()

    # ------- Figma -------

    def get_figma(self, file_key: str, node_id: str | None) -> dict[str, Any] | None:
        try:
            row = self._store.get_figma_cache(_figma_cache_key(file_key, node_id))
        except Exception as exc:
            logger.warning("figma cache get failed (key=%s): %s", file_key, exc)
            return None
        return row["payload"] if row else None

    def put_figma(
        self,
        file_key: str,
        node_id: str | None,
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        try:
            self._store.put_figma_cache(
                cache_key=_figma_cache_key(file_key, node_id),
                file_key=file_key,
                node_id=node_id,
                payload=payload,
                expires_at=_expires_at(ttl_seconds),
            )
        except Exception as exc:
            logger.warning("figma cache put failed (key=%s): %s", file_key, exc)

    # ------- Miro -------

    def get_miro(self, board_id: str) -> dict[str, Any] | None:
        try:
            row = self._store.get_miro_cache(_miro_cache_key(board_id))
        except Exception as exc:
            logger.warning("miro cache get failed (key=%s): %s", board_id, exc)
            return None
        return row["payload"] if row else None

    def put_miro(
        self,
        board_id: str,
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        try:
            self._store.put_miro_cache(
                cache_key=_miro_cache_key(board_id),
                board_id=board_id,
                payload=payload,
                expires_at=_expires_at(ttl_seconds),
            )
        except Exception as exc:
            logger.warning("miro cache put failed (key=%s): %s", board_id, exc)
