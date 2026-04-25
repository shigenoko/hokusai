"""
Phase 8完了処理

全PRのレビュープロセスが完了した際の処理を行う。
"""

from ...state import (
    PhaseStatus,
    PRStatus,
    WorkflowState,
    add_audit_log,
    update_phase_status,
)
from ...utils.notion_helpers import record_pr_callout_to_notion


def phase8_complete_node(state: WorkflowState) -> WorkflowState:
    """Phase 8完了処理"""
    pull_requests = state.get("pull_requests", [])

    # 全PRが人間確認済みかチェック
    all_confirmed = all(
        pr.get("human_review_confirmed", False)
        for pr in pull_requests
    ) if pull_requests else True

    if not all_confirmed:
        # 未確認のPRがある → レビュー待ちに戻す
        state["waiting_for_human"] = True
        state["human_input_request"] = "review_status"
        unconfirmed = [
            pr for pr in pull_requests
            if not pr.get("human_review_confirmed", False)
        ]
        print(f"⏳ レビュー未確認のPRが{len(unconfirmed)}件あります:")
        for pr in unconfirmed:
            print(f"   - PR #{pr.get('number')} ({pr.get('repo_name')})")
        return state

    # 全PRのレビュー完了
    # NotionのPR情報を更新（phase8a以降に追加されたPRがあれば記録）
    state = record_pr_callout_to_notion(state, phase=9)

    state = update_phase_status(state, 9, PhaseStatus.COMPLETED)
    state = add_audit_log(state, 9, "phase_completed", "success", {
        "total_prs": len(pull_requests),
        "completed_prs": len(pull_requests),
    })

    if len(pull_requests) > 1:
        print(f"✅ Phase 9 完了: 全{len(pull_requests)}件のPRレビュープロセス完了")
        for pr in pull_requests:
            status_emoji = "✓" if pr.get("status") in (PRStatus.APPROVED.value, PRStatus.MERGED.value) else "○"
            print(f"   {status_emoji} PR #{pr.get('number')} ({pr.get('repo_name')})")
    else:
        print("✅ Phase 9 完了: PRレビュープロセス完了")

    return state
