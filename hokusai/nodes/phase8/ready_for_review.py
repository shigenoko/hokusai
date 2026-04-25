"""
Phase 8e: Ready for Review処理

成功したリポジトリのPRをReady for Reviewに変更し、
失敗したリポジトリのPRはDraftのまま維持する。
レビュー前に変更サマリーをPR本文に反映する。
"""

from ...logging_config import get_logger
from ...state import (
    PullRequestInfo,
    RepositoryPhaseStatus,
    WorkflowState,
    add_audit_log,
    get_repository_state,
)
from ...utils.change_summary import build_pr_change_summary
from ...utils.pr_body_sections import upsert_section
from .pr_lookup import _get_git_client_for_pr

logger = get_logger("phase8e")


def _is_repository_successful(state: WorkflowState, repo_name: str) -> bool:
    """
    リポジトリが成功状態かどうかを判定

    Phase 6 (検証) と Phase 7 (レビュー) の両方で成功している場合にTrueを返す。

    Args:
        state: ワークフロー状態
        repo_name: リポジトリ名

    Returns:
        True: 成功、False: 失敗または未完了
    """
    # repositories で確認
    repo_state = get_repository_state(state, repo_name)
    if repo_state:
        phase_status = repo_state.get("phase_status", {})
        # Phase 6 と 7 が完了しているか
        p6_status = phase_status.get(6, RepositoryPhaseStatus.PENDING.value)
        p7_status = phase_status.get(7, RepositoryPhaseStatus.PENDING.value)
        if (p6_status == RepositoryPhaseStatus.COMPLETED.value and
            p7_status == RepositoryPhaseStatus.COMPLETED.value):
            return True

    return False


def _mark_successful_prs_ready(
    state: WorkflowState,
) -> tuple[list[PullRequestInfo], list[PullRequestInfo]]:
    """
    成功したリポジトリのPRをReady for Reviewに変更

    C-2: 部分的なPR完了フロー

    Args:
        state: ワークフロー状態

    Returns:
        (ready_prs, draft_prs): Ready for Reviewに変更したPRと、Draftのまま維持するPRのタプル
    """
    pull_requests = state.get("pull_requests", [])
    ready_prs = []
    draft_prs = []

    for pr in pull_requests:
        repo_name = pr.get("repo_name", "")
        pr_number = pr.get("number")

        if not pr_number:
            continue

        # PRは常にDraftのまま維持（Ready for Reviewは人間が判断して手動で行う）
        print(f"   📋 {repo_name}: PR #{pr_number} は Draft のまま維持")
        draft_prs.append(pr)

    return ready_prs, draft_prs


def _update_prs_with_change_summary(state: WorkflowState) -> WorkflowState:
    """各 PR の本文に対応するリポジトリの変更サマリーを反映する。

    各 PR には対応するリポジトリの変更サマリーのみを反映する。
    失敗してもワークフローは継続する（fail-open）。
    """
    summaries = build_pr_change_summary(state)
    if not summaries:
        logger.info("変更サマリー: 差分なし、スキップ")
        return state

    pull_requests = state.get("pull_requests", [])
    for pr in pull_requests:
        pr_number = pr.get("number")
        if not pr_number:
            continue

        repo_name = pr.get("repo_name", "")
        summary_md = summaries.get(repo_name)
        if not summary_md:
            logger.info(f"PR #{pr_number} ({repo_name}): 変更サマリーなし、スキップ")
            continue

        try:
            git_client = _get_git_client_for_pr(pr)

            # 現在の PR 本文を取得
            current_body = git_client.get_pr_body(pr_number) or ""

            # 変更サマリーセクションを差し替え/追加
            new_body = upsert_section(current_body, "変更サマリー", summary_md)

            # PR 本文を更新
            if git_client.update_pr_body(pr_number, new_body):
                print(f"   📝 {repo_name}: PR #{pr_number} に変更サマリーを反映しました")
                logger.info(f"PR #{pr_number} ({repo_name}) に変更サマリーを反映")
                state = add_audit_log(
                    state, 8, "pr_body_change_summary_updated", "success",
                    {"repo_name": repo_name, "pr_number": pr_number},
                )
            else:
                logger.warning(f"PR #{pr_number} ({repo_name}) の本文更新に失敗")
                state = add_audit_log(
                    state, 8, "pr_body_change_summary_updated", "warning",
                    {"repo_name": repo_name, "pr_number": pr_number},
                    error="update_pr_body returned False",
                )
        except Exception as e:
            logger.warning(f"PR #{pr_number} ({repo_name}) の変更サマリー反映に失敗: {e}")
            state = add_audit_log(
                state, 8, "pr_body_change_summary_updated", "warning",
                {"repo_name": repo_name, "pr_number": pr_number},
                error=str(e),
            )

    return state


def phase8e_ready_for_review_node(state: WorkflowState) -> WorkflowState:
    """Phase 8e: Ready for Review（部分的なPR完了フロー対応）

    C-2: 成功したリポジトリのPRは自動的にReady for Reviewに変更し、
    失敗したリポジトリのPRはDraftのまま維持する。
    """
    print()
    print("📋 PRのReady for Review処理を開始...")
    print()

    # 変更サマリーを生成してPR本文に反映
    state = _update_prs_with_change_summary(state)

    # 成功したリポジトリのPRをReady for Reviewに変更
    ready_prs, draft_prs = _mark_successful_prs_ready(state)

    # 結果を監査ログに記録
    state = add_audit_log(
        state,
        8,
        "ready_for_review_processed",
        "success",
        {
            "ready_prs": [
                {"repo_name": pr["repo_name"], "number": pr["number"]}
                for pr in ready_prs
            ],
            "draft_prs": [
                {"repo_name": pr["repo_name"], "number": pr["number"]}
                for pr in draft_prs
            ],
        },
    )

    print()
    if draft_prs:
        print(f"📋 {len(draft_prs)}件のPRはDraft状態のままです:")
        for pr in draft_prs:
            print(f"   - {pr['repo_name']}: PR #{pr['number']} - {pr.get('url', '')}")

    # 人間レビュー待ちに移行（Ready for Reviewは人間が手動で行う）
    state["waiting_for_human"] = True
    state["human_input_request"] = "human_review"
    print()
    print("⏳ 人間レビュー待ち:")
    print("   1. PRの内容を確認し、Ready for Reviewに変更してください")
    print("   2. レビュー完了後: workflow continue <workflow_id>")

    return state
