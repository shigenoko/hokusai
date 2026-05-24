"""M2.5 (#100): SQLiteStore.delete_old_completed_workflows と CLI --gc-workflows

`hokusai cleanup --gc-workflows` で完了済み workflow を cascade 削除する経路
の単体テスト。SQLiteStore レベルで cascade 削除と保持期間ロジックを検証し、
CLI helper `_gc_old_workflows` で summary 出力を検証する。
"""

from __future__ import annotations

import io
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.persistence.sqlite_store import SQLiteStore


def _seed_workflow(
    store: SQLiteStore,
    *,
    workflow_id: str,
    current_phase: int,
    updated_at: str,
    profile_name: str = "test",
) -> None:
    """テスト用 workflow row を直接 SQL で投入（save_workflow は updated_at を
    now() で上書きするため、過去日付の seed には使えない）."""
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO workflows (
                workflow_id, task_url, task_title, branch_name,
                current_phase, state_json, created_at, updated_at,
                profile_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                "https://example.com/issues/1",
                "test workflow",
                "test-branch",
                current_phase,
                "{}",
                updated_at,
                updated_at,
                profile_name,
            ),
        )
        conn.commit()


def _seed_dependent_row(
    store: SQLiteStore, table: str, workflow_id: str
) -> None:
    """dependent table に該当 workflow_id の row を 1 件挿入する helper."""
    now = datetime.now().isoformat()
    with store._connect() as conn:
        if table == "checkpoints":
            conn.execute(
                "INSERT INTO checkpoints (workflow_id, phase, state_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (workflow_id, 1, "{}", now),
            )
        elif table == "audit_logs":
            conn.execute(
                "INSERT INTO audit_logs "
                "(workflow_id, phase, action, status, details_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (workflow_id, 1, "test_action", "ok", None, now),
            )
        elif table == "notion_sync_outbox":
            conn.execute(
                "INSERT INTO notion_sync_outbox "
                "(idempotency_key, workflow_id, event_type, payload_json, "
                "attempts, last_error, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{workflow_id}:test", workflow_id, "test_event", "{}",
                    0, None, now, now,
                ),
            )
        elif table == "notion_sync_errors":
            conn.execute(
                "INSERT INTO notion_sync_errors "
                "(idempotency_key, workflow_id, event_type, payload_json, "
                "error, attempts, failed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{workflow_id}:err", workflow_id, "test_event", "{}",
                    "failed", 3, now,
                ),
            )
        conn.commit()


def _make_store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "workflow.db")


# ---------------------------------------------------------------------------
# delete_old_completed_workflows: 基本動作
# ---------------------------------------------------------------------------


def test_delete_old_completed_workflows_removes_only_completed_old(tmp_path):
    """current_phase >= 10 + updated_at が retention より古い workflow だけ削除."""
    store = _make_store(tmp_path)
    now = datetime.now()
    old_ts = (now - timedelta(days=100)).isoformat()
    recent_ts = (now - timedelta(days=10)).isoformat()

    # 削除対象: completed + old
    _seed_workflow(store, workflow_id="wf-old-done", current_phase=10, updated_at=old_ts)
    # 残るべき: completed だが recent
    _seed_workflow(store, workflow_id="wf-recent-done", current_phase=10, updated_at=recent_ts)
    # 残るべき: 進行中（old だが phase < 10）
    _seed_workflow(store, workflow_id="wf-old-running", current_phase=5, updated_at=old_ts)
    # 残るべき: 進行中 + recent
    _seed_workflow(store, workflow_id="wf-recent-running", current_phase=3, updated_at=recent_ts)

    counts = store.delete_old_completed_workflows(retention_days=90)

    assert counts["workflows"] == 1
    # 削除対象だけ消えている
    assert store.load_workflow("wf-old-done") is None
    assert store.load_workflow("wf-recent-done") is not None
    assert store.load_workflow("wf-old-running") is not None
    assert store.load_workflow("wf-recent-running") is not None


def test_delete_old_completed_workflows_returns_zero_when_none_match(tmp_path):
    """対象 0 件のときは counts が全 0 で workflows row も無変化."""
    store = _make_store(tmp_path)
    recent_ts = (datetime.now() - timedelta(days=10)).isoformat()
    _seed_workflow(store, workflow_id="wf-recent", current_phase=10, updated_at=recent_ts)

    counts = store.delete_old_completed_workflows(retention_days=90)

    assert counts["workflows"] == 0
    assert store.load_workflow("wf-recent") is not None


def test_delete_old_completed_workflows_rejects_negative_retention(tmp_path):
    """retention_days < 1 は ValueError を投げる（保持期間最小 1 日強制）."""
    store = _make_store(tmp_path)

    with pytest.raises(ValueError, match="retention_days must be >= 1"):
        store.delete_old_completed_workflows(retention_days=0)
    with pytest.raises(ValueError, match="retention_days must be >= 1"):
        store.delete_old_completed_workflows(retention_days=-5)


# ---------------------------------------------------------------------------
# delete_old_completed_workflows: cascade 削除
# ---------------------------------------------------------------------------


def test_delete_old_completed_workflows_cascades_dependent_tables(tmp_path):
    """依存テーブル (checkpoints / audit_logs / notion_sync_outbox / errors)
    の workflow_id 一致行も同時に削除される."""
    store = _make_store(tmp_path)
    old_ts = (datetime.now() - timedelta(days=100)).isoformat()

    # 削除対象 workflow + 依存 row
    _seed_workflow(store, workflow_id="wf-target", current_phase=10, updated_at=old_ts)
    for table in (
        "checkpoints", "audit_logs",
        "notion_sync_outbox", "notion_sync_errors",
    ):
        _seed_dependent_row(store, table, "wf-target")

    # 残るべき workflow + 依存 row（recent なので残る）
    recent_ts = (datetime.now() - timedelta(days=5)).isoformat()
    _seed_workflow(store, workflow_id="wf-keep", current_phase=10, updated_at=recent_ts)
    for table in (
        "checkpoints", "audit_logs",
        "notion_sync_outbox", "notion_sync_errors",
    ):
        _seed_dependent_row(store, table, "wf-keep")

    counts = store.delete_old_completed_workflows(retention_days=90)

    assert counts["workflows"] == 1
    assert counts["checkpoints"] == 1
    assert counts["audit_logs"] == 1
    assert counts["notion_sync_outbox"] == 1
    assert counts["notion_sync_errors"] == 1

    # wf-target 関連が全て消え、wf-keep 関連は残る
    with store._connect() as conn:
        for table in (
            "checkpoints", "audit_logs",
            "notion_sync_outbox", "notion_sync_errors",
        ):
            target_rows = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE workflow_id = ?",
                ("wf-target",),
            ).fetchone()[0]
            keep_rows = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE workflow_id = ?",
                ("wf-keep",),
            ).fetchone()[0]
            assert target_rows == 0, f"wf-target should be deleted from {table}"
            assert keep_rows == 1, f"wf-keep should remain in {table}"


def test_delete_old_completed_workflows_skips_missing_legacy_tables(
    tmp_path, monkeypatch
):
    """レガシー DB で dependent table が存在しないケースでも例外を投げず、
    counts は 0 として返る（v0.3.x 後方互換）."""
    store = _make_store(tmp_path)
    old_ts = (datetime.now() - timedelta(days=100)).isoformat()
    _seed_workflow(store, workflow_id="wf-legacy", current_phase=10, updated_at=old_ts)

    # design_writeback_idempotency / miro_sync_outbox 等を物理削除して
    # レガシー DB の状態を模擬
    with store._connect() as conn:
        for legacy_table in (
            "design_writeback_idempotency",
            "miro_sync_outbox", "miro_sync_errors",
            "figma_sync_outbox", "figma_sync_errors",
        ):
            try:
                conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
            except sqlite3.OperationalError:
                pass
        conn.commit()

    # 例外を投げず通常完了する
    counts = store.delete_old_completed_workflows(retention_days=90)
    assert counts["workflows"] == 1
    # 不在テーブルは 0 件として記録される
    for legacy_table in (
        "design_writeback_idempotency",
        "miro_sync_outbox", "miro_sync_errors",
        "figma_sync_outbox", "figma_sync_errors",
    ):
        assert counts[legacy_table] == 0


# ---------------------------------------------------------------------------
# CLI helper: _gc_old_workflows の summary 出力
# ---------------------------------------------------------------------------


def test_gc_old_workflows_prints_zero_message_when_no_targets(tmp_path):
    """削除対象 0 件のとき「削除対象 0 件」メッセージのみ出力."""
    from hokusai.cli_main import _gc_old_workflows

    store = _make_store(tmp_path)
    recent_ts = (datetime.now() - timedelta(days=5)).isoformat()
    _seed_workflow(store, workflow_id="wf-recent", current_phase=10, updated_at=recent_ts)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _gc_old_workflows(store, retention_days=90)
    out = buf.getvalue()

    assert "削除対象 0 件" in out
    assert "🧹" not in out  # 削除アイコンは出ない


def test_gc_old_workflows_prints_summary_with_cascade_detail(tmp_path):
    """削除発生時は workflows 件数 + cascade 内訳を出力."""
    from hokusai.cli_main import _gc_old_workflows

    store = _make_store(tmp_path)
    old_ts = (datetime.now() - timedelta(days=100)).isoformat()
    _seed_workflow(store, workflow_id="wf-target", current_phase=10, updated_at=old_ts)
    _seed_dependent_row(store, "checkpoints", "wf-target")
    _seed_dependent_row(store, "audit_logs", "wf-target")

    buf = io.StringIO()
    with redirect_stdout(buf):
        _gc_old_workflows(store, retention_days=90)
    out = buf.getvalue()

    assert "🧹 workflow GC: 1 件" in out
    assert "retention: 90 日" in out
    # cascade 内訳
    assert "checkpoints=1" in out
    assert "audit_logs=1" in out


def test_gc_old_workflows_respects_custom_retention_days(tmp_path):
    """--retention-days を 30 にすると 30 日以上前の completed が削除対象."""
    from hokusai.cli_main import _gc_old_workflows

    store = _make_store(tmp_path)
    # 40 日前 → 削除対象
    ts_40 = (datetime.now() - timedelta(days=40)).isoformat()
    # 20 日前 → 残る
    ts_20 = (datetime.now() - timedelta(days=20)).isoformat()
    _seed_workflow(store, workflow_id="wf-40d", current_phase=10, updated_at=ts_40)
    _seed_workflow(store, workflow_id="wf-20d", current_phase=10, updated_at=ts_20)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _gc_old_workflows(store, retention_days=30)
    out = buf.getvalue()

    assert "🧹 workflow GC: 1 件" in out
    assert store.load_workflow("wf-40d") is None
    assert store.load_workflow("wf-20d") is not None
