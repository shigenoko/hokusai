"""
Phase 7.5: ブランチ衛生チェック

PRに複数機能やマージ済み変更が混在していないかを検証し、
必要に応じてチェリーピックで整理する。

背景:
複数機能の混在やレビュアーからの整理要請を事前に検出・対応するために追加されたPhase。
"""

import logging
import re

from ..config import get_config
from ..constants import (
    BRANCH_NAME_LIMIT,
    COMMIT_THRESHOLD,
    MAX_DISPLAY_FILES,
    MAX_DISPLAY_FILES_SHORT,
    MERGE_COMMIT_THRESHOLD,
)
from ..integrations.git import GitClient
from ..state import (
    PhaseStatus,
    WorkflowState,
    add_audit_log,
    update_phase_status,
)

logger = logging.getLogger(__name__)


def _extract_task_keywords(task_title: str) -> list[str]:
    """タスクタイトルからキーワードを抽出"""
    keywords = []

    # 日本語キーワード
    japanese_words = re.findall(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]+', task_title)
    keywords.extend(japanese_words)

    # 英語キーワード（推測）
    # 一般的なパターンに対応
    common_mappings = {
        "販売": ["sales", "Sales", "sale"],
        "商品": ["item", "Item", "product"],
        "アイテム": ["item", "Item", "distribution"],
        "一覧": ["list", "management", "Management"],
        "ユーザー": ["user", "User"],
        "管理": ["manage", "Management", "admin"],
        "設定": ["setting", "config", "Settings"],
        "編集": ["edit", "Edit"],
        "作成": ["create", "Create"],
        "削除": ["delete", "Delete"],
    }

    for japanese, english in common_mappings.items():
        if japanese in task_title:
            keywords.extend(english)

    return keywords


def _slugify(text: str) -> str:
    """テキストをブランチ名用にスラッグ化"""
    # 日本語を除去し、英数字とハイフンのみに
    slug = re.sub(r'[^\w\s-]', '', text.lower())
    slug = re.sub(r'[\s_]+', '-', slug)
    return slug[:BRANCH_NAME_LIMIT]  # 最大BRANCH_NAME_LIMIT文字


def _detect_already_merged_commits(
    git: GitClient,
    base_branch: str,
) -> list[tuple[str, str]]:
    """ブランチ上のコミットのうち、既にベースブランチにマージ済みのものを検出

    git cherry を使用して、ベースブランチに同等のコミットが存在するかを判定する。
    git cherry は patch-id ベースで比較し、マージ済みコミットを "-" で表示する。

    Args:
        git: GitClient インスタンス
        base_branch: ベースブランチ名

    Returns:
        マージ済みコミットのリスト [(commit_hash, commit_message), ...]
    """
    success, output = git.run_git_command(
        ["cherry", "-v", f"origin/{base_branch}", "HEAD"]
    )
    if not success or not output:
        return []

    already_merged = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        # git cherry 出力: "- <hash> <message>" (マージ済み) or "+ <hash> <message>" (未マージ)
        if line.startswith("- "):
            parts = line[2:].split(maxsplit=1)
            if len(parts) >= 2:
                already_merged.append((parts[0][:12], parts[1]))
            elif parts:
                already_merged.append((parts[0][:12], "(no message)"))

    return already_merged


def phase7_5_branch_hygiene_node(state: WorkflowState) -> WorkflowState:
    """
    Phase 7.5: ブランチ衛生チェック（実装後の品質検査）

    検出項目:
    1. ベースブランチとの差分ファイル数が想定より多い
    2. コミット履歴に複数の無関係な機能が混在
    3. マージコミットが多すぎる（長期ブランチの兆候）
    4. マージ済みコミットが混入している（rebaseで除外可能）

    責務分離:
    - Phase 1: 開始前ガード（古い base や構造差分のある branch を弾く）
      → .gitmodules/submodule 構造差分、base からの大幅な behind を検出して停止
    - Phase 7.5（本フェーズ）: 実装後の衛生確認
      → 差分ファイル数、コミット履歴、マージコミット数を検査

    Phase 1 で弾くべき問題（構造的な base 不整合）はここまで持ち越さない。
    ここでは「実装が終わった branch の品質」を検査する。

    Note:
        Phase 4 がスキップされた場合（expected_changed_files がない場合）、
        このフェーズも自動的にスキップされます。
    """

    # Phase 4 がスキップされた場合、expected_changed_files がないためスキップ
    if not state.get("expected_changed_files"):
        print("⏭️  Phase 7.5 スキップ: 変更予定ファイル情報なし（Phase 4 スキップ時）")
        return state

    state = update_phase_status(state, 7, PhaseStatus.IN_PROGRESS)  # Phase 7の一部として扱う
    config = get_config()
    base_branch = config.base_branch

    # worktree path を state から取得（worktree 未使用時は config.project_root にフォールバック）
    _repos = state.get("repositories", [])
    _worktree_path = _repos[0].get("path") if _repos else None
    git = GitClient(_worktree_path)

    try:
        issues = []

        # 1. ベースブランチとの差分ファイルを取得
        diff_files = git.get_diff_files(f"origin/{base_branch}", "HEAD")
        if not diff_files:
            # 差分なしまたは取得失敗
            success, _ = git.run_git_command(["diff", "--name-only", f"origin/{base_branch}...HEAD"])
            if not success:
                print("⚠️ git diff 失敗")
                state["branch_hygiene_issues"] = []
                return state

        # 2. タスク関連ファイルのみかチェック
        expected_files = state.get("expected_changed_files", [])
        if expected_files:
            unexpected_files = []
            for f in diff_files:
                is_expected = any(
                    f.startswith(prefix.rstrip("/")) or f == prefix
                    for prefix in expected_files
                )
                if not is_expected:
                    unexpected_files.append(f)

            if unexpected_files:
                issues.append({
                    "type": "unexpected_files",
                    "severity": "warning",
                    "message": f"タスクに関連しないファイルが{len(unexpected_files)}件含まれています",
                    "files": unexpected_files[:MAX_DISPLAY_FILES],  # 最大MAX_DISPLAY_FILES件
                    "recommendation": "チェリーピックで新規ブランチを作成することを推奨",
                })

        # 3. コミット履歴を分析
        commits_output = git.get_log_oneline(branch=f"origin/{base_branch}..HEAD")
        if commits_output:
            commits = [c for c in commits_output.split("\n") if c]
            commit_count = len(commits)
            merge_commits = [c for c in commits if "Merge" in c]

            if commit_count > COMMIT_THRESHOLD:
                issues.append({
                    "type": "too_many_commits",
                    "severity": "info",
                    "message": f"コミット数が{commit_count}件あります",
                    "recommendation": "コミット履歴の整理（squash）を検討してください",
                })

            if len(merge_commits) > MERGE_COMMIT_THRESHOLD:
                issues.append({
                    "type": "many_merges",
                    "severity": "warning",
                    "message": f"マージコミットが{len(merge_commits)}件あります",
                    "recommendation": f"長期ブランチの兆候です。{base_branch}からの新規ブランチを推奨",
                })

        # 4. マージ済みコミットの混入検出
        #    ブランチ上のコミットが既にベースブランチに含まれている場合を検出
        #    (rebaseすれば除外されるコミット = レビュアーに余計な差分を見せてしまう)
        already_merged = _detect_already_merged_commits(git, base_branch)
        if already_merged:
            issues.append({
                "type": "already_merged_commits",
                "severity": "warning",
                "message": f"ベースブランチにマージ済みのコミットが{len(already_merged)}件含まれています",
                "files": [f"{h} {m}" for h, m in already_merged[:MAX_DISPLAY_FILES]],
                "recommendation": f"rebase origin/{base_branch} で除外することを推奨（PRの差分がクリーンになります）",
            })

        # 結果を保存
        state["branch_hygiene_issues"] = issues

        # 問題がある場合はHuman-in-the-loop
        warning_or_error = [i for i in issues if i["severity"] in ["warning", "error"]]

        if warning_or_error:
            state["waiting_for_human"] = True
            state["human_input_request"] = "branch_hygiene"

            print()
            print("╔══════════════════════════════════════════════════════════════════╗")
            print("║  ⚠️  ブランチ衛生問題を検出                                       ║")
            print("╠══════════════════════════════════════════════════════════════════╣")
            for issue in issues:
                severity_icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}[issue["severity"]]
                print(f"║  {severity_icon} {issue['message']}")
                if "files" in issue:
                    for f in issue["files"][:MAX_DISPLAY_FILES_SHORT]:
                        print(f"║     - {f}")
                    if len(issue["files"]) > MAX_DISPLAY_FILES_SHORT:
                        print(f"║     ... 他{len(issue['files']) - MAX_DISPLAY_FILES_SHORT}件")
            print("╠══════════════════════════════════════════════════════════════════╣")
            print("║  対応方法を選択してください:                                       ║")
            print("║                                                                  ║")
            print("║  workflow continue <workflow_id> --action cherry-pick            ║")
            print("║      → チェリーピックで新規ブランチ作成（推奨）                      ║")
            print("║                                                                  ║")
            print("║  workflow continue <workflow_id> --action rebase                  ║")
            print(f"║      → rebase origin/{base_branch} で履歴を整理                              ║")
            print("║                                                                  ║")
            print(f"║  workflow continue <workflow_id> --action merge-{base_branch}             ║")
            print(f"║      → {base_branch}をマージして続行                                       ║")
            print("║                                                                  ║")
            print("║  workflow continue <workflow_id> --action ignore                 ║")
            print("║      → そのまま続行（問題を無視）                                   ║")
            print("╚══════════════════════════════════════════════════════════════════╝")

            state = add_audit_log(state, 7, "hygiene_issues_detected", "warning", {
                "issue_count": len(issues),
                "issues": issues,
            })
        else:
            print("✅ Phase 7.5 完了: ブランチ衛生チェックに合格しました")
            state = add_audit_log(state, 7, "hygiene_check_passed", "success", {
                "diff_file_count": len(diff_files),
            })

    except Exception as e:
        print(f"❌ Phase 7.5 エラー: {e}")
        state["branch_hygiene_issues"] = [{
            "type": "check_error",
            "severity": "error",
            "message": f"衛生チェック実行エラー: {e}",
        }]
        state = update_phase_status(state, 7, PhaseStatus.FAILED, str(e))
        state = add_audit_log(state, 7, "hygiene_check_error", "error", error=str(e))

    return state


def handle_hygiene_action(state: WorkflowState, action: str) -> WorkflowState:
    """
    ユーザーが選択したブランチ衛生対応アクションを実行

    Args:
        state: 現在の状態
        action: "cherry-pick", "rebase", "merge-base", "ignore"
    """
    _VALID_ACTIONS = {"cherry-pick", "rebase", "ignore"}

    # 不正なアクションは待機状態を維持して早期リターン
    if action not in _VALID_ACTIONS and not action.startswith("merge-"):
        logger.warning(f"不明なブランチ衛生アクション: {action!r}")
        print(f"⚠️ 不明なアクション: {action}")
        return state

    config = get_config()
    base_branch = config.base_branch

    # worktree path を state から取得
    _repos = state.get("repositories", [])
    _worktree_path = _repos[0].get("path") if _repos else None
    git = GitClient(_worktree_path)

    # 待機状態をリセット（アクション内部で再設定される場合がある）
    state["waiting_for_human"] = False
    state["human_input_request"] = None

    if action == "cherry-pick":
        state = _auto_cherry_pick_task_commits(state)
    elif action == "rebase":
        state = _rebase_onto_base(state, git, base_branch)
    elif action.startswith("merge-"):
        fetch_ok, _ = git.run_git_command(["fetch", "origin", base_branch])
        if fetch_ok:
            merge_ok, merge_output = git.run_git_command(["merge", f"origin/{base_branch}"])
            if not merge_ok:
                print(f"⚠️ {base_branch}マージ失敗: {merge_output}")
            else:
                print(f"✅ {base_branch}をマージしました")
        else:
            print(f"⚠️ {base_branch}のフェッチに失敗しました")
    elif action == "ignore":
        print("ℹ️ ブランチ衛生問題を無視して続行します")

    return state


def _rebase_onto_base(
    state: WorkflowState,
    git: GitClient,
    base_branch: str,
) -> WorkflowState:
    """
    rebase origin/{base_branch} を実行してブランチ履歴を整理する。

    コンフリクト発生時は rebase を中断し、Human-in-the-loop に戻す。
    """
    fetch_ok, _ = git.run_git_command(["fetch", "origin", base_branch])
    if not fetch_ok:
        print(f"⚠️ {base_branch}のフェッチに失敗しました")
        return state

    rebase_ok, rebase_output = git.run_git_command(
        ["rebase", f"origin/{base_branch}"]
    )
    if rebase_ok:
        print(f"✅ rebase origin/{base_branch} が完了しました")
        state = add_audit_log(state, 7, "rebase_completed", "success", {
            "base_branch": base_branch,
        })
    else:
        # コンフリクト発生 — rebase を中断して安全な状態に戻す
        git.run_git_command(["rebase", "--abort"])
        print(f"⚠️ rebase origin/{base_branch} でコンフリクトが発生しました")
        print(f"   出力: {rebase_output}")
        print()
        print("   以下の方法で対処してください:")
        print(f"   1. 手動で rebase を実行: git rebase origin/{base_branch}")
        print(f"   2. merge に切り替え: workflow continue <id> --action merge-{base_branch}")
        print("   3. 無視して続行: workflow continue <id> --action ignore")
        state["waiting_for_human"] = True
        state["human_input_request"] = "branch_hygiene"
        state = add_audit_log(state, 7, "rebase_conflict", "warning", {
            "base_branch": base_branch,
            "output": rebase_output[:500] if rebase_output else "",
        })

    return state


def _auto_cherry_pick_task_commits(state: WorkflowState) -> WorkflowState:
    """
    タスク関連コミットのみを新規ブランチにチェリーピック
    """
    config = get_config()
    base_branch = config.base_branch

    _repos = state.get("repositories", [])
    _worktree_path = _repos[0].get("path") if _repos else None
    git = GitClient(_worktree_path)

    # 1. 全コミットを取得
    success, commits_output = git.run_git_command(
        ["log", "--oneline", "--reverse", f"origin/{base_branch}..HEAD"]
    )
    if not success:
        print(f"⚠️ コミット取得失敗: {commits_output}")
        return state

    all_commits = [c for c in commits_output.strip().split("\n") if c]

    # 2. タスク関連コミットを特定
    task_keywords = _extract_task_keywords(state.get("task_title", ""))
    task_commits = []

    for commit_line in all_commits:
        parts = commit_line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        commit_hash = parts[0]
        commit_msg = parts[1]

        # Mergeコミットは除外
        if "Merge" in commit_msg:
            continue
        # タスク関連キーワードを含むコミットを選択
        if any(kw.lower() in commit_msg.lower() for kw in task_keywords):
            task_commits.append(commit_hash)

    if not task_commits:
        print("⚠️ タスク関連コミットが特定できませんでした")
        print("   手動でチェリーピックを行ってください")
        return state

    # 3. 新規ブランチ作成
    task_title = state.get("task_title", "task")
    new_branch = f"feature/{_slugify(task_title)}-clean"
    old_branch = state.get("branch_name", "")

    create_ok, create_output = git.run_git_command(
        ["checkout", "-b", new_branch, f"origin/{base_branch}"]
    )
    if not create_ok:
        print(f"⚠️ 新規ブランチ作成失敗: {create_output}")
        # 元のブランチに戻る
        git.run_git_command(["checkout", old_branch])
        return state

    # 4. チェリーピック実行
    print("📋 チェリーピック実行中...")
    for commit_hash in task_commits:
        cp_ok, cp_output = git.cherry_pick(commit_hash)
        if not cp_ok:
            print(f"⚠️ チェリーピック失敗 ({commit_hash}): {cp_output}")
            print("   コンフリクトを解決後、手動で続行してください")
            state["waiting_for_human"] = True
            state["human_input_request"] = "cherry_pick_conflict"
            return state

    # 5. 状態更新
    state["cherry_picked_from"] = old_branch
    state["cherry_picked_commits"] = task_commits
    state["branch_name"] = new_branch

    print("✅ チェリーピック完了")
    print(f"   新ブランチ: {new_branch}")
    print(f"   コミット数: {len(task_commits)}件")

    state = add_audit_log(state, 7, "cherry_pick_completed", "success", {
        "new_branch": new_branch,
        "old_branch": old_branch,
        "commits": task_commits,
    })

    return state


def should_run_hygiene_check(state: WorkflowState) -> str:
    """Phase 7.5をスキップするかどうかを判定"""
    # expected_changed_filesが設定されていない場合はスキップ
    if not state.get("expected_changed_files"):
        return "skip_hygiene"
    return "run_hygiene"
