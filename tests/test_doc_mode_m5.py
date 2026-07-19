"""Phase 0 doc-mode M5 テスト（CLI ハンドラ＋出力 seam）"""

from types import SimpleNamespace

import pytest

from hokusai import doc_cli
from hokusai.config import WorkflowConfig, reset_config, set_config
from hokusai.config.models import DocOrchestrationConfig
from hokusai.nodes import phase0_doc

OK_TEXT = "背景 業務要件 スコープ 受入基準 制約 参照"


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    reset_config()
    set_config(WorkflowConfig(doc_orchestration=DocOrchestrationConfig(enabled=True)))
    monkeypatch.setattr(phase0_doc, "dispatch_via_gateway", lambda **k: None)
    phase0_doc.set_generation_backend(lambda p, m, prompt: OK_TEXT)
    yield
    reset_config()
    phase0_doc.set_generation_backend(None)
    doc_cli.set_output_sink(None)


def test_handle_doc_disabled_returns_one():
    set_config(WorkflowConfig(doc_orchestration=DocOrchestrationConfig(enabled=False)))
    assert doc_cli.handle_doc(_args()) == 1


def test_rounds_defaults_from_config_when_arg_none():
    set_config(
        WorkflowConfig(
            doc_orchestration=DocOrchestrationConfig(enabled=True, rounds=3)
        )
    )
    captured = {}
    doc_cli.set_output_sink(lambda state: captured.update(state))
    rc = doc_cli.handle_doc(_args(max_rounds=None))
    assert rc == 0
    assert captured["round"] == 3  # config.rounds が max_rounds に反映


def _args(**kw):
    base = {
        "doc_subcommand": "start",
        "type": "requirements",
        "topic": "DOM指摘",
        "feature_page": "",
        "max_rounds": 1,
        "mode": "auto",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_run_doc_workflow_produces_final_doc():
    state = doc_cli.run_doc_workflow("requirements", "DOM指摘")
    assert state["final_doc"] == OK_TEXT
    assert state["template_check"]["ok"] is True


def test_handle_doc_returns_zero_on_template_ok(capsys):
    rc = doc_cli.handle_doc(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "doc-mode 出力" in out
    assert "Issue 化のインプット" in out


def test_handle_doc_returns_two_on_template_ng():
    phase0_doc.set_generation_backend(lambda p, m, prompt: "背景 だけ")
    rc = doc_cli.handle_doc(_args(max_rounds=1))
    assert rc == 2  # 型NG は非ゼロ終了


def test_handle_doc_missing_subcommand():
    assert doc_cli.handle_doc(SimpleNamespace(doc_subcommand=None)) == 1


def test_output_sink_override():
    captured = {}
    doc_cli.set_output_sink(lambda state: captured.setdefault("doc", state["final_doc"]))
    rc = doc_cli.handle_doc(_args())
    assert rc == 0
    assert captured["doc"] == OK_TEXT


def test_render_doc_output_shows_approval_pending():
    state = doc_cli.run_doc_workflow("requirements", "T")
    rendered = doc_cli.render_doc_output(state)
    assert "承認: 未" in rendered  # auto でも自動承認しない（HITL 原則）
