"""hokusai.health.compute_runtime_health の単体テスト (Step 2 共通 handler)

CLI `profile doctor --deep` と Operations Console が共有する runtime ヘルス
集約関数を検証する。SQLite-backed・live Notion 呼び出しなし。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hokusai.health import compute_runtime_health
from hokusai.persistence.sqlite_store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "wf.db")


def test_clean_store_returns_zero_health(store: SQLiteStore):
    h = compute_runtime_health(store, llm_gateway_enabled=False)
    assert h["ran"] is True
    assert h["outbox_pending"] == 0
    assert h["outbox_errors"] == 0
    assert h["gaps"] == []
    assert h["error"] is None


def test_outbox_pending_surfaces_count_and_gap(store: SQLiteStore):
    store.enqueue_notion_sync(
        idempotency_key="k1", workflow_id="wf-1",
        event_type="workflow_started", payload={},
    )
    h = compute_runtime_health(store, llm_gateway_enabled=False)
    assert h["outbox_pending"] == 1
    kinds = [g["kind"] for g in h["gaps"]]
    assert "notion_outbox_pending" in kinds
    # gap は {kind, detail} 構造
    assert all({"kind", "detail"} <= set(g) for g in h["gaps"])


def test_persistent_errors_counted(store: SQLiteStore):
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-x:workflow_started",
        workflow_id="wf-x",
        event_type="workflow_started",
        payload={},
        error="404",
    )
    h = compute_runtime_health(store, llm_gateway_enabled=False)
    assert h["outbox_errors"] == 1


def test_audit_silence_gap_gated_by_gateway_flag(store: SQLiteStore):
    # gateway 無効なら audit_log_silence は出ない
    h_off = compute_runtime_health(store, llm_gateway_enabled=False)
    assert "audit_log_silence" not in [g["kind"] for g in h_off["gaps"]]
    # gateway 有効 + audit 空なら出る
    h_on = compute_runtime_health(store, llm_gateway_enabled=True)
    assert "audit_log_silence" in [g["kind"] for g in h_on["gaps"]]


def test_store_exception_is_best_effort(store: SQLiteStore):
    """store 呼び出しで例外が出ても error に記録して dict を返す (raise しない)"""

    class _BrokenStore:
        def count_notion_sync_pending(self):
            raise RuntimeError("simulated outage")

    h = compute_runtime_health(_BrokenStore(), llm_gateway_enabled=False)
    assert h["ran"] is False
    assert h["error"] is not None
    assert "simulated outage" in h["error"]


def test_schema_matches_doctor_json_runtime_health(store: SQLiteStore):
    """返り値のキー集合が profile doctor --output json の runtime_health と一致"""
    h = compute_runtime_health(store, llm_gateway_enabled=False)
    assert set(h.keys()) == {
        "ran", "outbox_pending", "outbox_errors", "gaps", "error"
    }
