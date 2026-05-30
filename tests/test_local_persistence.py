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


# --- recurring review issue 検出 (Step 5 第4) ----------------------------


def _add_ri(store, dedupe_key, workflow_id, **kw):
    store.upsert_review_issue(
        dedupe_key=dedupe_key, workflow_id=workflow_id,
        source=kw.get("source", "verification_failure"),
        rule=kw.get("rule", "npm test"),
        file=kw.get("file"),
        message=kw.get("message", "boom"),
        repository=kw.get("repository", "Backend"),
        status=kw.get("status", "open"),
    )


def test_find_recurring_review_issues_across_workflows(store):
    # 同一 content (rule/message/repo) が 3 workflow で発生 → recurring
    _add_ri(store, "k1", "wf-1")
    _add_ri(store, "k2", "wf-2")
    _add_ri(store, "k3", "wf-3")
    # 別 content (1 workflow のみ) → recurring ではない
    _add_ri(store, "k4", "wf-1", rule="pytest", message="other")

    recurring = store.find_recurring_review_issues(min_workflows=2)
    assert len(recurring) == 1
    r = recurring[0]
    assert r["rule"] == "npm test"
    assert r["workflow_count"] == 3
    assert r["occurrence_count"] == 3
    assert r["workflow_ids"] == ["wf-1", "wf-2", "wf-3"]


def test_find_recurring_counts_distinct_workflows_not_rows(store):
    # 同一 workflow で同一 content が複数 row（別 dedupe_key）でも
    # workflow_count は 1 → min_workflows=2 では検出されない
    _add_ri(store, "k1", "wf-1")
    _add_ri(store, "k2", "wf-1", file="a.py")  # 別 file → 別 content
    _add_ri(store, "k3", "wf-1", file="b.py")
    assert store.find_recurring_review_issues(min_workflows=2) == []


def test_find_recurring_rejects_bad_args(store):
    with pytest.raises(ValueError, match="2 以上"):
        store.find_recurring_review_issues(min_workflows=1)
    with pytest.raises(ValueError, match="1 以上"):
        store.find_recurring_review_issues(limit=0)


def test_handle_graph_recurring_json(store, capsys):
    import argparse
    import json

    from hokusai.cli_main import _handle_graph

    _add_ri(store, "k1", "wf-1")
    _add_ri(store, "k2", "wf-2")

    class _Cfg:
        database_path = store.db_path

    args = argparse.Namespace(
        graph_subcommand="recurring", min_workflows=2, limit=100, output="json"
    )
    rc = _handle_graph(args, _Cfg())
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    items = payload["recurring_review_issues"]
    assert len(items) == 1
    assert items[0]["workflow_count"] == 2


def test_dedupe_key_is_not_null_enforced(store):
    """dedupe_key NULL は schema レベルで reject される (PR #147 Copilot Round 3)。

    SQLite は非 INTEGER PRIMARY KEY の NOT NULL を明示しないと NULL を許すため、
    DDL で NOT NULL を付けたことの回帰防止。NULL key の行が複数挿入できて
    idempotency が壊れるのを防ぐ。
    """
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_review_issue(dedupe_key=None, workflow_id="wf-1",
                                  source="x", message="m")
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_work_item(dedupe_key=None, workflow_id="wf-1", title="x")


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


def test_work_item_upsert_preserves_fields_on_omitted(store):
    """後続 lifecycle イベントが省略したフィールドを NULL で消さない。

    status="done" の後に status 無し (lease_release 相当) が来ても "done" を
    維持する (PR #147 Copilot Round 1 の COALESCE 回帰防止)。
    """
    store.upsert_work_item(dedupe_key="d1", workflow_id="wf-1", title="認証",
                           phase=4, status="done", description="desc")
    # lease_release 相当: status / title / description を省略
    store.upsert_work_item(dedupe_key="d1", workflow_id="wf-1")
    rows = store.list_work_items()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"       # 維持
    assert rows[0]["title"] == "認証"         # 維持
    assert rows[0]["description"] == "desc"   # 維持
    assert rows[0]["phase"] == 4              # 維持


def test_review_issue_upsert_preserves_fields_on_omitted(store):
    """review issue も省略フィールドを NULL で消さない (COALESCE)。"""
    store.upsert_review_issue(dedupe_key="k1", workflow_id="wf-1",
                              message="boom", status="open")
    store.upsert_review_issue(dedupe_key="k1", status="resolved")
    rows = store.list_review_issues()
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"
    assert rows[0]["message"] == "boom"  # 維持


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
    from hokusai.integrations.notion_dashboard.review_issues_db import (
        build_dedupe_key,
    )

    payloads = [
        {"dedupe_key": "k1", "workflow_id": "wf-1", "source": "x",
         "message": "m1", "status": "open"},
        # dedupe_key 無し → dispatch と同じ fallback で算出して永続化（skip しない）
        {"workflow_id": "wf-1", "source": "verification_failure",
         "rule": "npm test", "message": "boom", "repository": "Backend"},
        "not-a-dict",  # skip
    ]
    n = persist_review_issue_payloads(store, payloads)
    assert n == 2
    expected_fallback = build_dedupe_key(
        source="verification_failure", rule="npm test", file=None,
        message="boom", repository="Backend", workflow_id="wf-1",
    )
    keys = {r["dedupe_key"] for r in store.list_review_issues()}
    assert keys == {"k1", expected_fallback}


def test_persist_review_issue_skips_malformed(store):
    """dispatcher guard を mirror: source / message 欠落は永続化しない
    (PR #147 Copilot Round 2)。"""
    payloads = [
        {"dedupe_key": "k1", "workflow_id": "wf-1"},  # source/message 無し → skip
        {"dedupe_key": "k2", "source": "x", "workflow_id": "wf-1"},  # message 無し
        {"dedupe_key": "k3", "message": "m", "workflow_id": "wf-1"},  # source 無し
        {"dedupe_key": "k4", "source": "x", "message": "m"},  # 両方あり → 永続化
    ]
    n = persist_review_issue_payloads(store, payloads)
    assert n == 1
    assert [r["dedupe_key"] for r in store.list_review_issues()] == ["k4"]


def test_persist_work_item_payloads_computes_dedupe_key(store):
    from hokusai.integrations.notion_dashboard.work_items_db import (
        build_dedupe_key,
    )

    payloads = [
        {"workflow_id": "wf-1", "title": "認証", "phase": 4, "status": "pending"},
        {"workflow_id": "wf-1"},  # 明示 key も title も無し → skip
    ]
    n = persist_work_item_payloads(store, payloads)
    assert n == 1
    rows = store.list_work_items()
    assert len(rows) == 1
    expected_key = build_dedupe_key(workflow_id="wf-1", phase=4, title="認証")
    assert rows[0]["dedupe_key"] == expected_key
    assert rows[0]["title"] == "認証"


def test_persist_work_item_prefers_explicit_dedupe_key(store):
    """明示 dedupe_key があれば再計算せずそれを使う (Notion identity と一致)。

    PR #147 Copilot Round 2: status/claim/lease イベントが custom key を渡す
    契約に合わせる。
    """
    payloads = [
        {"dedupe_key": "custom-key", "workflow_id": "wf-1", "title": "認証",
         "phase": 5, "status": "done"},
    ]
    n = persist_work_item_payloads(store, payloads)
    assert n == 1
    rows = store.list_work_items()
    assert rows[0]["dedupe_key"] == "custom-key"  # 再計算しない


def test_persist_work_item_malformed_does_not_abort_batch(store):
    """非 str title (例: dict) の fallback でも他の有効な payload を止めない。

    PR #147 Copilot Round 4: build_dedupe_key の .strip() 例外が try の外で
    起きると drain 全体が止まるため、fallback を try 内に移しつつ str cast。
    malformed が混ざっても valid な 1 件は永続化される。
    """
    payloads = [
        {"workflow_id": "wf-1", "title": {"bad": "non-str"}, "phase": 4},
        {"workflow_id": "wf-1", "title": "正常", "phase": 4, "status": "pending"},
    ]
    n = persist_work_item_payloads(store, payloads)
    rows = store.list_work_items()
    titles = [r["title"] for r in rows]
    # valid な 1 件は必ず残り、batch 全体が落ちない
    assert "正常" in titles
    # 非 str title も str 正規化されて永続化される（transient に残らない、
    # PR #147 Copilot Round 5）。sqlite には str 化された値が入る
    assert n == 2
    assert str({"bad": "non-str"}) in titles


def test_persist_work_item_explicit_key_without_title_keeps_none(store):
    """明示 dedupe_key のみ・title 無しの event は title=None で永続化される。"""
    n = persist_work_item_payloads(
        store, [{"dedupe_key": "k1", "workflow_id": "wf-1", "status": "done"}]
    )
    assert n == 1
    rows = store.list_work_items()
    assert rows[0]["title"] is None
    assert rows[0]["status"] == "done"


def test_persist_helpers_are_best_effort_on_store_error():
    class _BoomStore:
        def upsert_review_issue(self, **kwargs):
            raise RuntimeError("db down")

        def upsert_work_item(self, **kwargs):
            raise RuntimeError("db down")

    # 例外は握りつぶし、永続化件数 0 を返す（drain 本体を止めない）。
    # review は guard を通すため source/message を持たせる。
    assert persist_review_issue_payloads(
        _BoomStore(),
        [{"dedupe_key": "k1", "source": "x", "message": "m"}]) == 0
    assert persist_work_item_payloads(
        _BoomStore(), [{"title": "x", "workflow_id": "wf-1", "phase": 4}]) == 0
