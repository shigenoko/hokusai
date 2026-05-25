"""Issue #109 / A. fail-fast モードの単体テスト

`docs/dogfooding-findings.md` §3.1: workflow_started が永続失敗環境では
後続子イベントが outbox 膨張するため、opt-in な fail-fast モードを追加。

検証ポイント:
1. SQLiteStore.has_failed_workflow_started の判定
2. SQLiteStore.record_permanent_notion_sync_failure の直送
3. dispatcher _enqueue_failure で flag × workflow_started errors 有無の組み合わせ
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# isort: off
from hokusai.persistence.sqlite_store import SQLiteStore  # noqa: E402
# isort: on


# --- SQLiteStore helper の単体テスト ---


def test_has_failed_workflow_started_returns_false_for_clean_state(tmp_path):
    """`notion_sync_errors` が空なら False"""
    store = SQLiteStore(tmp_path / "wf.db")
    assert store.has_failed_workflow_started("wf-test") is False


def test_has_failed_workflow_started_returns_true_when_errors_present(tmp_path):
    """errors テーブルに workflow_started 行があれば True"""
    store = SQLiteStore(tmp_path / "wf.db")
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-test:workflow_started:0:0",
        workflow_id="wf-test",
        event_type="workflow_started",
        payload={"workflow_id": "wf-test"},
        error="404 share missing",
    )
    assert store.has_failed_workflow_started("wf-test") is True


def test_has_failed_workflow_started_other_event_type_does_not_count(tmp_path):
    """同じ workflow_id でも別 event_type が errors に入っていても False"""
    store = SQLiteStore(tmp_path / "wf.db")
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-test:pr_created:0:0",
        workflow_id="wf-test",
        event_type="pr_created",
        payload={"workflow_id": "wf-test"},
        error="boom",
    )
    # workflow_started 自体は errors に無いので False
    assert store.has_failed_workflow_started("wf-test") is False


def test_has_failed_workflow_started_isolates_workflows(tmp_path):
    """別 workflow_id の workflow_started 失敗には影響されない"""
    store = SQLiteStore(tmp_path / "wf.db")
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-other:workflow_started:0:0",
        workflow_id="wf-other",
        event_type="workflow_started",
        payload={"workflow_id": "wf-other"},
        error="boom",
    )
    assert store.has_failed_workflow_started("wf-target") is False
    assert store.has_failed_workflow_started("wf-other") is True


def test_record_permanent_notion_sync_failure_handles_non_json_types(tmp_path):
    """payload に datetime 等の JSON 非対応型が混ざっても落ちない（Round 2 指摘）.

    enqueue_notion_sync と同じく `default=str` でフォールバックする方針に揃え、
    fail-fast 経路が TypeError で落ちて通常 outbox に fallback してしまう
    バグを防ぐ。
    """
    from datetime import datetime as _dt

    store = SQLiteStore(tmp_path / "wf.db")
    # datetime はそのままでは json.dumps できないが、default=str で文字列化される
    payload = {
        "workflow_id": "wf-dt",
        "occurred_at": _dt(2026, 5, 25, 19, 38, 0),
    }
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-dt:pr_created:0:0",
        workflow_id="wf-dt",
        event_type="pr_created",
        payload=payload,
        error="fail-fast",
    )
    with store._connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM notion_sync_errors WHERE idempotency_key = ?",
            ("wf-dt:pr_created:0:0",),
        ).fetchone()
    decoded = json.loads(row[0])
    assert decoded["workflow_id"] == "wf-dt"
    # default=str で datetime が文字列化されている
    assert isinstance(decoded["occurred_at"], str)
    assert "2026-05-25" in decoded["occurred_at"]


def test_record_permanent_notion_sync_failure_is_idempotent(tmp_path):
    """同一 idempotency_key の重複呼び出しでは errors に行が増えない（Round 1 指摘）"""
    store = SQLiteStore(tmp_path / "wf.db")
    key = "wf-y:pr_created:0:0"
    for _ in range(3):
        store.record_permanent_notion_sync_failure(
            idempotency_key=key,
            workflow_id="wf-y",
            event_type="pr_created",
            payload={"workflow_id": "wf-y"},
            error="fail-fast",
        )
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM notion_sync_errors WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
    assert rows[0] == 1, "同一 idempotency_key は 1 行のみ（重複挿入されない）"


def test_record_permanent_notion_sync_failure_inserts_row(tmp_path):
    """errors テーブルに新規行が attempts=0 で挿入される"""
    store = SQLiteStore(tmp_path / "wf.db")
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-x:pr_created:0:0",
        workflow_id="wf-x",
        event_type="pr_created",
        payload={"workflow_id": "wf-x", "pr_number": 42},
        error="fail-fast",
    )
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT workflow_id, event_type, attempts, payload_json, error "
            "FROM notion_sync_errors WHERE idempotency_key = ?",
            ("wf-x:pr_created:0:0",),
        ).fetchall()
    assert len(rows) == 1
    wf_id, event_type, attempts, payload_json, error = rows[0]
    assert wf_id == "wf-x"
    assert event_type == "pr_created"
    assert attempts == 0
    assert json.loads(payload_json) == {"workflow_id": "wf-x", "pr_number": 42}
    assert "fail-fast" in error


# --- Dispatcher _enqueue_failure の 4 ケーステスト ---


def _build_dispatcher_with_config(tmp_path, *, fail_fast: bool):
    """テスト用に dispatcher を最小構成で組む。

    config / store / retry のみセットアップ、Notion API は触らない経路だけ
    （_enqueue_failure 単体テストのため）。
    """
    from hokusai.integrations.notion_dashboard.dispatcher import (
        NotionSyncDispatcher,
    )

    store = SQLiteStore(tmp_path / "wf.db")

    config = MagicMock()
    config.sync_outbox = MagicMock()
    config.sync_outbox.fail_fast_on_workflow_started_error = fail_fast
    config.sync_outbox.max_retry_attempts = 10
    config.retry = MagicMock()
    config.retry.backoff_seconds = 5.0

    # NotionSyncDispatcher.__init__ を回避し、必要属性だけ直接設定
    dispatcher = NotionSyncDispatcher.__new__(NotionSyncDispatcher)
    dispatcher._store = store
    dispatcher._config = config
    dispatcher._workflow_page_id_cache = {}
    return dispatcher, store


def test_enqueue_failure_flag_off_uses_outbox(tmp_path):
    """flag=False なら workflow_started 失敗状態でも通常 outbox enqueue"""
    dispatcher, store = _build_dispatcher_with_config(tmp_path, fail_fast=False)
    # workflow_started が errors 入り済みの状態を準備
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-1:workflow_started:0:0",
        workflow_id="wf-1",
        event_type="workflow_started",
        payload={"workflow_id": "wf-1"},
        error="404",
    )
    # 子イベント発生
    dispatcher._enqueue_failure(
        idempotency_key="wf-1:pr_created:0:0",
        workflow_id="wf-1",
        event_type="pr_created",
        payload={"workflow_id": "wf-1"},
        error=Exception("boom"),
    )
    # flag=False なので通常 outbox 経路: pending に入る
    assert store.count_notion_sync_pending() == 1
    # errors は workflow_started の 1 件のみ（fail-fast 直送なし）
    assert store.count_notion_sync_errors() == 1


def test_enqueue_failure_flag_on_no_failed_workflow_started_uses_outbox(tmp_path):
    """flag=True でも workflow_started が errors に無ければ通常 outbox enqueue"""
    dispatcher, store = _build_dispatcher_with_config(tmp_path, fail_fast=True)
    dispatcher._enqueue_failure(
        idempotency_key="wf-2:pr_created:0:0",
        workflow_id="wf-2",
        event_type="pr_created",
        payload={"workflow_id": "wf-2"},
        error=Exception("boom"),
    )
    assert store.count_notion_sync_pending() == 1
    assert store.count_notion_sync_errors() == 0


def test_enqueue_failure_flag_on_with_failed_workflow_started_skips_outbox(tmp_path):
    """flag=True + workflow_started errors 入り → 子イベント errors 直送（fail-fast）"""
    dispatcher, store = _build_dispatcher_with_config(tmp_path, fail_fast=True)
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-3:workflow_started:0:0",
        workflow_id="wf-3",
        event_type="workflow_started",
        payload={"workflow_id": "wf-3"},
        error="404 share missing",
    )
    dispatcher._enqueue_failure(
        idempotency_key="wf-3:pr_created:0:0",
        workflow_id="wf-3",
        event_type="pr_created",
        payload={"workflow_id": "wf-3"},
        error=Exception("boom"),
    )
    # outbox には乗らない（fail-fast 直送）
    assert store.count_notion_sync_pending() == 0
    # errors には workflow_started + pr_created の 2 件
    assert store.count_notion_sync_errors() == 2


def test_enqueue_failure_workflow_started_itself_is_exempt(tmp_path):
    """workflow_started 自身は fail-fast 対象外（無限後方確認回避）"""
    dispatcher, store = _build_dispatcher_with_config(tmp_path, fail_fast=True)
    # 既に errors 入り済みの workflow_started がある状態で、同じ event_type の
    # 新規失敗が来る（idempotency_key は別）
    store.record_permanent_notion_sync_failure(
        idempotency_key="wf-4:workflow_started:0:0",
        workflow_id="wf-4",
        event_type="workflow_started",
        payload={"workflow_id": "wf-4"},
        error="404",
    )
    dispatcher._enqueue_failure(
        idempotency_key="wf-4:workflow_started:1:0",
        workflow_id="wf-4",
        event_type="workflow_started",
        payload={"workflow_id": "wf-4"},
        error=Exception("boom"),
    )
    # workflow_started 自身は fail-fast 対象外 → 通常 outbox に乗る
    assert store.count_notion_sync_pending() == 1
    # errors は元の workflow_started 1 件のみ
    assert store.count_notion_sync_errors() == 1


def test_enqueue_failure_missing_workflow_id_falls_back_to_outbox(tmp_path):
    """workflow_id 空文字 / None なら fail-fast 判定スキップして通常 outbox"""
    dispatcher, store = _build_dispatcher_with_config(tmp_path, fail_fast=True)
    dispatcher._enqueue_failure(
        idempotency_key="unknown:pr_created:0:0",
        workflow_id="",
        event_type="pr_created",
        payload={},
        error=Exception("boom"),
    )
    assert store.count_notion_sync_pending() == 1
    assert store.count_notion_sync_errors() == 0
