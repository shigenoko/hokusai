"""
Graph Definition

LangGraphのStateGraph構築とコンパイル。
"""

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from .config import get_config
from .nodes import (
    phase1_prepare_node,
    phase2_research_node,
    phase3_design_node,
    phase4_plan_node,
    phase5_implement_node,
    phase6_verify_node,
    phase7_5_branch_hygiene_node,
    phase7_review_node,
    phase8_complete_node,
    phase8a_pr_draft_node,
    phase8b_copilot_wait_node,
    # 統合レビューループ用ノード
    phase8b_unified_wait_node,
    phase8c_copilot_check_node,
    phase8c_unified_check_node,
    phase8d_copilot_fix_node,
    phase8d_unified_fix_node,
    phase8e_ready_for_review_node,
    phase8f_human_wait_node,
    phase8g_human_check_node,
    phase8h_human_fix_node,
    phase10_record_node,
    should_continue_review_loop,
    # 統合レビューループ用ルーター
    should_fix_any_review_issues,
    should_retry_implementation,
    should_retry_review,
)
from .state import WorkflowState


def create_workflow() -> StateGraph:
    """
    開発ワークフローグラフを作成

    Returns:
        構築されたStateGraph
    """
    # StateGraphを作成
    workflow = StateGraph(WorkflowState)

    # ノードを追加
    workflow.add_node("phase1_prepare", phase1_prepare_node)
    workflow.add_node("phase2_research", phase2_research_node)
    workflow.add_node("phase3_design", phase3_design_node)
    workflow.add_node("phase4_plan", phase4_plan_node)
    workflow.add_node("phase5_implement", phase5_implement_node)
    workflow.add_node("phase6_verify", phase6_verify_node)
    workflow.add_node("phase7_review", phase7_review_node)
    workflow.add_node("phase7_5_hygiene", phase7_5_branch_hygiene_node)

    # Phase 8: PR作成とレビュー対応
    workflow.add_node("phase8a_pr_draft", phase8a_pr_draft_node)
    # 統合レビューループ用ノード（Copilot/人間/Devin.ai 順不同対応）
    workflow.add_node("phase8b_unified_wait", phase8b_unified_wait_node)
    workflow.add_node("phase8c_unified_check", phase8c_unified_check_node)
    workflow.add_node("phase8d_unified_fix", phase8d_unified_fix_node)
    workflow.add_node("phase8e_ready_for_review", phase8e_ready_for_review_node)
    workflow.add_node("phase8_complete", phase8_complete_node)
    # 旧ノード（後方互換性のため残す、グラフには接続しない）
    workflow.add_node("phase8b_copilot_wait", phase8b_copilot_wait_node)
    workflow.add_node("phase8c_copilot_check", phase8c_copilot_check_node)
    workflow.add_node("phase8d_copilot_fix", phase8d_copilot_fix_node)
    workflow.add_node("phase8f_human_wait", phase8f_human_wait_node)
    workflow.add_node("phase8g_human_check", phase8g_human_check_node)
    workflow.add_node("phase8h_human_fix", phase8h_human_fix_node)

    workflow.add_node("phase10_record", phase10_record_node)

    # エントリーポイント
    workflow.set_entry_point("phase1_prepare")

    # 基本的なエッジ（順次実行）
    workflow.add_edge("phase1_prepare", "phase2_research")
    workflow.add_edge("phase2_research", "phase3_design")

    # Phase 3 → Phase 4 (スキーマ変更チェック)
    # 現在の実装ではスキーマPRは別途作成するため、直接Phase 4へ
    workflow.add_edge("phase3_design", "phase4_plan")

    # Phase 4 → Phase 5
    workflow.add_edge("phase4_plan", "phase5_implement")

    # Phase 5 (Human-in-the-loop)
    # 実装完了後、Phase 6 へ
    # 注: Human-in-the-loopの場合、ワークフローは一時停止し、
    # 手動で continue コマンドを実行して再開する
    workflow.add_edge("phase5_implement", "phase6_verify")

    # Phase 6 → 条件分岐
    workflow.add_conditional_edges(
        "phase6_verify",
        should_retry_implementation,
        {
            "phase5_implement": "phase5_implement",  # 検証失敗 → 実装に戻る
            "phase7_review": "phase7_review",        # 検証成功 → レビューへ
            "end": END,                              # リトライ上限到達 → 停止（fail-close）
        }
    )

    # Phase 7 → 条件分岐
    workflow.add_conditional_edges(
        "phase7_review",
        should_retry_review,
        {
            "phase5_implement": "phase5_implement",  # レビュー失敗 → 実装に戻る
            "phase7_5_hygiene": "phase7_5_hygiene",  # レビュー成功 → ブランチ衛生チェックへ
            "end": END,                              # リトライ上限到達 → 停止（fail-close）
        }
    )

    # Phase 7.5 → Phase 8a（ブランチ衛生チェック後、PR作成）
    workflow.add_edge("phase7_5_hygiene", "phase8a_pr_draft")

    # === Phase 8: PR作成とレビュー対応（統合レビューループ） ===
    #
    # 統合フロー（Copilot/人間レビュー 順不同対応）:
    # 8a: Draft PR作成 → 8b: レビュー待ち (Human-in-the-loop)
    # 8b: レビュー確認完了（コメント返信処理）
    # 8c: 統合レビュー指摘確認 → 指摘あり？
    #     ├─ Yes → 8d: 修正 (Human-in-the-loop) → 8b に戻る
    #     └─ No  → 8e: Ready for Review処理 → 承認判定
    # 8e: Ready for Review → 全PR承認済み？
    #     ├─ No  → 8b: レビュー待ちに戻る
    #     └─ Yes → 8_complete: Phase 9完了 → Phase 10

    # Phase 8a: Draft PR作成 → 統合レビュー待ち (Human-in-the-loop)
    workflow.add_edge("phase8a_pr_draft", "phase8b_unified_wait")

    # Phase 8b: レビュー確認完了 → 統合指摘確認
    workflow.add_edge("phase8b_unified_wait", "phase8c_unified_check")

    # Phase 8c: 統合レビュー指摘確認 → 条件分岐
    workflow.add_conditional_edges(
        "phase8c_unified_check",
        should_fix_any_review_issues,
        {
            "phase8d_unified_fix": "phase8d_unified_fix",           # 指摘あり → 修正
            "phase8e_ready_for_review": "phase8e_ready_for_review", # 指摘なし → Ready for Review
        }
    )

    # Phase 8d: 統合修正 (Human-in-the-loop) → レビュー待ちに戻る
    workflow.add_edge("phase8d_unified_fix", "phase8b_unified_wait")

    # Phase 8e: Ready for Review → 承認状態で分岐
    workflow.add_conditional_edges(
        "phase8e_ready_for_review",
        should_continue_review_loop,
        {
            "phase8b_unified_wait": "phase8b_unified_wait",  # 未承認あり → 継続
            "phase8_complete": "phase8_complete",            # 全承認済み → 完了
        }
    )

    # Phase 9 完了 → Phase 10
    workflow.add_edge("phase8_complete", "phase10_record")

    # Phase 10 → END
    workflow.add_edge("phase10_record", END)

    # === 移行パス（旧フロー → 統合フロー） ===
    # 旧ワークフローが旧ノードで停止していた場合に統合フローへ移行
    workflow.add_edge("phase8b_copilot_wait", "phase8c_unified_check")
    workflow.add_edge("phase8f_human_wait", "phase8c_unified_check")

    return workflow


def create_compiled_workflow(checkpointer=None):
    """
    コンパイル済みワークフローを作成

    Args:
        checkpointer: チェックポインター（省略時はSQLiteSaverを使用）

    Returns:
        コンパイル済みワークフロー
    """
    config = get_config()

    if checkpointer is None:
        # SQLiteSaverを使用（独自のDBファイルを使用）
        conn = sqlite3.connect(config.checkpoint_db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        checkpointer = SqliteSaver(conn)
        # テーブルを初期化
        checkpointer.setup()

    workflow = create_workflow()
    return workflow.compile(checkpointer=checkpointer)
