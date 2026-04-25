"""
Phase 8c/8d/8g/8h: レビュー確認と修正

Copilot/人間レビューの指摘確認と修正フローを行う。

統合レビューループ:
- 全レビューワー（Copilot, 人間）のコメントを一括処理
- レビューの順序に依存しない設計
"""

from ...integrations.factory import get_git_hosting_client
from ...state import (
    PRStatus,
    ReviewComment,
    WorkflowState,
    add_audit_log,
    get_current_pr,
    update_pr_in_list,
)
from .pr_lookup import _get_git_client_for_pr


def _check_review_comments(
    state: WorkflowState,
    pr: dict | None,
    exclude_authors: list[str] | None = None,
    check_changes_requested: bool = False,
) -> tuple[list, bool, int, bool]:
    """Check review comments and determine pass/fail.

    Args:
        state: ワークフロー状態
        pr: PR情報（Noneの場合はstate fallback）
        exclude_authors: 除外する著者リスト（get_review_commentsに渡す）
        check_changes_requested: CHANGES_REQUESTEDステータスも確認するか

    Returns:
        (comments, passed, unreplied_count, changes_requested)
    """
    if pr:
        pr_number = pr.get("number")
        git_hosting = _get_git_client_for_pr(pr)
    else:
        current_pr = get_current_pr(state)
        pr_number = current_pr.get("number") if current_pr else None
        git_hosting = get_git_hosting_client()

    # コメントを取得
    if exclude_authors:
        all_comments = git_hosting.get_review_comments(
            pr_number, exclude_authors=exclude_authors
        )
    else:
        all_comments = git_hosting.get_review_comments(pr_number)

    # 既存のコメント情報を取得（返信済み・解決済みを引き継ぐ）
    # exclude_authorsがない場合はcopilot用、ある場合はhuman用
    is_copilot = exclude_authors is None
    if pr:
        existing_comments_list = pr.get(
            "copilot_comments" if is_copilot else "human_comments", []
        )
        existing_issue_list = pr.get("issue_comments", [])
    else:
        existing_comments_list = state.get(
            "copilot_review_comments" if is_copilot else "human_review_comments", []
        )
        existing_issue_list = state.get("issue_comments", [])
    existing_comments = {c.get("id"): c for c in existing_comments_list}
    existing_issue = {c.get("id"): c for c in existing_issue_list}

    # コメントをReviewComment形式に変換
    comments: list[ReviewComment] = []
    for comment in all_comments:
        # Copilotモードの場合はCopilotコメントのみフィルタ
        if is_copilot and "copilot" not in comment.author.lower():
            continue

        comment_id = comment.id
        existing = existing_comments.get(comment_id, {})
        comments.append(
            ReviewComment(
                id=comment_id,
                thread_id=existing.get("thread_id"),
                body=comment.body,
                path=comment.path,
                line=comment.line,
                author=comment.author,
                replied=existing.get("replied", False),
                resolved=existing.get("resolved", False),
                fix_summary=existing.get("fix_summary"),
            )
        )

    # issue comment の取得・保存は human フェーズ（exclude_authors あり）でのみ行う
    # Copilot フェーズでは issue comment を処理しない（段階別フローの互換性維持）
    issue_comments: list[ReviewComment] = []
    if not is_copilot:
        issue_comments_raw = git_hosting.get_issue_comments(
            pr_number, exclude_authors=exclude_authors
        )
        for comment in issue_comments_raw:
            comment_id = comment.id
            existing = existing_issue.get(comment_id, {})
            issue_comments.append(
                ReviewComment(
                    id=comment_id,
                    thread_id=None,
                    body=comment.body,
                    path=None,
                    line=None,
                    author=comment.author,
                    replied=existing.get("replied", False) or comment.replied,
                    resolved=existing.get("resolved", False) or comment.replied,
                    fix_summary=existing.get("fix_summary"),
                    comment_type="issue",
                )
            )

        # issue comment を state に保存
        if pr:
            from ...state import update_pr_in_list as _upl
            _upl(state, pr["url"], {"issue_comments": issue_comments})
        state["issue_comments"] = issue_comments

    # 未返信のコメント数をカウント
    unreplied_count = sum(1 for c in comments if not c.get("replied"))
    # human フェーズでは issue comment も加算
    if not is_copilot:
        unreplied_count += sum(1 for c in issue_comments if not c.get("replied"))

    # CHANGES_REQUESTEDステータスの確認
    changes_requested = False
    if check_changes_requested:
        changes_requested = git_hosting.is_changes_requested(pr_number)

    # pass/fail判定
    passed = (unreplied_count == 0) and not changes_requested

    return comments, passed, unreplied_count, changes_requested


def phase8c_copilot_check_node(state: WorkflowState) -> WorkflowState:
    """Phase 8c: Copilot指摘確認"""
    # 現在のPRを取得（複数PR対応）
    current_pr = get_current_pr(state)
    if current_pr:
        pr_number = current_pr.get("number")
        pr_display = f"PR #{pr_number} ({current_pr.get('repo_name', 'Backend')})"
    else:
        pr_number = None
        pr_display = "PR (不明)"

    try:
        copilot_comments, passed, unreplied_count, _ = _check_review_comments(
            state, current_pr,
        )

        # コメントを保存（複数PR対応）
        if current_pr:
            state = update_pr_in_list(state, current_pr["url"], {
                "copilot_comments": copilot_comments,
            })
        state["copilot_review_comments"] = copilot_comments

        if not passed:
            # 未対応の指摘あり
            state["copilot_review_passed"] = False
            state["copilot_fix_requested"] = True
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {
                    "copilot_review_passed": False,
                })

            print(f"📝 {pr_display} Copilot指摘あり: {unreplied_count}件（未対応）")
            all_unreplied = [c for c in copilot_comments if not c.get("replied")]
            all_unreplied += [c for c in state.get("issue_comments", []) if not c.get("replied")]
            for i, comment in enumerate(all_unreplied[:3], 1):
                body_preview = comment["body"][:80].replace("\n", " ")
                if comment.get("comment_type") == "issue":
                    print(f"   {i}. [PR全体] {body_preview}...")
                else:
                    path = comment.get("path", "")
                    line = comment.get("line", "")
                    print(f"   {i}. [{path}:{line}] {body_preview}...")

        else:
            # 全て対応済み
            state["copilot_review_passed"] = True
            state["copilot_fix_requested"] = False
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {
                    "copilot_review_passed": True,
                })

            print(f"✅ {pr_display} Copilot指摘なし（または全て対応済み）")

        state = add_audit_log(
            state,
            8,
            "copilot_check",
            "success",
            {
                "total_comments": len(copilot_comments),
                "unreplied_count": unreplied_count,
                "passed": state["copilot_review_passed"],
            },
        )

    except Exception as e:
        # エラーの場合は指摘なしとして続行
        print(f"⚠️ Copilotレビュー確認エラー（続行）: {e}")
        state["copilot_review_passed"] = True
        state["copilot_fix_requested"] = False

    return state


def phase8d_copilot_fix_node(state: WorkflowState) -> WorkflowState:
    """Phase 8d: Copilot指摘修正（自動修正 → Human-in-the-loopフォールバック）"""
    # 現在のPRを取得（複数PR対応）
    current_pr = get_current_pr(state)
    if current_pr:
        pr_display = f"PR #{current_pr.get('number')} ({current_pr.get('repo_name', 'Backend')})"
        pr_url = current_pr.get("url", "")
    else:
        pr_display = "PR (不明)"
        pr_url = ""

    # 修正前のコミット数を記録（プッシュ検証用）
    if current_pr:
        git_hosting = _get_git_client_for_pr(current_pr)
        commit_count = git_hosting.get_pr_commit_count(current_pr.get("number"))
        if commit_count is not None:
            state = update_pr_in_list(state, current_pr["url"], {
                "commit_count_before_fix": commit_count,
            })

    # 自動修正を試行
    auto_fix_attempts = state.get("auto_fix_attempts", 0)
    MAX_AUTO_FIX = 2

    if auto_fix_attempts < MAX_AUTO_FIX and current_pr:
        copilot_comments = state.get("copilot_review_comments", [])
        issue_comments = state.get("issue_comments", [])
        unreplied = [c for c in copilot_comments + issue_comments if not c.get("replied")]
        if unreplied:
            from .review_fix import _auto_fix_review_comments
            success = _auto_fix_review_comments(state, current_pr, unreplied)
            state["auto_fix_attempts"] = auto_fix_attempts + 1
            if success:
                state["waiting_for_human"] = False
                state["human_input_request"] = "copilot_fix"
                return state

    # 自動修正失敗 or リトライ上限 → 人間にフォールバック
    state["waiting_for_human"] = True
    state["human_input_request"] = "copilot_fix"

    print()
    print(f"🔧 {pr_display} Copilot指摘の修正が必要です:")
    if pr_url:
        print(f"   PR: {pr_url}")
    print("   1. 指摘内容を確認し、修正を行ってください")
    print("   2. 修正をコミット＆プッシュしてください")
    print("   3. 修正完了後: workflow continue <workflow_id>")
    print()
    print("   ※ 続行すると再度Copilotレビューを待機します")

    return state


def phase8g_human_check_node(state: WorkflowState) -> WorkflowState:
    """Phase 8g: 人間レビュー指摘確認"""
    # 現在のPRを取得（複数PR対応）
    current_pr = get_current_pr(state)
    if current_pr:
        pr_number = current_pr.get("number")
        pr_display = f"PR #{pr_number} ({current_pr.get('repo_name', 'Backend')})"
    else:
        pr_number = None
        pr_display = "PR (不明)"

    try:
        human_comments, passed, unreplied_count, changes_requested = _check_review_comments(
            state, current_pr,
            exclude_authors=["copilot"],
            check_changes_requested=True,
        )

        # コメントを保存（複数PR対応）
        if current_pr:
            state = update_pr_in_list(state, current_pr["url"], {
                "human_comments": human_comments,
                "human_review_passed": False,  # 後で更新
            })
        state["human_review_comments"] = human_comments

        if not passed:
            # 指摘あり
            state["human_review_passed"] = False
            state["human_fix_requested"] = True
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {
                    "human_review_passed": False,
                    "status": PRStatus.CHANGES_REQUESTED.value,
                })

            print(f"📝 {pr_display} 人間レビュー指摘あり: {unreplied_count}件（未対応）")
            if changes_requested:
                print("   ※ CHANGES_REQUESTED ステータス")
            all_unreplied = [c for c in human_comments if not c.get("replied")]
            all_unreplied += [c for c in state.get("issue_comments", []) if not c.get("replied")]
            for i, comment in enumerate(all_unreplied[:3], 1):
                body_preview = comment["body"][:80].replace("\n", " ")
                if comment.get("comment_type") == "issue":
                    print(f"   {i}. [PR全体] {body_preview}...")
                else:
                    path = comment.get("path", "")
                    line = comment.get("line", "")
                    print(f"   {i}. [{path}:{line}] {body_preview}...")

        else:
            # 全て対応済み
            state["human_review_passed"] = True
            state["human_fix_requested"] = False
            if current_pr:
                state = update_pr_in_list(state, current_pr["url"], {
                    "human_review_passed": True,
                    "status": PRStatus.APPROVED.value,
                })

            print(f"✅ {pr_display} 人間レビュー完了（指摘なし or 承認済み）")

        state = add_audit_log(
            state,
            8,
            "human_check",
            "success",
            {
                "total_comments": len(human_comments),
                "unreplied_count": unreplied_count,
                "changes_requested": changes_requested,
                "passed": state["human_review_passed"],
            },
        )

    except Exception as e:
        # エラーの場合はユーザーに確認
        print(f"⚠️ 人間レビュー確認エラー: {e}")
        print("   指摘がある場合は workflow continue で修正フローに進みます")
        state["human_review_passed"] = False
        state["human_fix_requested"] = True

    return state


def phase8h_human_fix_node(state: WorkflowState) -> WorkflowState:
    """Phase 8h: 人間レビュー指摘修正（自動修正 → Human-in-the-loopフォールバック）"""
    # 現在のPRを取得（複数PR対応）
    current_pr = get_current_pr(state)
    if current_pr:
        pr_display = f"PR #{current_pr.get('number')} ({current_pr.get('repo_name', 'Backend')})"
        pr_url = current_pr.get("url", "")
    else:
        pr_display = "PR (不明)"
        pr_url = ""

    # 修正前のコミット数を記録（プッシュ検証用）
    if current_pr:
        git_hosting = _get_git_client_for_pr(current_pr)
        commit_count = git_hosting.get_pr_commit_count(current_pr.get("number"))
        if commit_count is not None:
            state = update_pr_in_list(state, current_pr["url"], {
                "commit_count_before_fix": commit_count,
            })

    # 自動修正を試行
    auto_fix_attempts = state.get("auto_fix_attempts", 0)
    MAX_AUTO_FIX = 2

    if auto_fix_attempts < MAX_AUTO_FIX and current_pr:
        human_comments = state.get("human_review_comments", [])
        issue_comments = state.get("issue_comments", [])
        unreplied = [c for c in human_comments + issue_comments if not c.get("replied")]
        if unreplied:
            from .review_fix import _auto_fix_review_comments
            success = _auto_fix_review_comments(state, current_pr, unreplied)
            state["auto_fix_attempts"] = auto_fix_attempts + 1
            if success:
                state["waiting_for_human"] = False
                state["human_input_request"] = "human_fix"
                return state

    # 自動修正失敗 or リトライ上限 → 人間にフォールバック
    state["waiting_for_human"] = True
    state["human_input_request"] = "human_fix"

    print()
    print(f"🔧 {pr_display} 人間レビュー指摘の修正が必要です:")
    if pr_url:
        print(f"   PR: {pr_url}")
    print("   1. 指摘内容を確認し、修正を行ってください")
    print("   2. 修正をコミット＆プッシュしてください")
    print("   3. 修正完了後: workflow continue <workflow_id>")
    print()
    print("   ※ 続行すると再度人間レビューを待機します")

    return state


# === 統合レビューループ（順不同対応） ===


def _check_all_review_comments(
    state: WorkflowState,
    pr: dict | None,
) -> tuple[list, list, list, bool, int, bool]:
    """Check ALL review comments (Copilot + Human + Issue) regardless of order.

    統合レビューループ用: 全レビューワーのコメントを一括取得・処理する。

    Args:
        state: ワークフロー状態
        pr: PR情報（Noneの場合はstate fallback）

    Returns:
        (copilot_comments, human_comments, issue_comments, passed, unreplied_count, changes_requested)
    """
    if pr:
        pr_number = pr.get("number")
        git_hosting = _get_git_client_for_pr(pr)
    else:
        current_pr = get_current_pr(state)
        pr_number = current_pr.get("number") if current_pr else None
        git_hosting = get_git_hosting_client()

    # 全コメントを取得（フィルタなし）
    all_comments = git_hosting.get_review_comments(pr_number)

    # issue comment を取得
    issue_comments_raw = git_hosting.get_issue_comments(pr_number)

    # 既存のコメント情報を取得
    if pr:
        existing_copilot = {c.get("id"): c for c in pr.get("copilot_comments", [])}
        existing_human = {c.get("id"): c for c in pr.get("human_comments", [])}
        existing_issue = {c.get("id"): c for c in pr.get("issue_comments", [])}
    else:
        existing_copilot = {c.get("id"): c for c in state.get("copilot_review_comments", [])}
        existing_human = {c.get("id"): c for c in state.get("human_review_comments", [])}
        existing_issue = {c.get("id"): c for c in state.get("issue_comments", [])}

    # コメントを種類別に分類
    copilot_comments: list[ReviewComment] = []
    human_comments: list[ReviewComment] = []

    for comment in all_comments:
        comment_id = comment.id
        is_copilot = "copilot" in comment.author.lower()

        if is_copilot:
            existing = existing_copilot.get(comment_id, {})
        else:
            existing = existing_human.get(comment_id, {})

        # GitHub API から取得した最新の replied 状態を優先
        # （返信コメントが削除された場合に正しく反映するため）
        github_replied = comment.replied

        review_comment = ReviewComment(
            id=comment_id,
            thread_id=existing.get("thread_id"),
            body=comment.body,
            path=comment.path,
            line=comment.line,
            author=comment.author,
            replied=github_replied,
            resolved=github_replied,  # replied と連動（返信なし→未解決）
            fix_summary=existing.get("fix_summary") if github_replied else None,
        )

        if is_copilot:
            copilot_comments.append(review_comment)
        else:
            human_comments.append(review_comment)

    # issue comment をReviewComment形式に変換
    issue_comments: list[ReviewComment] = []
    for comment in issue_comments_raw:
        comment_id = comment.id
        existing = existing_issue.get(comment_id, {})
        # GitHub API から取得した最新の replied 状態を優先
        # （返信コメントが削除された場合に正しく反映するため）
        github_replied = comment.replied

        issue_comments.append(
            ReviewComment(
                id=comment_id,
                thread_id=None,
                body=comment.body,
                path=None,
                line=None,
                author=comment.author,
                replied=github_replied,
                resolved=github_replied,
                fix_summary=existing.get("fix_summary") if github_replied else None,
                comment_type="issue",
            )
        )

    # 未返信のコメント数をカウント（全種類）
    unreplied_copilot = sum(1 for c in copilot_comments if not c.get("replied"))
    unreplied_human = sum(1 for c in human_comments if not c.get("replied"))
    unreplied_issue = sum(1 for c in issue_comments if not c.get("replied"))
    unreplied_count = unreplied_copilot + unreplied_human + unreplied_issue

    # CHANGES_REQUESTEDステータスの確認
    changes_requested = git_hosting.is_changes_requested(pr_number)

    # pass/fail判定
    passed = (unreplied_count == 0) and not changes_requested

    return copilot_comments, human_comments, issue_comments, passed, unreplied_count, changes_requested


def phase8c_unified_check_node(state: WorkflowState) -> WorkflowState:
    """Phase 8c: 統合レビュー指摘確認（Copilot/人間 順不同）

    全レビューワーのコメントを一括で確認し、未対応の指摘があれば修正フローへ。
    """
    # 現在のPRを取得（複数PR対応）
    current_pr = get_current_pr(state)
    if current_pr:
        pr_number = current_pr.get("number")
        pr_display = f"PR #{pr_number} ({current_pr.get('repo_name', 'Backend')})"
    else:
        pr_number = None
        pr_display = "PR (不明)"

    try:
        copilot_comments, human_comments, issue_comments, passed, unreplied_count, changes_requested = \
            _check_all_review_comments(state, current_pr)

        # コメントを保存（複数PR対応）
        if current_pr:
            state = update_pr_in_list(state, current_pr["url"], {
                "copilot_comments": copilot_comments,
                "human_comments": human_comments,
                "issue_comments": issue_comments,
            })
        state["copilot_review_comments"] = copilot_comments
        state["human_review_comments"] = human_comments
        state["issue_comments"] = issue_comments

        # 未対応Copilotコメント数
        unreplied_copilot = sum(1 for c in copilot_comments if not c.get("replied"))
        unreplied_human = sum(1 for c in human_comments if not c.get("replied"))
        unreplied_issue = sum(1 for c in issue_comments if not c.get("replied"))

        # レビュー状態を表示（自動判定はせず、人間に委ねる）
        state["copilot_review_passed"] = unreplied_copilot == 0
        state["human_review_passed"] = unreplied_human == 0 and not changes_requested

        if current_pr:
            state = update_pr_in_list(state, current_pr["url"], {
                "copilot_review_passed": state["copilot_review_passed"],
                "human_review_passed": state["human_review_passed"],
            })

        if not passed:
            print(f"📝 {pr_display} レビュー指摘あり:")
            if unreplied_copilot > 0:
                print(f"   - Copilot: {unreplied_copilot}件（未対応）")
            if unreplied_human > 0:
                print(f"   - 人間: {unreplied_human}件（未対応）")
            if unreplied_issue > 0:
                print(f"   - PR全体: {unreplied_issue}件（未対応）")
            if changes_requested:
                print("   ※ CHANGES_REQUESTED ステータス")

            # 未対応コメントをプレビュー表示
            all_unreplied = [c for c in copilot_comments + human_comments + issue_comments if not c.get("replied")]
            for i, comment in enumerate(all_unreplied[:3], 1):
                body_preview = comment["body"][:80].replace("\n", " ")
                comment_type = comment.get("comment_type", "review")
                if comment_type == "issue":
                    print(f"   {i}. [PR全体] {body_preview}...")
                else:
                    path = comment.get("path", "")
                    line = comment.get("line", "")
                    print(f"   {i}. [{path}:{line}] {body_preview}...")
        else:
            print(f"✅ {pr_display} 全コメント対応済み")

        if not passed:
            # 指摘あり → 自動修正に直接流す（人間待機しない）
            state["review_fix_requested"] = True
        else:
            # 全コメント対応済み → 人間に判断を委ねる
            state["review_fix_requested"] = False
            state["waiting_for_human"] = True
            state["human_input_request"] = "review_status"

        state = add_audit_log(
            state,
            8,
            "unified_review_check",
            "success",
            {
                "copilot_comments": len(copilot_comments),
                "human_comments": len(human_comments),
                "issue_comments": len(issue_comments),
                "unreplied_copilot": unreplied_copilot,
                "unreplied_human": unreplied_human,
                "unreplied_issue": unreplied_issue,
                "changes_requested": changes_requested,
                "passed": passed,
            },
        )

    except Exception as e:
        # エラーの場合は指摘ありとして続行
        print(f"⚠️ レビュー確認エラー（続行）: {e}")
        state["review_fix_requested"] = True

    return state


def phase8d_unified_fix_node(state: WorkflowState) -> WorkflowState:
    """Phase 8d: 統合レビュー指摘修正（自動修正 → Human-in-the-loopフォールバック）

    Copilot/人間 の指摘を一括で修正する。
    自動修正に成功した場合は waiting_for_human を設定せず次のノードへ進む。
    """
    # 現在のPRを取得（複数PR対応）
    current_pr = get_current_pr(state)
    if current_pr:
        pr_display = f"PR #{current_pr.get('number')} ({current_pr.get('repo_name', 'Backend')})"
        pr_url = current_pr.get("url", "")
    else:
        pr_display = "PR (不明)"
        pr_url = ""

    # 修正前のコミット数を記録（プッシュ検証用）
    if current_pr:
        git_hosting = _get_git_client_for_pr(current_pr)
        commit_count = git_hosting.get_pr_commit_count(current_pr.get("number"))
        if commit_count is not None:
            state = update_pr_in_list(state, current_pr["url"], {
                "commit_count_before_fix": commit_count,
            })

    # 未対応コメント数を集計
    copilot_comments = state.get("copilot_review_comments", [])
    human_comments = state.get("human_review_comments", [])
    issue_comments = state.get("issue_comments", [])
    unreplied_copilot = sum(1 for c in copilot_comments if not c.get("replied"))
    unreplied_human = sum(1 for c in human_comments if not c.get("replied"))
    unreplied_issue = sum(1 for c in issue_comments if not c.get("replied"))

    # 自動修正を試行（連続試行回数チェック）
    auto_fix_attempts = state.get("auto_fix_attempts", 0)
    MAX_AUTO_FIX = 2

    if auto_fix_attempts < MAX_AUTO_FIX and current_pr:
        all_unreplied = [c for c in copilot_comments + human_comments + issue_comments if not c.get("replied")]
        if all_unreplied:
            from .review_fix import _auto_fix_review_comments
            success = _auto_fix_review_comments(state, current_pr, all_unreplied)
            state["auto_fix_attempts"] = auto_fix_attempts + 1
            if success:
                # 自動修正成功 → 人間待機を解除し自動フローへ
                state["waiting_for_human"] = False
                state["human_input_request"] = "review_fix"
                return state

    # 自動修正失敗 or リトライ上限 → 人間にフォールバック
    # auto_fix_attempts はリセットしない（8b_wait で返信判定に使用、
    # リセットは「自動修正を実行」ボタンが担当）
    state["waiting_for_human"] = True
    state["human_input_request"] = "review_fix"

    print()
    print(f"🔧 {pr_display} レビュー指摘の修正が必要です:")
    if pr_url:
        print(f"   PR: {pr_url}")
    if unreplied_copilot > 0:
        print(f"   - Copilot指摘: {unreplied_copilot}件")
    if unreplied_human > 0:
        print(f"   - 人間指摘: {unreplied_human}件")
    if unreplied_issue > 0:
        print(f"   - PR全体への指摘: {unreplied_issue}件")
    print()
    print("   1. 指摘内容を確認し、修正を行ってください")
    print("   2. 修正をコミット＆プッシュしてください")
    print("   3. 修正完了後: hokusai continue <workflow_id>")
    print()
    print("   ※ 続行すると再度レビュー確認を行います")

    return state
