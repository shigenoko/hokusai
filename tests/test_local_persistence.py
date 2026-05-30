"""Review Issue / Work Item ローカル永続化 (Step 5 第3スライス) のテスト。

SQLiteStore の upsert/list/冪等/cascade と、drain payload → store への
永続化ヘルパ (persist_*_payloads) を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.local_persistence import (
    persist_review_issue_payloads,
    persist_work_item_payloads,
)
from hokusai.persistence import SQLiteStore


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / "wf.db")


# --- review_issues -------------------------------------------------------


def test_upsert_and_list_review_issue(store):
    store.upsert_review_issue(
        dedupe_key="k1", workflow_id="wf-1", source="verification_failure",
        rule="npm test", message="boom", repository="Backend",
        severity="high", status="open",
    )
    rows = store.list_review_issues()
    assert len(rows) == 1
    r = rows[0]
    assert r["dedupe_key"] == "k1"
    assert r["workflow_id"] == "wf-1"
    assert r["status"] == "open"
    assert r["repository"] == "Backend"


def test_review_issue_upsert_is_idempotent_and_updates_status(store):
    store.upsert_review_issue(dedupe_key="k1", workflow_id="wf-1", status="open")
    store.upsert_review_issue(
        dedupe_key="k1", workflow_id="wf-1", status="resolved"
    )
    rows = store.list_review_issues()
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"


def test_list_review_issues_filters(store):
    store.upsert_review_issue(dedupe_key="k1", workflow_id="wf-1", status="open")
    store.upsert_review_issue(
        dedupe_key="k2", workflow_id="wf-1", status="resolved"
    )
    store.upsert_review_issue(dedupe_key="k3", workflow_id="wf-2", status="open")
    assert len(store.list_review_issues(workflow_id="wf-1")) == 2
    assert len(store.list_review_issues(status="open")) == 2
    assert len(store.list_review_issues(workflow_id="wf-1", status="open")) == 1


def test_list_review_issues_rejects_bad_limit(store):
    with pytest.raises(ValueError, match="1 以上"):
        store.list_review_issues(limit=0)


# --- work_items ----------------------------------------------------------


def test_upsert_and_list_work_item(store):
    store.upsert_work_item(
        dedupe_key="d1", workflow_id="wf-1", title="認証を実装",
        phase=4, status="pending", description="src line",
    )
    rows = store.list_work_items()
    assert len(rows) == 1
    assert rows[0]["title"] == "認証を実装"
    assert rows[0]["phase"] == 4


def test_work_item_upsert_updates_status(store):
    store.upsert_work_item(dedupe_key="d1", workflow_id="wf-1", title="x",
                           phase=4, status="pending")
    store.upsert_work_item(dedupe_key="d1", workflow_id="wf-1", title="x",
                           phase=5, status="done")
    rows = store.list_work_items()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["phase"] == 5


# --- cascade -------------------------------------------------------------


def test_review_issues_and_work_items_cascade_on_gc(tmp_path):
    from datetime import datetime, timedelta

    store = SQLiteStore(tmp_path / "wf.db")
    old_ts = (datetime.now() - timedelta(days=120)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO workflows (workflow_id, task_url, task_title, "
            "branch_name, current_phase, state_json, created_at, updated_at, "
            "profile_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wf-old", "u", "t", "b", 10, "{}", old_ts, old_ts, None),
        )
        conn.commit()
    store.upsert_review_issue(dedupe_key="k1", workflow_id="wf-old",
                              status="open")
    store.upsert_work_item(dedupe_key="d1", workflow_id="wf-old", title="x")

    counts = store.delete_old_completed_workflows(retention_days=90)
    assert counts["review_issues"] == 1
    assert counts["work_items"] == 1
    assert store.list_review_issues(workflow_id="wf-old") == []
    assert store.list_work_items(workflow_id="wf-old") == []


# --- persist helpers (drain payload → store) -----------------------------


def test_persist_review_issue_payloads(store):
    payloads = [
        {"dedupe_key": "k1", "workflow_id": "wf-1", "source": "x",
         "message": "m1", "status": "open"},
        {"workflow_id": "wf-1", "message": "no dedupe key"},  # skip
        "not-a-dict",  # skip
    ]
    n = persist_review_issue_payloads(store, payloads)
    assert n == 1
    rows = store.list_review_issues()
    assert [r["dedupe_key"] for r in rows] == ["k1"]


def test_persist_work_item_payloads_computes_dedupe_key(store):
    from hokusai.integrations.notion_dashboard.work_items_db import (
        build_dedupe_key,
    )

    payloads = [
        {"workflow_id": "wf-1", "title": "認証", "phase": 4, "status": "pending"},
        {"workflow_id": "wf-1"},  # title 無し → skip
    ]
    n = persist_work_item_payloads(store, payloads)
    assert n == 1
    rows = store.list_work_items()
    assert len(rows) == 1
    expected_key = build_dedupe_key(workflow_id="wf-1", phase=4, title="認証")
    assert rows[0]["dedupe_key"] == expected_key
    assert rows[0]["title"] == "認証"


def test_persist_helpers_are_best_effort_on_store_error():
    class _BoomStore:
        def upsert_review_issue(self, **kwargs):
            raise RuntimeError("db down")

        def upsert_work_item(self, **kwargs):
            raise RuntimeError("db down")

    # 例外は握りつぶし、永続化件数 0 を返す（drain 本体を止めない）
    assert persist_review_issue_payloads(
        _BoomStore(), [{"dedupe_key": "k1"}]) == 0
    assert persist_work_item_payloads(
        _BoomStore(), [{"title": "x", "workflow_id": "wf-1", "phase": 4}]) == 0
