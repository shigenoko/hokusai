"""doc_orchestration 設定パースの回帰テスト（PR #177 レビュー P1）

YAML に doc_orchestration を書いたとき、role→provider が無視されず
DocOrchestrationConfig として正しく反映されることを保証する。
"""

import textwrap

import pytest

from hokusai.config import create_config_from_env_and_file, reset_config
from hokusai.config.loaders import _parse_doc_orchestration_config
from hokusai.config.models import DocOrchestrationConfig


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


def test_parse_honors_roles_and_enabled_and_rounds():
    cfg = _parse_doc_orchestration_config(
        {
            "doc_orchestration": {
                "enabled": True,
                "rounds": 3,
                "roles": {
                    "drafter": {"provider": "gemini"},
                    "reviewer": {"provider": "codex"},
                },
            }
        }
    )
    assert isinstance(cfg, DocOrchestrationConfig)
    assert cfg.enabled is True
    assert cfg.rounds == 3
    assert cfg.roles["drafter"]["provider"] == "gemini"
    assert cfg.roles["reviewer"]["provider"] == "codex"


def test_parse_unknown_provider_falls_back():
    cfg = _parse_doc_orchestration_config(
        {"doc_orchestration": {"roles": {"drafter": {"provider": "bogus"}}}}
    )
    # 未知 provider は既定（claude_code）にフォールバック
    assert cfg.roles["drafter"]["provider"] == "claude_code"


def test_parse_invalid_rounds_falls_back():
    cfg = _parse_doc_orchestration_config(
        {"doc_orchestration": {"rounds": 0}}
    )
    assert cfg.rounds == 1


def test_parse_non_dict_returns_default():
    cfg = _parse_doc_orchestration_config({"doc_orchestration": "nope"})
    assert cfg == DocOrchestrationConfig()


def test_yaml_config_reflects_doc_orchestration(tmp_path):
    """マネージャ経由（YAML→WorkflowConfig）で role が反映される（P1 回帰）。"""
    config_file = tmp_path / "cfg.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            project_root: .
            base_branch: main
            doc_orchestration:
              enabled: true
              rounds: 2
              roles:
                drafter:
                  provider: gemini
            """
        ),
        encoding="utf-8",
    )
    config = create_config_from_env_and_file(config_file)
    assert config.doc_orchestration.enabled is True
    assert config.doc_orchestration.rounds == 2
    # raw dict のまま渡って roles が空になる回帰を防ぐ
    assert config.doc_orchestration.roles["drafter"]["provider"] == "gemini"
