"""Phase 0 doc-mode M1 スモークテスト（Issue #176）

検証内容:
- doc-mode グラフが構築・コンパイルできる
- draft → crosscheck → finalize を1周実行し、各成果が State に入る
- 各 LLM 呼び出しが dispatch_via_gateway（purpose=draft/review/finalize、
  phase=0）を経由する
- finalize の型準拠チェックが動作する
"""

import pytest

from hokusai.config import WorkflowConfig, reset_config, set_config
from hokusai.config.models import DocOrchestrationConfig
from hokusai.doc_graph import create_compiled_doc_workflow, create_doc_workflow
from hokusai.nodes import phase0_doc
from hokusai.state import create_doc_workflow_state


@pytest.fixture(autouse=True)
def _config():
    """doc_orchestration を含む既定 config をセットする。"""
    reset_config()
    set_config(WorkflowConfig(doc_orchestration=DocOrchestrationConfig(enabled=True)))
    yield
    reset_config()
    phase0_doc.set_generation_backend(None)


def test_graph_builds_and_compiles():
    create_doc_workflow()  # 構築できる
    app = create_compiled_doc_workflow()  # コンパイルできる
    assert app is not None


def test_one_pass_with_injected_backend(monkeypatch):
    dispatched = []

    def fake_dispatch(**kwargs):
        dispatched.append((kwargs.get("purpose"), kwargs.get("phase")))

    monkeypatch.setattr(phase0_doc, "dispatch_via_gateway", fake_dispatch)

    def fake_backend(provider, model, prompt):
        # 要件定義書の必須セクションを全て含む確定稿を返す
        return "背景 / 業務要件 / スコープ / 受入基準 / 制約 / 参照"

    phase0_doc.set_generation_backend(fake_backend)

    app = create_compiled_doc_workflow()
    state = create_doc_workflow_state(
        workflow_id="wf-doc-0001",
        doc_type="requirements",
        topic="DOM指摘のMCP登録Tool",
    )
    result = app.invoke(state)

    # 各ステップの成果が入っている
    assert result["draft"]
    assert result["review_notes"] and len(result["review_notes"]) == 1
    assert result["final_doc"]
    assert result["current_step"] == "finalize"

    # 型準拠チェックが OK（必須セクション網羅）
    assert result["template_check"]["ok"] is True
    assert result["template_check"]["missing"] == []

    # Gateway を ideation/draft/review/finalize の順で、phase=0 で経由している
    assert [p for p, _ in dispatched] == ["ideation", "draft", "review", "finalize"]
    assert all(phase == 0 for _, phase in dispatched)

    # 監査ログが各ノード分積まれている
    actions = [e["action"] for e in result["audit_log"]]
    assert actions == [
        "phase0a_ideation",
        "phase0b_draft",
        "phase0c_crosscheck",
        "phase0d_finalize",
    ]


def test_template_check_detects_missing_sections():
    result = phase0_doc.check_template("requirements", "背景 と 業務要件 だけ")
    assert result["ok"] is False
    assert "スコープ" in result["missing"]


def test_invoke_llm_falls_back_to_default_backend(monkeypatch):
    """バックエンド未注入なら default_generation_backend にフォールバックする（M2）。"""
    monkeypatch.setattr(phase0_doc, "dispatch_via_gateway", lambda **k: None)
    monkeypatch.setattr(
        phase0_doc,
        "default_generation_backend",
        lambda provider, model, prompt: f"DEFAULT:{provider}",
    )
    phase0_doc.set_generation_backend(None)
    out = phase0_doc.invoke_llm("claude_code", "", "prompt", purpose="draft")
    assert out == "DEFAULT:claude_code"
