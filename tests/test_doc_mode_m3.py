"""Phase 0 doc-mode M3 テスト（ideation ノード＋壁打ちルールブック）"""

import pytest

from hokusai.config import WorkflowConfig, reset_config, set_config
from hokusai.nodes import phase0_doc
from hokusai.state import create_doc_workflow_state


@pytest.fixture(autouse=True)
def _config():
    reset_config()
    set_config(WorkflowConfig())
    yield
    reset_config()
    phase0_doc.set_generation_backend(None)


def test_ideation_node_uses_rulebook_and_gateway(monkeypatch):
    seen = {}

    def fake_dispatch(**kwargs):
        seen["purpose"] = kwargs.get("purpose")
        seen["phase"] = kwargs.get("phase")

    monkeypatch.setattr(phase0_doc, "dispatch_via_gateway", fake_dispatch)

    captured = {}

    def fake_backend(provider, model, prompt):
        captured["prompt"] = prompt
        return "発散結果"

    phase0_doc.set_generation_backend(fake_backend)

    state = create_doc_workflow_state("wf-i", "requirements", "DOM指摘")
    out = phase0_doc.phase0a_ideation_node(state)

    assert out["ideation_result"] == "発散結果"
    assert out["current_step"] == "ideation"
    # 壁打ちルールブックの技法がプロンプトに含まれる
    assert "steelman" in captured["prompt"]
    assert "red-team" in captured["prompt"]
    # Gateway を purpose=ideation / phase=0 で経由
    assert seen == {"purpose": "ideation", "phase": 0}
