"""Step 5: WorkflowRunner 統合のテスト

対象: hokusai/integrations/design/writeback/integration.py
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hokusai.integrations.design.writeback import (
    WritebackEnabledConfig,
    build_figma_dispatcher,
    build_miro_dispatcher,
    decide_primary_figma,
    decide_primary_miro,
    dispatch_phase8a_writeback,
    load_writeback_config,
    populate_primary_writeback_targets,
)
from hokusai.persistence.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# load_writeback_config
# ---------------------------------------------------------------------------


def test_load_writeback_config_with_dict_config():
    """dict 形式の config から writeback 設定を読む"""
    class Cfg:
        figma = {
            "api_token_env": "HOKUSAI_FIGMA_API_TOKEN",
            "writeback": {"enabled": True, "on_failure": "warn"},
        }
        miro = {
            "api_token_env": "HOKUSAI_MIRO_API_TOKEN",
            "writeback": {"enabled": False, "on_failure": "block"},
        }
    cfg = load_writeback_config(Cfg())
    assert cfg.figma_enabled is True
    assert cfg.figma_on_failure == "warn"
    assert cfg.figma_token_env == "HOKUSAI_FIGMA_API_TOKEN"
    assert cfg.miro_enabled is False
    assert cfg.miro_on_failure == "block"
    assert cfg.miro_token_env == "HOKUSAI_MIRO_API_TOKEN"


def test_load_writeback_config_normalizes_invalid_on_failure():
    """on_failure に不正値が来たら既定 warn にフォールバック"""
    class Cfg:
        figma = {"writeback": {"enabled": True, "on_failure": "unknown"}}
        miro = {"writeback": {"enabled": True, "on_failure": None}}
    cfg = load_writeback_config(Cfg())
    assert cfg.figma_on_failure == "warn"
    assert cfg.miro_on_failure == "warn"


def test_load_writeback_config_with_dataclass_writeback():
    """dataclass の WritebackConfig 経由でも動作（実 WorkflowConfig 経路）"""
    from hokusai.config.models import (
        FigmaIntegrationConfig,
        MiroIntegrationConfig,
        WritebackConfig,
    )
    class Cfg:
        figma = FigmaIntegrationConfig(
            enabled=True,
            api_token_env="HOKUSAI_FIGMA_API_TOKEN",
            writeback=WritebackConfig(enabled=True, on_failure="block"),
        )
        miro = MiroIntegrationConfig(
            enabled=True,
            api_token_env="HOKUSAI_MIRO_API_TOKEN",
            writeback=WritebackConfig(enabled=False, on_failure="skip"),
        )
    cfg = load_writeback_config(Cfg())
    assert cfg.figma_enabled is True
    assert cfg.figma_on_failure == "block"
    assert cfg.miro_enabled is False
    assert cfg.miro_on_failure == "skip"


def test_load_writeback_config_missing_writeback_section():
    """既存 config に writeback 節がない場合は disabled"""
    class Cfg:
        figma = {"api_token_env": "X"}
        miro = None
    cfg = load_writeback_config(Cfg())
    assert cfg.figma_enabled is False
    assert cfg.miro_enabled is False


def test_load_writeback_config_no_figma_miro_attrs():
    """figma / miro 属性自体が無くても安全に動く"""
    class Cfg:
        pass
    cfg = load_writeback_config(Cfg())
    assert cfg.figma_enabled is False
    assert cfg.miro_enabled is False


def test_load_writeback_config_rejects_non_bool_enabled():
    """enabled に "false" / "0" / 数値などの非 bool が来たら False にフォールバック。

    Copilot 指摘: bool("false") は True なので、誤って writeback が
    有効化されてしまうのを防ぐ。
    """
    class Cfg:
        figma = {"writeback": {"enabled": "false"}}
        miro = {"writeback": {"enabled": "0"}}
    cfg = load_writeback_config(Cfg())
    assert cfg.figma_enabled is False
    assert cfg.miro_enabled is False

    class Cfg2:
        figma = {"writeback": {"enabled": 1}}  # int も拒否
        miro = {"writeback": {"enabled": "true"}}  # 文字列 true も拒否
    cfg2 = load_writeback_config(Cfg2())
    assert cfg2.figma_enabled is False
    assert cfg2.miro_enabled is False


# ---------------------------------------------------------------------------
# build_*_dispatcher
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "wf.db"
    SQLiteStore(path)
    return path


def test_build_figma_dispatcher_disabled(db_path):
    cfg = WritebackEnabledConfig(figma_enabled=False)
    assert build_figma_dispatcher(db_path, cfg) is None


def test_build_figma_dispatcher_token_missing(db_path, monkeypatch):
    """enabled でも token env が未設定なら None"""
    monkeypatch.delenv("HOKUSAI_FIGMA_API_TOKEN", raising=False)
    cfg = WritebackEnabledConfig(
        figma_enabled=True, figma_token_env="HOKUSAI_FIGMA_API_TOKEN",
    )
    assert build_figma_dispatcher(db_path, cfg) is None


def test_build_figma_dispatcher_returns_instance(db_path, monkeypatch):
    monkeypatch.setenv("HOKUSAI_FIGMA_API_TOKEN", "dummy-token")
    cfg = WritebackEnabledConfig(
        figma_enabled=True, figma_token_env="HOKUSAI_FIGMA_API_TOKEN",
    )
    dispatcher = build_figma_dispatcher(db_path, cfg)
    assert dispatcher is not None
    assert dispatcher.store.db_path == db_path


def test_build_miro_dispatcher_returns_instance(db_path, monkeypatch):
    monkeypatch.setenv("HOKUSAI_MIRO_API_TOKEN", "dummy-token")
    cfg = WritebackEnabledConfig(
        miro_enabled=True, miro_token_env="HOKUSAI_MIRO_API_TOKEN",
    )
    dispatcher = build_miro_dispatcher(db_path, cfg)
    assert dispatcher is not None


# ---------------------------------------------------------------------------
# decide_primary_*
# ---------------------------------------------------------------------------


def test_decide_primary_figma_from_target_node():
    """figma_target_node_id があれば優先採用"""
    state = {
        "figma_file_key": "file-abc",
        "figma_target_node_id": "node-1",
        "figma_context": {"screens": [{"id": "node-other"}]},
    }
    result = decide_primary_figma(state)
    assert result["primary_figma_file_key"] == "file-abc"
    assert result["primary_figma_node_id"] == "node-1"
    assert result["primary_figma_frame_id"] == "node-1"
    assert result["primary_figma_node_offset"] == {"x": 0, "y": 0}


def test_decide_primary_figma_from_first_screen():
    """target_node_id が無ければ screens 先頭（FigmaClient._screen_from_node が
    生成する実スキーマ node_id キーを使う）"""
    state = {
        "figma_file_key": "file-abc",
        "figma_target_node_id": None,
        "figma_context": {
            "screens": [{"node_id": "node-1"}, {"node_id": "node-2"}],
        },
    }
    result = decide_primary_figma(state)
    assert result["primary_figma_node_id"] == "node-1"


def test_decide_primary_figma_legacy_id_fallback():
    """旧 / 別形式の "id" キーも fallback として受け付ける"""
    state = {
        "figma_file_key": "file-abc",
        "figma_target_node_id": None,
        "figma_context": {"screens": [{"id": "node-1"}, {"id": "node-2"}]},
    }
    result = decide_primary_figma(state)
    assert result["primary_figma_node_id"] == "node-1"


def test_decide_primary_figma_missing_file_key():
    """file_key が無ければ空 dict"""
    state = {"figma_file_key": None}
    assert decide_primary_figma(state) == {}


def test_decide_primary_miro_from_first_screen():
    """MiroClient.to_common_context() の実 schema（node_id キー）を採用"""
    state = {
        "miro_board_id": "board-abc",
        "miro_context": {"screens": [{"node_id": "frame-1", "name": "Home"}]},
    }
    result = decide_primary_miro(state)
    assert result["primary_miro_board_id"] == "board-abc"
    assert result["primary_miro_frame_id"] == "frame-1"


def test_decide_primary_miro_fallback_legacy_id_key():
    """旧 schema (id / frame_id) も後方互換として受け付ける"""
    state = {
        "miro_board_id": "board-abc",
        "miro_context": {"screens": [{"id": "frame-legacy"}]},
    }
    result = decide_primary_miro(state)
    assert result["primary_miro_frame_id"] == "frame-legacy"


def test_decide_primary_miro_missing_board():
    state = {"miro_board_id": None}
    assert decide_primary_miro(state) == {}


def test_decide_primary_miro_empty_screens():
    state = {"miro_board_id": "b", "miro_context": {"screens": []}}
    assert decide_primary_miro(state) == {}


# ---------------------------------------------------------------------------
# populate_primary_writeback_targets
# ---------------------------------------------------------------------------


def test_populate_writes_to_state():
    """state に primary_* を書き込む（実 schema: node_id キー）"""
    state = {
        "figma_file_key": "file-abc",
        "figma_target_node_id": "node-1",
        "miro_board_id": "board-abc",
        "miro_context": {"screens": [{"node_id": "frame-1"}]},
    }
    populate_primary_writeback_targets(state)
    assert state["primary_figma_file_key"] == "file-abc"
    assert state["primary_figma_node_id"] == "node-1"
    assert state["primary_miro_board_id"] == "board-abc"
    assert state["primary_miro_frame_id"] == "frame-1"


def test_populate_does_not_overwrite_existing():
    """既存値があれば上書きしない"""
    state = {
        "figma_file_key": "file-abc",
        "figma_target_node_id": "node-new",
        "primary_figma_node_id": "node-existing",
    }
    populate_primary_writeback_targets(state)
    assert state["primary_figma_node_id"] == "node-existing"


# ---------------------------------------------------------------------------
# dispatch_phase8a_writeback
# ---------------------------------------------------------------------------


def test_dispatch_skips_without_dispatchers(db_path):
    """両 dispatcher が None なら何も呼ばれない"""
    state = {"workflow_id": "wf-1"}
    result = dispatch_phase8a_writeback(
        state,
        mr_url="https://example.com",
        commit_sha="abc",
        figma_dispatcher=None,
        miro_dispatcher=None,
    )
    assert result.figma is None
    assert result.miro is None


def test_dispatch_figma_called_with_args(db_path):
    """Figma dispatcher が primary_* 設定済み state で呼ばれる"""
    state = {
        "workflow_id": "wf-1",
        "primary_figma_file_key": "file-abc",
        "primary_figma_node_id": "node-1",
        "primary_figma_node_offset": {"x": 0, "y": 0},
    }
    figma = MagicMock()
    figma.dispatch.return_value = {"status": "delivered"}

    result = dispatch_phase8a_writeback(
        state,
        mr_url="https://example.com/mr/1",
        commit_sha="abc1234",
        figma_dispatcher=figma,
        miro_dispatcher=None,
        profile_name="company-a",
    )
    assert result.figma == {"status": "delivered"}
    args = figma.dispatch.call_args.args[0]
    assert args.workflow_id == "wf-1"
    assert args.profile_name == "company-a"
    assert args.file_key == "file-abc"
    assert args.node_id == "node-1"
    assert args.mr_url == "https://example.com/mr/1"
    assert args.commit_sha == "abc1234"


def test_dispatch_figma_skipped_if_primary_unset(db_path):
    """primary_figma_* が未設定なら dispatcher を呼ばずに skipped"""
    state = {"workflow_id": "wf-1"}
    figma = MagicMock()

    result = dispatch_phase8a_writeback(
        state,
        mr_url="https://example.com",
        commit_sha="abc",
        figma_dispatcher=figma,
        miro_dispatcher=None,
    )
    figma.dispatch.assert_not_called()
    assert result.figma["status"] == "skipped"


def test_dispatch_miro_uses_frame_meta_from_context(db_path):
    """Miro dispatch の frame_meta を miro_context から復元

    実 schema: MiroClient.to_common_context() / _build_miro_screens() が
    screens に node_id / x / y / width / height を含める（v0.4.0 拡張）。
    """
    state = {
        "workflow_id": "wf-1",
        "primary_miro_board_id": "board-1",
        "primary_miro_frame_id": "frame-1",
        "miro_context": {
            "screens": [
                {"node_id": "frame-1", "x": 10.0, "y": 20.0, "width": 100.0},
                {"node_id": "frame-2", "x": 999.0, "y": 999.0, "width": 100.0},
            ],
        },
    }
    miro = MagicMock()
    miro.dispatch.return_value = {"status": "delivered"}

    dispatch_phase8a_writeback(
        state,
        mr_url="https://example.com",
        commit_sha="abc",
        figma_dispatcher=None,
        miro_dispatcher=miro,
    )
    args = miro.dispatch.call_args.args[0]
    assert args.board_id == "board-1"
    assert args.frame_id == "frame-1"
    assert args.frame_meta == {"x": 10.0, "y": 20.0, "width": 100.0}


def test_dispatch_both_targets(db_path):
    """Figma / Miro 両方 enabled なら両方呼ばれる"""
    state = {
        "workflow_id": "wf-1",
        "primary_figma_file_key": "f-1",
        "primary_figma_node_id": "n-1",
        "primary_miro_board_id": "b-1",
        "primary_miro_frame_id": "fr-1",
    }
    figma = MagicMock()
    figma.dispatch.return_value = {"status": "delivered"}
    miro = MagicMock()
    miro.dispatch.return_value = {"status": "delivered"}

    result = dispatch_phase8a_writeback(
        state,
        mr_url="https://example.com",
        commit_sha="abc",
        figma_dispatcher=figma,
        miro_dispatcher=miro,
    )
    assert result.figma["status"] == "delivered"
    assert result.miro["status"] == "delivered"
    figma.dispatch.assert_called_once()
    miro.dispatch.assert_called_once()
