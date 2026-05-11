"""HOKUSAI Web Dashboard の Notion 同期パネル + 再送 API のテスト

Phase D: Operations Console 化のうち、Web Dashboard 側の追加機能を検証する。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.config import set_config
from hokusai.config.models import NotionDashboardConfig, WorkflowConfig
from hokusai.persistence.sqlite_store import SQLiteStore


@pytest.fixture
def isolated_dashboard(monkeypatch, tmp_path):
    """dashboard.py のモジュール状態を毎テストでクリーンに保つ"""
    import scripts.dashboard as dashboard_module

    # SQLite を tmp_path に逃がす
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr(dashboard_module, "DB_PATH", db_path)
    monkeypatch.setattr(dashboard_module, "_store", None)

    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=db_path,
        checkpoint_db_path=tmp_path / "cp.db",
    )
    set_config(cfg)

    yield dashboard_module


def test_render_notion_dashboard_panel_returns_empty_when_disabled(isolated_dashboard):
    # デフォルトは enabled=False
    html = isolated_dashboard.render_notion_dashboard_panel()
    assert html == ""


def test_render_notion_dashboard_panel_shows_when_enabled(isolated_dashboard, monkeypatch):
    # enabled=True にして再描画
    cfg = WorkflowConfig(
        data_dir=isolated_dashboard.DB_PATH.parent,
        database_path=isolated_dashboard.DB_PATH,
        checkpoint_db_path=isolated_dashboard.DB_PATH.parent / "cp.db",
        notion_dashboard=NotionDashboardConfig(
            enabled=True,
            api_token_env="NONEXISTENT",
            workflows_db_id_env="NONEXISTENT_DB",
        ),
    )
    set_config(cfg)
    monkeypatch.setattr(isolated_dashboard, "_store", None)

    html = isolated_dashboard.render_notion_dashboard_panel()
    assert "Notion メインダッシュボード" in html
    assert "同期再送" in html
    # is_configured = False（環境変数未設定）でもパネルは表示される
    assert "設定済み" in html or "設定" in html


def test_render_notion_dashboard_panel_shows_pending_count(isolated_dashboard, monkeypatch):
    cfg = WorkflowConfig(
        data_dir=isolated_dashboard.DB_PATH.parent,
        database_path=isolated_dashboard.DB_PATH,
        checkpoint_db_path=isolated_dashboard.DB_PATH.parent / "cp.db",
        notion_dashboard=NotionDashboardConfig(enabled=True),
    )
    set_config(cfg)
    monkeypatch.setattr(isolated_dashboard, "_store", None)

    # outbox に 2 件、errors に 1 件積む
    store = isolated_dashboard._get_store()
    store.enqueue_notion_sync("k1", "wf-1", "phase_changed", {})
    store.enqueue_notion_sync("k2", "wf-2", "phase_changed", {})
    # error queue に手動で挿入
    store.enqueue_notion_sync("k3", "wf-3", "phase_changed", {})
    store.move_notion_sync_to_error("k3", "fatal")

    html = isolated_dashboard.render_notion_dashboard_panel()
    assert "保留 2 件" in html
    assert "永続失敗 1 件" in html


def test_get_notion_dispatcher_returns_dispatcher(isolated_dashboard):
    cfg = WorkflowConfig(
        data_dir=isolated_dashboard.DB_PATH.parent,
        database_path=isolated_dashboard.DB_PATH,
        checkpoint_db_path=isolated_dashboard.DB_PATH.parent / "cp.db",
        notion_dashboard=NotionDashboardConfig(enabled=True),
    )
    set_config(cfg)
    disp = isolated_dashboard._get_notion_dispatcher()
    assert disp is not None
    # disabled 環境変数 → is_configured False
    assert disp.is_configured() is False
