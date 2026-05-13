"""Figma / Miro 書き戻し用 outbox / errors / idempotency テーブルの操作 API。

責務:
- enqueue: 投稿失敗時に outbox に積む
- list / get: Operations Console から outbox / errors を表示
- mark_succeeded: 投稿成功時の冪等キー記録（design_writeback_idempotency に INSERT）
- move_to_errors: 5 回失敗で errors に移動
- increment_attempt: 手動再送のたびに attempt_count +1
- is_already_delivered: dispatcher 入口で 3 段階チェック

計画書 §5 / §9.2 / §10 に対応。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

# 計画書 §8.4: 5 回手動再送で errors に自動移動
MAX_ATTEMPT_COUNT = 5

# 計画書 §5.3: errors / idempotency は 30 日経過で cleanup
RETENTION_DAYS = 30


class WritebackTarget(str, Enum):
    """書き戻し先の種別"""

    FIGMA = "figma"
    MIRO = "miro"

    @property
    def outbox_table(self) -> str:
        return f"{self.value}_sync_outbox"

    @property
    def errors_table(self) -> str:
        return f"{self.value}_sync_errors"


@dataclass
class OutboxEntry:
    """outbox の 1 行を表す"""

    id: int
    idempotency_key: str
    workflow_id: str
    profile_name: str | None
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempt_count: int = 0
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple) -> OutboxEntry:
        """SQLite Row から OutboxEntry を生成"""
        return cls(
            id=row[0],
            idempotency_key=row[1],
            workflow_id=row[2],
            profile_name=row[3],
            event_type=row[4],
            payload=json.loads(row[5]) if row[5] else {},
            attempt_count=row[6],
            last_error=row[7],
            created_at=row[8],
            updated_at=row[9],
        )


class OutboxStore:
    """outbox / errors / idempotency テーブルへの SQL 操作ラッパー。

    使い方:

        store = OutboxStore(db_path, target=WritebackTarget.FIGMA)
        if not store.is_already_delivered(idempotency_key):
            try:
                response_id = api_call(...)
                store.mark_succeeded(
                    idempotency_key=..., workflow_id=..., profile_name=...,
                    resource=..., response_id=response_id,
                )
            except Exception as e:
                store.enqueue(
                    idempotency_key=..., workflow_id=..., profile_name=...,
                    event_type=..., payload={...}, error=str(e),
                )
    """

    def __init__(self, db_path: Path, target: WritebackTarget):
        self.db_path = Path(db_path)
        self.target = target

    def _connect(self) -> sqlite3.Connection:
        """既存 SQLiteStore と同じ WAL + busy_timeout の接続"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ------------------------------------------------------------------
    # 3 段階チェック（計画書 §9.2）
    # ------------------------------------------------------------------

    def is_already_delivered(self, idempotency_key: str) -> bool:
        """投稿済み（design_writeback_idempotency にヒット）か確認"""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM design_writeback_idempotency WHERE idempotency_key = ? LIMIT 1",
                (idempotency_key,),
            )
            return cursor.fetchone() is not None

    def is_pending(self, idempotency_key: str) -> bool:
        """outbox に既にある（pending 中）か確認"""
        with self._connect() as conn:
            cursor = conn.execute(
                f"SELECT 1 FROM {self.target.outbox_table} "
                "WHERE idempotency_key = ? LIMIT 1",
                (idempotency_key,),
            )
            return cursor.fetchone() is not None

    def is_in_errors(self, idempotency_key: str) -> bool:
        """errors（5 回失敗で諦め済）にあるか確認"""
        with self._connect() as conn:
            cursor = conn.execute(
                f"SELECT 1 FROM {self.target.errors_table} "
                "WHERE idempotency_key = ? LIMIT 1",
                (idempotency_key,),
            )
            return cursor.fetchone() is not None

    def should_skip(self, idempotency_key: str, *, force: bool = False) -> bool:
        """dispatcher 入口での 3 段階チェック（force=True なら errors を無視）"""
        if self.is_already_delivered(idempotency_key):
            return True
        if self.is_pending(idempotency_key):
            return True
        if not force and self.is_in_errors(idempotency_key):
            return True
        return False

    # ------------------------------------------------------------------
    # mark_succeeded: 投稿成功
    # ------------------------------------------------------------------

    def mark_succeeded(
        self,
        *,
        idempotency_key: str,
        workflow_id: str,
        profile_name: str | None,
        resource: str,
        response_id: str | None,
    ) -> None:
        """投稿成功時に design_writeback_idempotency に記録する。

        対応する outbox 行があれば削除（成功時に残さない設計、§5.3）。
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO design_writeback_idempotency
                    (idempotency_key, workflow_id, profile_name, target,
                     resource, response_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (idempotency_key, workflow_id, profile_name,
                 self.target.value, resource, response_id, now),
            )
            conn.execute(
                f"DELETE FROM {self.target.outbox_table} WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # enqueue: 投稿失敗
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        idempotency_key: str,
        workflow_id: str,
        profile_name: str | None,
        event_type: str,
        payload: dict[str, Any],
        error: str,
    ) -> int:
        """投稿失敗時に outbox に積む。既存行があれば last_error / updated_at を更新。

        Returns:
            outbox の id（既存行の場合も）
        """
        now = datetime.now().isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        with self._connect() as conn:
            # 同じ idempotency_key で再 enqueue する場合、最新の payload / メタ情報も
            # 更新する。Operations Console や手動再送で stale な内容を参照しないため。
            # attempt_count はリセットせず保持（再送回数の累積を維持）。
            cursor = conn.execute(
                f"""
                INSERT INTO {self.target.outbox_table}
                    (idempotency_key, workflow_id, profile_name, event_type,
                     payload_json, attempt_count, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    profile_name = excluded.profile_name,
                    event_type = excluded.event_type,
                    payload_json = excluded.payload_json,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (idempotency_key, workflow_id, profile_name, event_type,
                 payload_json, error, now, now),
            )
            conn.commit()
            row_id_cursor = conn.execute(
                f"SELECT id FROM {self.target.outbox_table} WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = row_id_cursor.fetchone()
            return row[0] if row else cursor.lastrowid

    # ------------------------------------------------------------------
    # 一覧 / 詳細
    # ------------------------------------------------------------------

    def list_outbox(self, *, limit: int = 100,
                    profile_name: str | None = None) -> list[OutboxEntry]:
        """outbox を更新時刻の降順で取得（最大 limit 件）"""
        query = f"""
            SELECT id, idempotency_key, workflow_id, profile_name, event_type,
                   payload_json, attempt_count, last_error, created_at, updated_at
            FROM {self.target.outbox_table}
        """
        params: tuple[Any, ...] = ()
        if profile_name is not None:
            query += " WHERE profile_name = ?"
            params = (profile_name,)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params = params + (limit,)

        with self._connect() as conn:
            cursor = conn.execute(query, params)
            return [OutboxEntry.from_row(row) for row in cursor.fetchall()]

    def list_errors(self, *, limit: int = 100,
                    profile_name: str | None = None) -> list[dict[str, Any]]:
        """errors を失敗時刻の降順で取得"""
        query = f"""
            SELECT id, idempotency_key, workflow_id, profile_name, event_type,
                   payload_json, error_message, failed_at
            FROM {self.target.errors_table}
        """
        params: tuple[Any, ...] = ()
        if profile_name is not None:
            query += " WHERE profile_name = ?"
            params = (profile_name,)
        query += " ORDER BY failed_at DESC LIMIT ?"
        params = params + (limit,)

        with self._connect() as conn:
            cursor = conn.execute(query, params)
            return [
                {
                    "id": row[0],
                    "idempotency_key": row[1],
                    "workflow_id": row[2],
                    "profile_name": row[3],
                    "event_type": row[4],
                    "payload": json.loads(row[5]) if row[5] else {},
                    "error_message": row[6],
                    "failed_at": row[7],
                }
                for row in cursor.fetchall()
            ]

    def get_outbox(self, outbox_id: int) -> OutboxEntry | None:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                SELECT id, idempotency_key, workflow_id, profile_name, event_type,
                       payload_json, attempt_count, last_error, created_at, updated_at
                FROM {self.target.outbox_table}
                WHERE id = ?
                """,
                (outbox_id,),
            )
            row = cursor.fetchone()
            return OutboxEntry.from_row(row) if row else None

    # ------------------------------------------------------------------
    # increment / move_to_errors（手動再送のたびに）
    # ------------------------------------------------------------------

    def increment_attempt(self, outbox_id: int, *,
                          error: str | None = None) -> int:
        """手動再送のたびに +1。新しい attempt_count を返す。"""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {self.target.outbox_table}
                SET attempt_count = attempt_count + 1,
                    last_error = COALESCE(?, last_error),
                    updated_at = ?
                WHERE id = ?
                """,
                (error, now, outbox_id),
            )
            conn.commit()
            cursor = conn.execute(
                f"SELECT attempt_count FROM {self.target.outbox_table} WHERE id = ?",
                (outbox_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    def move_to_errors(self, outbox_id: int, *,
                       error: str | None = None) -> bool:
        """outbox 行を errors に移動する。

        attempt_count >= MAX_ATTEMPT_COUNT のときに呼ばれる。
        手動で `move-to-errors` API から呼ばれることもある。

        Returns:
            移動が成功したか（既に削除済みなら False）
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                SELECT idempotency_key, workflow_id, profile_name, event_type,
                       payload_json, last_error
                FROM {self.target.outbox_table}
                WHERE id = ?
                """,
                (outbox_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return False

            error_msg = error if error is not None else (row[5] or "moved to errors")
            conn.execute(
                f"""
                INSERT INTO {self.target.errors_table}
                    (idempotency_key, workflow_id, profile_name, event_type,
                     payload_json, error_message, failed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (row[0], row[1], row[2], row[3], row[4], error_msg, now),
            )
            conn.execute(
                f"DELETE FROM {self.target.outbox_table} WHERE id = ?",
                (outbox_id,),
            )
            conn.commit()
            return True

    # ------------------------------------------------------------------
    # cleanup（30 日経過削除）
    # ------------------------------------------------------------------

    def cleanup_old_errors(self, retention_days: int = RETENTION_DAYS) -> int:
        """retention_days を超える errors / idempotency を削除する。

        Returns:
            削除した行数（errors + idempotency 合計）
        """
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            cursor1 = conn.execute(
                f"DELETE FROM {self.target.errors_table} WHERE failed_at < ?",
                (cutoff,),
            )
            cursor2 = conn.execute(
                """
                DELETE FROM design_writeback_idempotency
                WHERE target = ? AND created_at < ?
                """,
                (self.target.value, cutoff),
            )
            conn.commit()
            return cursor1.rowcount + cursor2.rowcount
