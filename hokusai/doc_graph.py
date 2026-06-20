"""Phase 0 doc-mode グラフ定義（M1: 最小パス）

実装フロー（``graph.py`` / phase1-10）とは独立した別グラフ。
M1 では draft → crosscheck → finalize の線形フローを構築する。
rounds ループ / 型NGループ / HITL ゲートは後続マイルストーン（M4/M5）。

Issue #176 / 設計書「Phase 0 doc-mode ワークフロー」§3・§6 に対応。
"""

from langgraph.graph import END, StateGraph

from .nodes.phase0_doc import (
    phase0a_ideation_node,
    phase0b_draft_node,
    phase0c_crosscheck_node,
    phase0d_finalize_node,
    should_continue_crosscheck,
    should_fix_template,
)
from .state import DocWorkflowState


def create_doc_workflow() -> StateGraph:
    """doc-mode の StateGraph を構築する（未コンパイル）。

    M3+M4: ideation → draft → crosscheck（rounds ループ）→ finalize
    （型NG なら draft へ戻す、上限つき）。
    """
    workflow = StateGraph(DocWorkflowState)

    workflow.add_node("phase0a_ideation", phase0a_ideation_node)
    workflow.add_node("phase0b_draft", phase0b_draft_node)
    workflow.add_node("phase0c_crosscheck", phase0c_crosscheck_node)
    workflow.add_node("phase0d_finalize", phase0d_finalize_node)

    workflow.set_entry_point("phase0a_ideation")
    workflow.add_edge("phase0a_ideation", "phase0b_draft")
    workflow.add_edge("phase0b_draft", "phase0c_crosscheck")

    # crosscheck: max_rounds 回まで繰り返してから finalize へ
    workflow.add_conditional_edges(
        "phase0c_crosscheck",
        should_continue_crosscheck,
        {
            "phase0c_crosscheck": "phase0c_crosscheck",
            "phase0d_finalize": "phase0d_finalize",
        },
    )

    # finalize: 型NG なら draft に戻す（上限到達で END）
    workflow.add_conditional_edges(
        "phase0d_finalize",
        should_fix_template,
        {
            "phase0b_draft": "phase0b_draft",
            "END": END,
        },
    )

    return workflow


def create_compiled_doc_workflow(checkpointer=None):
    """コンパイル済み doc-mode ワークフローを返す。

    Args:
        checkpointer: 省略時は checkpointer なしでコンパイルする
            （doc-mode は短命なため M1 では永続化を必須としない）。
    """
    workflow = create_doc_workflow()
    if checkpointer is None:
        return workflow.compile()
    return workflow.compile(checkpointer=checkpointer)
