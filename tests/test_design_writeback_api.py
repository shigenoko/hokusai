"""Step 2: outbox 操作 API / 冪等キー生成のテスト

対象: hokusai/integrations/design/writeback/{outbox,idempotency}.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hokusai.integrations.design.writeback import (
    OutboxStore,
    WritebackTarget,
    build_idempotency_key,
)
from hokusai.persistence.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# 冪等キー生成
# ---------------------------------------------------------------------------


def test_build_idempotency_key_format():
    """計画書 §9.1 のキー形式 {workflow_id}:{event_type}:{resource}:{revision}"""
    key = build_idempotency_key(
        workflow_id="wf_20260513_001",
        event_type="phase8a_completed",
        resource="figma_frame_abc123",
        revision="a1b2c3d4",
    )
    assert key == "wf_20260513_001:phase8a_completed:figma_frame_abc123:a1b2c3d4"


def test_build_idempotency_key_rejects_empty():
    with pytest.raises(ValueError):
        build_idempotency_key(
            workflow_id="",
            event_type="phase8a_completed",
            resource="frame",
            revision="r",
        )


def test_build_idempotency_key_encodes_colons():
    """構成要素に ':' を含む値は URL エンコードして結合（Figma node_id 対応）"""
    # Figma node_id 例: "0:1" → "0%3A1" にエンコードされて区切り文字と曖昧化しない
    key = build_idempotency_key(
        workflow_id="wf_1",
        event_type="phase8a_completed",
        resource="figma_0:1",
        revision="rev",
    )
    assert key == "wf_1:phase8a_completed:figma_0%3A1:rev"


def test_build_idempotency_key_round_trip_decode():
    """エンコード後の key を split + unquote で元の値を復元できる"""
    from urllib.parse import unquote
    key = build_idempotency_key(
        workflow_id="wf_1",
        event_type="phase8a_completed",
        resource="figma_0:1",
        revision="abc",
    )
    parts = [unquote(p) for p in key.split(":")]
    assert parts == ["wf_1", "phase8a_completed", "figma_0:1", "abc"]


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Phase E スキーマ込みで初期化された DB"""
    path = tmp_path / "wf.db"
    SQLiteStore(path)
    return path


@pytest.fixture
def figma_store(db_path: Path) -> OutboxStore:
    return OutboxStore(db_path, target=WritebackTarget.FIGMA)


@pytest.fixture
def miro_store(db_path: Path) -> OutboxStore:
    return OutboxStore(db_path, target=WritebackTarget.MIRO)


# ---------------------------------------------------------------------------
# enqueue / mark_succeeded
# ---------------------------------------------------------------------------


def test_enqueue_creates_outbox_row(figma_store: OutboxStore):
    """失敗時に outbox に積まれる"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name="company-a",
        event_type="phase8a_completed",
        payload={"mr_url": "https://example.com", "commit_sha": "abc123"},
        error="HTTPError 403",
    )

    entries = figma_store.list_outbox()
    assert len(entries) == 1
    e = entries[0]
    assert e.idempotency_key == "key-1"
    assert e.workflow_id == "wf-1"
    assert e.profile_name == "company-a"
    assert e.event_type == "phase8a_completed"
    assert e.payload["mr_url"] == "https://example.com"
    assert e.attempt_count == 0
    assert e.last_error == "HTTPError 403"


def test_enqueue_is_upsert(figma_store: OutboxStore):
    """同じ idempotency_key で 2 回 enqueue すると更新（重複しない）"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        event_type="phase8a_completed",
        payload={"v": 1},
        error="error A",
    )
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        event_type="phase8a_completed",
        payload={"v": 2},
        error="error B",
    )

    entries = figma_store.list_outbox()
    assert len(entries) == 1
    assert entries[0].last_error == "error B"


def test_enqueue_upsert_refreshes_payload_and_metadata(figma_store: OutboxStore):
    """upsert 時に payload_json / workflow_id / profile_name / event_type も更新"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-old",
        profile_name="company-a",
        event_type="phase8a_completed",
        payload={"mr_url": "https://old"},
        error="initial",
    )
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-new",
        profile_name="company-b",
        event_type="phase8a_completed",
        payload={"mr_url": "https://new"},
        error="updated",
    )

    entries = figma_store.list_outbox()
    assert len(entries) == 1
    e = entries[0]
    assert e.workflow_id == "wf-new"
    assert e.profile_name == "company-b"
    assert e.payload["mr_url"] == "https://new"
    assert e.last_error == "updated"


def test_mark_succeeded_records_idempotency_and_removes_outbox(
    figma_store: OutboxStore,
):
    """成功時: design_writeback_idempotency に INSERT、outbox から削除"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name="company-a",
        event_type="phase8a_completed",
        payload={},
        error="temporary failure",
    )
    assert len(figma_store.list_outbox()) == 1

    figma_store.mark_succeeded(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name="company-a",
        resource="node-abc",
        response_id="comment-xyz",
    )

    assert len(figma_store.list_outbox()) == 0
    assert figma_store.is_already_delivered("key-1")


def test_mark_succeeded_without_prior_outbox(figma_store: OutboxStore):
    """outbox に pending が無くても直接成功記録できる（通常経路）"""
    figma_store.mark_succeeded(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        resource="node-abc",
        response_id="comment-xyz",
    )
    assert figma_store.is_already_delivered("key-1")


# ---------------------------------------------------------------------------
# should_skip: 3 段階チェック
# ---------------------------------------------------------------------------


def test_should_skip_for_already_delivered(figma_store: OutboxStore):
    """投稿済みは skip"""
    figma_store.mark_succeeded(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        resource="node",
        response_id="r",
    )
    assert figma_store.should_skip("key-1") is True


def test_should_skip_for_pending(figma_store: OutboxStore):
    """pending（outbox にある）も skip"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        event_type="phase8a_completed",
        payload={},
        error="err",
    )
    assert figma_store.should_skip("key-1") is True


def test_should_skip_for_errors_by_default(figma_store: OutboxStore):
    """errors（5 回失敗で諦め済）も既定では skip"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        event_type="phase8a_completed",
        payload={},
        error="err",
    )
    outbox = figma_store.list_outbox()[0]
    figma_store.move_to_errors(outbox.id)

    assert figma_store.should_skip("key-1") is True
    # force=True なら errors を無視
    assert figma_store.should_skip("key-1", force=True) is False


def test_should_skip_for_unknown_key(figma_store: OutboxStore):
    """未知のキーは skip しない"""
    assert figma_store.should_skip("key-unknown") is False


# ---------------------------------------------------------------------------
# increment_attempt / move_to_errors
# ---------------------------------------------------------------------------


def test_increment_attempt(figma_store: OutboxStore):
    """手動再送で attempt_count が +1 される"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        event_type="phase8a_completed",
        payload={},
        error="err1",
    )
    outbox = figma_store.list_outbox()[0]
    assert outbox.attempt_count == 0

    count = figma_store.increment_attempt(outbox.id, error="err2")
    assert count == 1

    count = figma_store.increment_attempt(outbox.id, error="err3")
    assert count == 2

    updated = figma_store.get_outbox(outbox.id)
    assert updated is not None
    assert updated.attempt_count == 2
    assert updated.last_error == "err3"


def test_move_to_errors(figma_store: OutboxStore):
    """outbox 行を errors に移動"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name="company-a",
        event_type="phase8a_completed",
        payload={"v": 1},
        error="initial failure",
    )
    outbox = figma_store.list_outbox()[0]

    assert figma_store.move_to_errors(outbox.id, error="5 attempts exceeded") is True

    # outbox から消えて、errors に入っている
    assert len(figma_store.list_outbox()) == 0
    errors = figma_store.list_errors()
    assert len(errors) == 1
    assert errors[0]["idempotency_key"] == "key-1"
    assert errors[0]["profile_name"] == "company-a"
    assert errors[0]["error_message"] == "5 attempts exceeded"


def test_move_to_errors_missing_row(figma_store: OutboxStore):
    """存在しない id を move_to_errors しても False を返す"""
    assert figma_store.move_to_errors(99999) is False


# ---------------------------------------------------------------------------
# profile_name による分離
# ---------------------------------------------------------------------------


def test_list_outbox_filters_by_profile(figma_store: OutboxStore):
    """profile_name で絞り込める"""
    figma_store.enqueue(
        idempotency_key="key-a",
        workflow_id="wf-a",
        profile_name="company-a",
        event_type="phase8a_completed",
        payload={},
        error="err",
    )
    figma_store.enqueue(
        idempotency_key="key-b",
        workflow_id="wf-b",
        profile_name="company-b",
        event_type="phase8a_completed",
        payload={},
        error="err",
    )

    only_a = figma_store.list_outbox(profile_name="company-a")
    assert [e.idempotency_key for e in only_a] == ["key-a"]

    only_b = figma_store.list_outbox(profile_name="company-b")
    assert [e.idempotency_key for e in only_b] == ["key-b"]

    all_entries = figma_store.list_outbox()
    assert len(all_entries) == 2


# ---------------------------------------------------------------------------
# Figma / Miro の分離
# ---------------------------------------------------------------------------


def test_figma_and_miro_are_isolated(figma_store: OutboxStore, miro_store: OutboxStore):
    """Figma と Miro の outbox は別テーブル、同じキーでも独立"""
    figma_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        event_type="phase8a_completed",
        payload={"target": "figma"},
        error="err",
    )
    miro_store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        event_type="phase8a_completed",
        payload={"target": "miro"},
        error="err",
    )

    assert len(figma_store.list_outbox()) == 1
    assert len(miro_store.list_outbox()) == 1
    assert figma_store.list_outbox()[0].payload["target"] == "figma"
    assert miro_store.list_outbox()[0].payload["target"] == "miro"


def test_idempotency_table_shared_between_targets(
    figma_store: OutboxStore, miro_store: OutboxStore,
):
    """design_writeback_idempotency は共有テーブル（target 列で識別）"""
    figma_store.mark_succeeded(
        idempotency_key="key-figma",
        workflow_id="wf-1",
        profile_name=None,
        resource="node",
        response_id="r",
    )
    miro_store.mark_succeeded(
        idempotency_key="key-miro",
        workflow_id="wf-1",
        profile_name=None,
        resource="card",
        response_id="r",
    )

    assert figma_store.is_already_delivered("key-figma") is True
    assert miro_store.is_already_delivered("key-miro") is True
    # 共有テーブルだが PRIMARY KEY なので別キー扱い
    assert figma_store.is_already_delivered("key-miro") is True  # idempotency は target 横断
    assert miro_store.is_already_delivered("key-figma") is True


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_old_errors(figma_store: OutboxStore, db_path: Path):
    """retention_days を超える errors / idempotency を削除"""
    import sqlite3
    from datetime import datetime, timedelta
    # 31 日前の行を動的に生成（テスト実行日に依存しないため）
    old = (datetime.now() - timedelta(days=31)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO figma_sync_errors
                (idempotency_key, workflow_id, profile_name, event_type,
                 payload_json, error_message, failed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("old-key", "wf-old", None, "phase8a_completed", "{}", "old err", old),
        )
        conn.execute(
            """
            INSERT INTO design_writeback_idempotency
                (idempotency_key, workflow_id, profile_name, target,
                 resource, response_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("old-idem", "wf-old", None, "figma", "node", "r", old),
        )
        conn.commit()

    # 新しい行も追加（残るはず）
    figma_store.enqueue(
        idempotency_key="recent-key",
        workflow_id="wf-recent",
        profile_name=None,
        event_type="phase8a_completed",
        payload={},
        error="err",
    )
    figma_store.move_to_errors(figma_store.list_outbox()[0].id)

    deleted = figma_store.cleanup_old_errors(retention_days=30)
    assert deleted == 2  # old-key (errors) + old-idem (idempotency)

    errors = figma_store.list_errors()
    assert {e["idempotency_key"] for e in errors} == {"recent-key"}
