"""
Router Functions

ワークフローの分岐を制御する関数群。
"""

from ..config import get_config
from ..state import VerificationResult, WorkflowState


def check_schema_change(state: WorkflowState) -> str:
    """
    スキーマ変更の有無で分岐を決定

    Returns:
        "create_schema_pr": スキーマ変更が必要
        "phase4_plan": スキーマ変更不要
    """
    if state["schema_change_required"]:
        return "create_schema_pr"
    return "phase4_plan"


def should_retry_implementation(state: WorkflowState) -> str:
    """
    Phase 6の結果に基づいて次のノードを決定

    Returns:
        "phase5_implement": 検証失敗、実装に戻る
        "phase7_review": 検証成功、レビューに進む
        "end": リトライ上限到達、ワークフロー停止（fail-close）
    """
    # fail-close: リトライ上限到達でワークフロー停止
    if state.get("waiting_for_human", False):
        return "end"

    config = get_config()

    # 検証失敗のチェック
    has_failure = any(
        v == VerificationResult.FAIL.value
        for v in state["verification"].values()
    )

    if has_failure:
        if state["phases"][6]["retry_count"] >= config.max_retry_count:
            return "end"
        return "phase5_implement"

    return "phase7_review"


def should_retry_review(state: WorkflowState) -> str:
    """
    Phase 7の結果に基づいて次のノードを決定

    Returns:
        "phase5_implement": レビュー失敗、実装に戻る
        "phase7_5_hygiene": レビュー成功、ブランチ衛生チェックに進む
        "end": リトライ上限到達、ワークフロー停止（fail-close）
    """
    # fail-close: リトライ上限到達でワークフロー停止
    if state.get("waiting_for_human", False):
        return "end"

    config = get_config()

    if not state["final_review_passed"]:
        if state["phases"][7]["retry_count"] >= config.max_retry_count:
            return "end"
        return "phase5_implement"

    return "phase7_5_hygiene"


def is_waiting_for_human(state: WorkflowState) -> bool:
    """Human-in-the-loop待機状態かどうか"""
    return state.get("waiting_for_human", False)


def should_fix_copilot_issues(state: WorkflowState) -> str:
    """
    Copilot指摘の有無で分岐を決定

    Returns:
        "phase8d_copilot_fix": 指摘あり、修正に進む
        "phase8e_ready_for_review": 指摘なし、Ready for Reviewに進む
    """
    if state.get("copilot_fix_requested", False):
        return "phase8d_copilot_fix"
    return "phase8e_ready_for_review"


def should_fix_human_issues(state: WorkflowState) -> str:
    """
    人間レビュー指摘の有無で分岐を決定

    Returns:
        "phase8h_human_fix": 指摘あり、修正に進む
        "phase8_complete": 指摘なし、Phase 8完了に進む
    """
    if state.get("human_fix_requested", False):
        return "phase8h_human_fix"
    return "phase8_complete"


def should_fix_any_review_issues(state: WorkflowState) -> str:
    """
    統合レビュー指摘の有無で分岐を決定（Copilot/人間/Devin.ai 順不同対応）

    Returns:
        "phase8d_unified_fix": 指摘あり、修正に進む
        "phase8e_ready_for_review": 指摘なし、Ready for Reviewチェック/完了に進む
    """
    if state.get("review_fix_requested", False):
        return "phase8d_unified_fix"
    return "phase8e_ready_for_review"


def should_continue_review_loop(state: WorkflowState) -> str:
    """
    Ready for Review後のレビューループ継続判定

    Returns:
        "phase8b_unified_wait": レビュー継続（承認待ち）
        "phase8_complete": 全承認済み、完了
    """
    # 全PRが承認済みかチェック
    pull_requests = state.get("pull_requests", [])
    if not pull_requests:
        return "phase8_complete"

    from ..state import PRStatus
    for pr in pull_requests:
        status = pr.get("status", PRStatus.PENDING.value)
        # APPROVED または MERGED 以外は継続
        if status not in (PRStatus.APPROVED.value, PRStatus.MERGED.value):
            return "phase8b_unified_wait"

    return "phase8_complete"
