"""Phase E (v0.4.0): Figma / Miro 書き戻し用 outbox / errors / idempotency
テーブルの SQLite スキーマ検証。

対象:
- figma_sync_outbox / figma_sync_errors
- miro_sync_outbox / miro_sync_errors
- design_writeback_idempotency

詳細は docs/hokusai-figma-miro-writeback-implementation-plan.md §5 参照。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hokusai.persistence.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# テーブル存在確認
# ---------------------------------------------------------------------------


WRITEBACK_TABLES = [
    "figma_sync_outbox",
    "figma_sync_errors",
    "miro_sync_outbox",
    "miro_sync_errors",
    "design_writeback_idempotency",
]


def _table_exists(db_path: Path, table_name: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cursor.fetchone() is not None


def _get_columns(db_path: Path, table_name: str) -> dict[str, str]:
    """カラム名 → 型のマッピングを返す"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(f"PRAGMA table_info({table_name})")
        return {row[1]: row[2] for row in cursor.fetchall()}


def _index_exists(db_path: Path, index_name: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        )
        return cursor.fetchone() is not None


def test_writeback_tables_created_on_new_db(tmp_path):
    """新規 DB を開くと Phase E の 5 テーブルが全て作成される"""
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)

    for table in WRITEBACK_TABLES:
        assert _table_exists(db_path, table), f"{table} が作成されていない"


def test_writeback_indexes_created_on_new_db(tmp_path):
    """新規 DB を開くと Phase E の 9 index が全て作成される。

    errors 側の idempotency_key index は writeback の 3 段階チェック
    （is_in_errors）が O(1) で動くために必須。
    """
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)

    expected_indexes = [
        "idx_figma_outbox_workflow",
        "idx_figma_outbox_event",
        "idx_figma_errors_workflow",
        "idx_figma_errors_idempotency",
        "idx_miro_outbox_workflow",
        "idx_miro_outbox_event",
        "idx_miro_errors_workflow",
        "idx_miro_errors_idempotency",
        "idx_writeback_idempotency_workflow",
    ]
    for index in expected_indexes:
        assert _index_exists(db_path, index), f"{index} が作成されていない"


# ---------------------------------------------------------------------------
# outbox スキーマ詳細
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", ["figma_sync_outbox", "miro_sync_outbox"])
def test_outbox_has_required_columns(tmp_path, table_name):
    """outbox テーブルが計画書 §5.1 の全列を持つ"""
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)

    columns = _get_columns(db_path, table_name)
    expected = {
        "id",
        "idempotency_key",
        "workflow_id",
        "profile_name",     # v0.3.0 整合
        "event_type",
        "payload_json",
        "attempt_count",
        "last_error",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(columns.keys()), (
        f"{table_name} に不足列: {expected - columns.keys()}"
    )


@pytest.mark.parametrize("table_name", ["figma_sync_errors", "miro_sync_errors"])
def test_errors_has_required_columns(tmp_path, table_name):
    """errors テーブルが計画書 §5.1 の全列を持つ"""
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)

    columns = _get_columns(db_path, table_name)
    expected = {
        "id",
        "idempotency_key",
        "workflow_id",
        "profile_name",
        "event_type",
        "payload_json",
        "error_message",
        "failed_at",
    }
    assert expected.issubset(columns.keys()), (
        f"{table_name} に不足列: {expected - columns.keys()}"
    )


def test_idempotency_table_has_required_columns(tmp_path):
    """design_writeback_idempotency が計画書 §5.1 の全列を持つ"""
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)

    columns = _get_columns(db_path, "design_writeback_idempotency")
    expected = {
        "idempotency_key",
        "workflow_id",
        "profile_name",
        "target",        # "figma" | "miro"
        "resource",      # frame_id / board_id
        "response_id",   # 投稿成功時の comment_id / card_id
        "created_at",
    }
    assert expected.issubset(columns.keys()), (
        f"design_writeback_idempotency に不足列: {expected - columns.keys()}"
    )


# ---------------------------------------------------------------------------
# 制約検証
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", ["figma_sync_outbox", "miro_sync_outbox"])
def test_outbox_idempotency_key_unique(tmp_path, table_name):
    """outbox の idempotency_key は UNIQUE 制約付き"""
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {table_name}
                (idempotency_key, workflow_id, event_type, payload_json,
                 attempt_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("key-1", "wf-1", "phase8a_completed", "{}", 0,
             "2026-05-13", "2026-05-13"),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {table_name}
                    (idempotency_key, workflow_id, event_type, payload_json,
                     attempt_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("key-1", "wf-2", "phase8a_completed", "{}", 0,
                 "2026-05-13", "2026-05-13"),
            )


def test_idempotency_key_is_primary_key(tmp_path):
    """design_writeback_idempotency の idempotency_key は PRIMARY KEY"""
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO design_writeback_idempotency
                (idempotency_key, workflow_id, target, resource, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("key-1", "wf-1", "figma", "node-1", "2026-05-13"),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO design_writeback_idempotency
                    (idempotency_key, workflow_id, target, resource, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("key-1", "wf-2", "miro", "node-2", "2026-05-13"),
            )


# ---------------------------------------------------------------------------
# 後方互換
# ---------------------------------------------------------------------------


def test_existing_v0_3_db_opens_without_error(tmp_path):
    """v0.3.x で作成済みの DB（Phase E テーブル無し）を v0.4.0 で開いても壊れない"""
    db_path = tmp_path / "legacy.db"

    # v0.3.x 相当の DB を手動で作成（Phase E のテーブルは含まない）
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE workflows (
                workflow_id TEXT PRIMARY KEY,
                task_url TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                profile_name TEXT
            )
        """)
        conn.execute(
            "INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?)",
            ("wf-legacy", "https://example.com", "{}",
             "2026-05-12", "2026-05-12", "company-a"),
        )
        conn.commit()

    # v0.4.0 で開く（_init_db が走り、Phase E テーブルが追加される）
    SQLiteStore(db_path)

    # 既存データが残っている
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT workflow_id, profile_name FROM workflows WHERE workflow_id = ?",
            ("wf-legacy",),
        )
        row = cursor.fetchone()
        assert row == ("wf-legacy", "company-a")

    # 新規 Phase E テーブルも作成されている
    for table in WRITEBACK_TABLES:
        assert _table_exists(db_path, table), f"{table} が legacy DB で作成されていない"


def test_reopening_db_is_idempotent(tmp_path):
    """同じ DB を 2 回開いても CREATE TABLE IF NOT EXISTS で副作用なし"""
    db_path = tmp_path / "wf.db"

    SQLiteStore(db_path)
    # データを投入
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO figma_sync_outbox
                (idempotency_key, workflow_id, event_type, payload_json,
                 attempt_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("key-1", "wf-1", "phase8a_completed", "{}", 0,
             "2026-05-13", "2026-05-13"),
        )
        conn.commit()

    # 2 回目の open でデータが消えないことを確認
    SQLiteStore(db_path)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT idempotency_key FROM figma_sync_outbox WHERE workflow_id = ?",
            ("wf-1",),
        )
        assert cursor.fetchone() == ("key-1",)
