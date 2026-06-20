"""doc-mode 本格 HITL（interrupt / continue）テスト

LangGraph の interrupt/Command を使い、step モードで HITL ゲートに停止し、
continue（approve/reject）で再開できることを検証する。実 LLM/CLI には依存せず、
生成 backend を注入し MemorySaver で checkpoint する。
"""

from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver

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
    doc_cli.set_doc_checkpointer(MemorySaver())
    yield
    reset_config()
    phase0_doc.set_generation_backend(None)
    doc_cli.set_doc_checkpointer(None)
    doc_cli.set_output_sink(None)


def test_step_interrupts_at_human_gate():
    wid, result, interrupted = doc_cli.start_doc_step("requirements", "T", workflow_id="wf-hitl-1")
    assert interrupted is True
    # ゲート前に finalize 済み（確定稿はある）が、まだ承認されていない
    assert result.get("final_doc") == OK_TEXT
    assert result.get("approved", False) is False


def test_continue_approve_sets_approved():
    doc_cli.start_doc_step("requirements", "T", workflow_id="wf-hitl-2")
    final = doc_cli.continue_doc("wf-hitl-2", approve=True)
    assert final["approved"] is True
    assert final["current_step"] == "human_gate"


def test_continue_reject_keeps_unapproved():
    doc_cli.start_doc_step("requirements", "T", workflow_id="wf-hitl-3")
    final = doc_cli.continue_doc("wf-hitl-3", approve=False)
    assert final["approved"] is False


def test_auto_mode_passthrough_not_approved():
    # auto は自動承認しない（承認待ちのまま）— HITL 原則
    state = doc_cli.run_doc_workflow("requirements", "T", run_mode="auto")
    assert state["current_step"] == "human_gate"
    assert state["approved"] is False


def _start_args(**kw):
    base = {
        "doc_subcommand": "start",
        "type": "requirements",
        "topic": "DOM指摘",
        "feature_page": "",
        "max_rounds": 1,
        "mode": "step",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_cli_step_then_continue_approve(capsys):
    rc = doc_cli.handle_doc(_start_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "承認待ち" in out
    # start で発行された workflow_id を出力から取得
    import re

    m = re.search(r"doc continue (\S+) --approve", out)
    assert m, out
    wid = m.group(1)

    rc2 = doc_cli.handle_doc(
        SimpleNamespace(doc_subcommand="continue", workflow_id=wid, approve=True, reject=False)
    )
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "Issue 化のインプット" in out2


def test_cli_continue_reject_returns_one(capsys):
    doc_cli.start_doc_step("requirements", "T", workflow_id="wf-hitl-cli-rej")
    rc = doc_cli.handle_doc(
        SimpleNamespace(
            doc_subcommand="continue", workflow_id="wf-hitl-cli-rej", approve=False, reject=True
        )
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "却下" in out
