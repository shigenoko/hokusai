"""
Phase 8b/8f: レビュー待機フロー

Copilot/人間レビュー待ちの再開時の処理を行う。
"""

from ...integrations.factory import get_git_hosting_client
from ...state import (
    PhaseStatus,
    WorkflowState,
    get_current_pr,
    update_phase_status,
    update_pr_in_list,
)
from .comment_handler import _reply_to_all_comments
from .pr_lookup import _get_git_client_for_pr


def _resume_review_wait(
    state: WorkflowState,
    review_type: str,
) -> WorkflowState:
    """レビュー待ち再開時の共通処理。

    待機フラグをクリアし、未返信コメントに自動返信する。

    Args:
        state: ワークフロー状態
        review_type: "copilot" または "human"

    Returns:
        更新されたワークフロー状態
    """
    is_copilot = review_type == "copilot"

    # 待機フラグをクリア
    if is_copilot:
        state["waiting_for_copilot_review"] = False
    else:
        state["waiting_for_human_review"] = False
    state["waiting_for_human"] = False

    # レビュータイプに応じたフィールド名を決定
    pr_comments_key = "copilot_comments" if is_copilot else "human_comments"
    state_comments_key = "copilot_review_comments" if is_copilot else "human_review_comments"
    review_label = "Copilotレビュー" if is_copilot else "人間レビュー"
    comment_label = "Copilotコメント" if is_copilot else "人間レビューコメント"

    # 現在のPRを取得（複数PR対応）
    current_pr = get_current_pr(state)
    if current_pr:
        pr_number = current_pr.get("number")
        git_hosting = _get_git_client_for_pr(current_pr)
        pr_display = f"PR #{pr_number} ({current_pr.get('repo_name', 'Backend')})"
        comments = current_pr.get(pr_comments_key, [])
    else:
        pr_number = None
        git_hosting = get_git_hosting_client()
        pr_display = "PR (不明)"
        comments = state.get(state_comments_key, [])

    print(f"▶️ {review_label}確認完了 ({pr_display})")

    # 修正フローからの再開時のプッシュ検証
    is_fix_resume = state.get("human_input_request") in (
        "review_fix", "copilot_fix", "human_fix"
    )
    # 自動修正の場合はプッシュ検証をスキップ（push済みを確認済み、GitHub API遅延を回避）
    is_auto_fix = state.get("auto_fix_attempts", 0) > 0
    if is_fix_resume and not is_auto_fix and current_pr:
        baseline = current_pr.get("commit_count_before_fix")
        if baseline is not None:
            current_count = git_hosting.get_pr_commit_count(pr_number)
            if current_count is not None and current_count <= baseline:
                print(f"⚠️ {pr_display}: 新しいコミットが検出されませんでした")
                print(f"   修正前: {baseline}コミット → 現在: {current_count}コミット")
                print("   コードを修正してプッシュしてから再実行してください")
                state["waiting_for_human"] = True
                state["human_input_request"] = state.get("human_input_request")
                state["push_verification_failed"] = True
                return state

    state["push_verification_failed"] = False

    # 修正後かつプッシュ検証を通過した場合のみ、未返信のコメントに返信
    # レビュー再確認（review_status）では返信しない
    if is_fix_resume:
        unreplied = [c for c in comments if not c.get("replied")]
        if unreplied:
            print(f"📝 {comment_label} {len(unreplied)}件 に返信中...")
            updated_comments = _reply_to_all_comments(
                state, comments, pr_number=pr_number, git_hosting=git_hosting
            )
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {pr_comments_key: updated_comments})
            else:
                state[state_comments_key] = updated_comments

        # issue comment にも返信
        if current_pr:
            issue_comments = current_pr.get("issue_comments", [])
        else:
            issue_comments = state.get("issue_comments", [])
        issue_unreplied = [c for c in issue_comments if not c.get("replied")]
        if issue_unreplied:
            print(f"📝 issue comment {len(issue_unreplied)}件 に返信中...")
            updated_issue = _reply_to_all_comments(
                state, issue_comments, pr_number=pr_number, git_hosting=git_hosting,
                resolve_after_reply=False,
            )
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {"issue_comments": updated_issue})
            state["issue_comments"] = updated_issue

    return state


def phase8b_copilot_wait_node(state: WorkflowState) -> WorkflowState:
    """Phase 8b: Copilotレビュー待ち（再開後の処理）"""
    return _resume_review_wait(state, "copilot")


def phase8f_human_wait_node(state: WorkflowState) -> WorkflowState:
    """Phase 8f: 人間レビュー待ち（再開後の処理）"""
    return _resume_review_wait(state, "human")


def phase8b_unified_wait_node(state: WorkflowState) -> WorkflowState:
    """Phase 8b: 統合レビュー待ち（再開後の処理）

    Copilot/人間 のレビューを順不同で処理する統合待機ノード。
    """
    # Phase 9 (レビュー対応) をIN_PROGRESSに設定（まだでなければ）
    if state["phases"][9]["status"] != PhaseStatus.IN_PROGRESS.value:
        state = update_phase_status(state, 9, PhaseStatus.IN_PROGRESS)

    # 待機フラグをクリア
    state["waiting_for_human"] = False
    state["waiting_for_copilot_review"] = False
    state["waiting_for_human_review"] = False

    # 現在のPRを取得（複数PR対応）
    current_pr = get_current_pr(state)
    if current_pr:
        pr_number = current_pr.get("number")
        git_hosting = _get_git_client_for_pr(current_pr)
        pr_display = f"PR #{pr_number} ({current_pr.get('repo_name', 'Backend')})"
        copilot_comments = current_pr.get("copilot_comments", [])
        human_comments = current_pr.get("human_comments", [])
        issue_comments = current_pr.get("issue_comments", [])
    else:
        pr_number = None
        git_hosting = get_git_hosting_client()
        pr_display = "PR (不明)"
        copilot_comments = state.get("copilot_review_comments", [])
        human_comments = state.get("human_review_comments", [])
        issue_comments = state.get("issue_comments", [])

    print(f"▶️ レビュー確認完了 ({pr_display})")

    # 修正フローからの再開か判定
    is_fix_resume = state.get("human_input_request") in (
        "review_fix", "copilot_fix", "human_fix"
    )
    # 自動修正の場合はプッシュ検証をスキップ（push済みを確認済み、GitHub API遅延を回避）
    is_auto_fix = state.get("auto_fix_attempts", 0) > 0

    # プッシュ検証（人間による修正フローからの再開時のみ）
    if is_fix_resume and not is_auto_fix and current_pr:
        baseline = current_pr.get("commit_count_before_fix")
        if baseline is not None:
            current_count = git_hosting.get_pr_commit_count(pr_number)
            if current_count is not None and current_count <= baseline:
                print(f"⚠️ {pr_display}: 新しいコミットが検出されませんでした")
                print(f"   修正前: {baseline}コミット → 現在: {current_count}コミット")
                print("   コードを修正してプッシュしてから再実行してください")
                state["waiting_for_human"] = True
                state["human_input_request"] = "review_fix"
                state["push_verification_failed"] = True
                return state

    # プッシュ検証成功
    state["push_verification_failed"] = False

    # 未返信のコメントに返信（ソースコード修正後のみ）
    # is_fix_resume: 人間による修正フローからの再開（プッシュ検証通過済み）
    # is_auto_fix: 自動修正成功後（push済み確認済み）
    # レビュー再確認（review_status）では返信しない
    should_reply = is_fix_resume or is_auto_fix
    all_comments = copilot_comments + human_comments + issue_comments
    unreplied = [c for c in all_comments if not c.get("replied")]

    if unreplied and should_reply:
        print(f"📝 レビューコメント {len(unreplied)}件 に返信中...")
        # Copilotコメントの返信
        copilot_unreplied = [c for c in copilot_comments if not c.get("replied")]
        if copilot_unreplied:
            updated_copilot = _reply_to_all_comments(
                state, copilot_comments, pr_number=pr_number, git_hosting=git_hosting
            )
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {"copilot_comments": updated_copilot})
            else:
                state["copilot_review_comments"] = updated_copilot

        # 人間コメントの返信
        human_unreplied = [c for c in human_comments if not c.get("replied")]
        if human_unreplied:
            updated_human = _reply_to_all_comments(
                state, human_comments, pr_number=pr_number, git_hosting=git_hosting
            )
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {"human_comments": updated_human})
            else:
                state["human_review_comments"] = updated_human

        # issue comment の返信
        issue_unreplied = [c for c in issue_comments if not c.get("replied")]
        if issue_unreplied:
            updated_issue = _reply_to_all_comments(
                state, issue_comments, pr_number=pr_number, git_hosting=git_hosting,
                resolve_after_reply=False,
            )
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {"issue_comments": updated_issue})
            state["issue_comments"] = updated_issue

    return state
