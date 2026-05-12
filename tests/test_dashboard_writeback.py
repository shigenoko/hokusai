"""Step 6: Operations Console UI API（writeback 関連）のテスト

dashboard.py の writeback ハンドラを直接 import するのは困難なため、
最小限の API 動作確認は OutboxStore レイヤーで行う。

UI 経由の End-to-End は手動 QA / 統合テストで担保する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hokusai.integrations.design.writeback import (
    OutboxStore,
    WritebackTarget,
)
from hokusai.persistence.sqlite_store import SQLiteStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "wf.db"
    SQLiteStore(p)
    return p


def test_outbox_listing_provides_resource_field(db_path):
    """list_outbox の結果から resource (node_id / frame_id) が抽出できる"""
    store = OutboxStore(db_path, target=WritebackTarget.FIGMA)
    store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name="company-a",
        event_type="phase8a_completed",
        payload={
            "file_key": "file-abc",
            "node_id": "node-xyz",
            "mr_url": "https://example.com",
        },
        error="403 Forbidden",
    )

    entries = store.list_outbox()
    assert len(entries) == 1
    e = entries[0]
    # Console API が resource として抽出するキー
    resource = e.payload.get("node_id") or e.payload.get("frame_id")
    assert resource == "node-xyz"


def test_miro_outbox_provides_frame_id_as_resource(db_path):
    store = OutboxStore(db_path, target=WritebackTarget.MIRO)
    store.enqueue(
        idempotency_key="key-2",
        workflow_id="wf-2",
        profile_name=None,
        event_type="phase8a_completed",
        payload={"board_id": "board-1", "frame_id": "frame-x"},
        error="rate limit",
    )
    e = store.list_outbox()[0]
    resource = e.payload.get("node_id") or e.payload.get("frame_id")
    assert resource == "frame-x"


def test_list_outbox_supports_profile_filter(db_path):
    """Operations Console の profile フィルタが効く（v0.3.0 整合）"""
    store = OutboxStore(db_path, target=WritebackTarget.FIGMA)
    store.enqueue(
        idempotency_key="key-a",
        workflow_id="wf-a",
        profile_name="company-a",
        event_type="phase8a_completed",
        payload={},
        error="err",
    )
    store.enqueue(
        idempotency_key="key-b",
        workflow_id="wf-b",
        profile_name="company-b",
        event_type="phase8a_completed",
        payload={},
        error="err",
    )

    only_a = store.list_outbox(profile_name="company-a")
    assert [e.idempotency_key for e in only_a] == ["key-a"]


def test_errors_listing_includes_failed_at(db_path):
    """list_errors の各行が failed_at を含む（Console 表示に使う）"""
    store = OutboxStore(db_path, target=WritebackTarget.FIGMA)
    store.enqueue(
        idempotency_key="key-1",
        workflow_id="wf-1",
        profile_name=None,
        event_type="phase8a_completed",
        payload={},
        error="err",
    )
    store.move_to_errors(store.list_outbox()[0].id)
    errors = store.list_errors()
    assert len(errors) == 1
    assert "failed_at" in errors[0]
    assert errors[0]["error_message"] is not None
