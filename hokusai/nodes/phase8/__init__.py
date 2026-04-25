"""
Phase 8: PR作成とレビュー対応

=== 統合レビューループ（推奨・デフォルト） ===
Copilot/人間 のレビューを順不同で処理可能。

Phase 8a: Draft PR作成
Phase 8b (unified): レビュー待ち（Human-in-the-loop）
Phase 8c (unified): 統合レビュー指摘確認（全レビューワー対象）
Phase 8d (unified): 統合レビュー指摘修正（Human-in-the-loop）
Phase 8e: Ready for Review / 承認確認
Phase 8_complete: 完了

=== 旧フロー（後方互換性のため残存） ===
Phase 8b: Copilotレビュー待ち（Human-in-the-loop）
Phase 8c: Copilot指摘確認
Phase 8d: Copilot指摘修正（Human-in-the-loop）
Phase 8e: Ready for Review（人間レビュー開始）
Phase 8f: 人間レビュー待ち（Human-in-the-loop）
Phase 8g: 人間レビュー指摘確認
Phase 8h: 人間レビュー指摘修正（Human-in-the-loop）
"""

# PR作成関連
# コメント返信・解決
from .comment_handler import (
    _generate_reply_message,
    _reply_to_all_comments,
)

# 完了処理
from .complete import (
    phase8_complete_node,
)
from .pr_creation import (
    _check_branch_exists,
    _create_new_pr,
    _create_pr_for_repository,
    _extract_pr_info_from_result,
    phase8a_pr_draft_node,
)

# 既存PR検索
from .pr_lookup import (
    _find_existing_pr,
    _get_git_client_for_pr,
)

# Ready for Review処理
from .ready_for_review import (
    _is_repository_successful,
    _mark_successful_prs_ready,
    phase8e_ready_for_review_node,
)

# レビュー確認
from .review_check import (
    _check_all_review_comments,
    _check_review_comments,
    phase8c_copilot_check_node,
    phase8c_unified_check_node,
    phase8d_copilot_fix_node,
    phase8d_unified_fix_node,
    phase8g_human_check_node,
    phase8h_human_fix_node,
)

# レビュー待機フロー
from .review_wait import (
    _resume_review_wait,
    phase8b_copilot_wait_node,
    phase8b_unified_wait_node,
    phase8f_human_wait_node,
)

__all__ = [
    # Public phase nodes (used by graph.py)
    "phase8a_pr_draft_node",
    "phase8b_copilot_wait_node",
    "phase8c_copilot_check_node",
    "phase8d_copilot_fix_node",
    "phase8e_ready_for_review_node",
    "phase8f_human_wait_node",
    "phase8g_human_check_node",
    "phase8h_human_fix_node",
    "phase8_complete_node",
    # 統合レビューループ用ノード
    "phase8b_unified_wait_node",
    "phase8c_unified_check_node",
    "phase8d_unified_fix_node",
    # Helper functions (for testing and internal use)
    "_check_branch_exists",
    "_create_new_pr",
    "_extract_pr_info_from_result",
    "_create_pr_for_repository",
    "_get_git_client_for_pr",
    "_find_existing_pr",
    "_resume_review_wait",
    "_check_review_comments",
    "_check_all_review_comments",
    "_generate_reply_message",
    "_reply_to_all_comments",
    "_is_repository_successful",
    "_mark_successful_prs_ready",
]
