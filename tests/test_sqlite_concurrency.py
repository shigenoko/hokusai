"""
SQLite 並行アクセステスト

WAL モード + busy_timeout が正しく設定され、
並行書き込みで DATABASE IS LOCKED が発生しないことを検証する。
"""

import os
import sqlite3
import threading
import tempfile
from pathlib import Path

import pytest

from hokusai.persistence.sqlite_store import SQLiteStore


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    """テスト用 DB パス"""
    return tmp_path / "test_concurrent.db"


class TestWALMode:
    """WAL モードが有効になっていることの検証"""

    def test_wal_mode_enabled(self, store_path: Path):
        """SQLiteStore 接続で WAL モードが有効になる"""
        store = SQLiteStore(store_path)
        with store._connect() as conn:
            result = conn.execute("PRAGMA journal_mode").fetchone()
            assert result[0] == "wal"

    def test_busy_timeout_set(self, store_path: Path):
        """busy_timeout が設定されている"""
        store = SQLiteStore(store_path)
        with store._connect() as conn:
            result = conn.execute("PRAGMA busy_timeout").fetchone()
            assert result[0] == 5000


class TestConcurrentAccess:
    """並行アクセスのテスト"""

    def test_concurrent_save_and_load(self, store_path: Path):
        """2スレッドから同時に save/load しても LOCKED エラーが出ない"""
        store = SQLiteStore(store_path)
        errors = []
        iterations = 50

        def writer(thread_id: int):
            try:
                for i in range(iterations):
                    state = {
                        "workflow_id": f"wf-thread{thread_id}",
                        "task_url": f"https://example.com/task-{thread_id}",
                        "task_title": f"Task {thread_id}-{i}",
                        "current_phase": 1,
                        "branch_name": f"feature/thread-{thread_id}",
                    }
                    store.save_workflow(f"wf-thread{thread_id}", state)
            except Exception as e:
                errors.append((thread_id, str(e)))

        def reader(thread_id: int):
            try:
                for i in range(iterations):
                    store.load_workflow(f"wf-thread{thread_id}")
            except Exception as e:
                errors.append((thread_id, str(e)))

        threads = []
        for tid in range(4):
            t_write = threading.Thread(target=writer, args=(tid,))
            t_read = threading.Thread(target=reader, args=(tid,))
            threads.extend([t_write, t_read])

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"並行アクセスでエラー発生: {errors}"

    def test_concurrent_audit_logs(self, store_path: Path):
        """複数スレッドから同時に audit_log を追加しても問題ない"""
        store = SQLiteStore(store_path)

        # ワークフローを事前作成
        state = {
            "workflow_id": "wf-audit-test",
            "task_url": "https://example.com/audit",
            "current_phase": 1,
        }
        store.save_workflow("wf-audit-test", state)

        errors = []

        def add_logs(thread_id: int):
            try:
                for i in range(20):
                    store.add_audit_log(
                        "wf-audit-test",
                        phase=thread_id,
                        action=f"action-{i}",
                        status="success",
                        details={"thread": thread_id, "iteration": i},
                    )
            except Exception as e:
                errors.append((thread_id, str(e)))

        threads = [threading.Thread(target=add_logs, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"audit_log 並行追加でエラー: {errors}"

        # 全ログが保存されていること
        logs = store.get_audit_logs("wf-audit-test")
        assert len(logs) == 80  # 4 threads * 20 iterations
