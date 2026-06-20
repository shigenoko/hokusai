"""Phase 0 doc-mode M4 テスト（rounds ループ / 型NG→draft 戻し、上限つき）"""

import pytest

from hokusai.config import WorkflowConfig, reset_config, set_config
from hokusai.doc_graph import create_compiled_doc_workflow
from hokusai.nodes import phase0_doc
from hokusai.state import create_doc_workflow_state

OK_TEXT = "背景 業務要件 スコープ 受入基準 制約 参照"
NG_TEXT = "背景 業務要件 だけ"  # 必須セクション欠落


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    reset_config()
    set_config(WorkflowConfig())
    monkeypatch.setattr(phase0_doc, "dispatch_via_gateway", lambda **k: None)
    yield
    reset_config()
    phase0_doc.set_generation_backend(None)


def test_crosscheck_runs_max_rounds():
    phase0_doc.set_generation_backend(lambda p, m, prompt: OK_TEXT)
    app = create_compiled_doc_workflow()
    state = create_doc_workflow_state("wf", "requirements", "T", max_rounds=3)
    result = app.invoke(state)
    # crosscheck が max_rounds 回まわった
    assert result["round"] == 3
    assert len(result["review_notes"]) == 3
    assert result["template_check"]["ok"] is True


def test_template_ng_loops_back_then_caps():
    # 常に NG を返す → 型NGループが上限で打ち切られる
    phase0_doc.set_generation_backend(lambda p, m, prompt: NG_TEXT)
    app = create_compiled_doc_workflow()
    state = create_doc_workflow_state(
        "wf", "requirements", "T", max_rounds=1, max_finalize_rounds=2
    )
    result = app.invoke(state)
    assert result["template_check"]["ok"] is False
    # 上限（2回）で打ち切られ、無限ループしない
    assert result["finalize_attempts"] == 2


def test_template_ng_then_ok_recovers():
    calls = {"n": 0}

    def backend(provider, model, prompt):
        # finalize 1回目は NG、その後 OK（draft/review でも呼ばれるが内容は影響なし）
        if "確定稿を出力" in prompt:
            calls["n"] += 1
            return NG_TEXT if calls["n"] == 1 else OK_TEXT
        return "draft-or-review"

    phase0_doc.set_generation_backend(backend)
    app = create_compiled_doc_workflow()
    state = create_doc_workflow_state(
        "wf", "requirements", "T", max_rounds=1, max_finalize_rounds=3
    )
    result = app.invoke(state)
    assert result["template_check"]["ok"] is True
    assert result["finalize_attempts"] == 2  # NG→draft戻し→2回目でOK
