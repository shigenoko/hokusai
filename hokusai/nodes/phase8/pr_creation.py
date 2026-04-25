"""
Phase 8a: PR作成

Draft PRの作成とNotionへの情報追記を行う。
"""

from pathlib import Path

from ...config import get_config
from ...integrations.git_hosting.github import GitHubHostingClient
from ...logging_config import get_logger
from ...state import (
    PhaseStatus,
    PRStatus,
    WorkflowState,
    add_audit_log,
    update_phase_status,
)
from ...utils.notion_helpers import record_pr_callout_to_notion
from ...utils.repo_resolver import resolve_runtime_repositories
from ...utils.shell import ShellError, ShellRunner
from .pr_lookup import _find_existing_pr

logger = get_logger("phase8")


def _check_branch_exists(repo_path, branch_name: str, git_hosting) -> bool | None:
    """
    ブランチのローカル/リモート存在確認とプッシュ

    Args:
        repo_path: リポジトリパス（Pathオブジェクト）
        branch_name: フィーチャーブランチ名
        git_hosting: GitHubHostingClientインスタンス

    Returns:
        True: ブランチがリモートに存在（またはプッシュ成功）
        False: プッシュ失敗
        None: ブランチがローカルにも存在しない
    """
    if git_hosting.branch_exists_on_remote(branch_name):
        return True

    from ...integrations.git import GitClient
    try:
        git = GitClient(str(repo_path))
        if not git.branch_exists_locally(branch_name):
            return None

        # ローカルにはあるがリモートにない場合はプッシュ
        repo_name = repo_path.name
        print(f"   📤 {repo_name}: ブランチをリモートにプッシュ")
        if git_hosting.push_branch(branch_name):
            print("      ✓ プッシュ完了")
            return True
        else:
            print("      ⚠️ プッシュ失敗")
            return False
    except Exception as e:
        repo_name = repo_path.name
        print(f"   ⚠️ {repo_name}: ブランチ確認エラー - {e}")
        return False


def _create_new_pr(
    state: WorkflowState, repo_path, git_hosting, branch_name: str
) -> dict:
    """
    Draft PRを作成し、PR情報を返す

    重要: PRは必ずDraftで作成する（Openにするのは人間が手動で行う）

    Args:
        state: ワークフロー状態
        repo_path: リポジトリパス（Pathオブジェクト）
        git_hosting: GitHubHostingClientインスタンス
        branch_name: フィーチャーブランチ名

    Returns:
        PR情報dict（pr_url, pr_number を含む）
    """
    config = get_config()

    # ベースブランチを取得（state → config(source_path) → default の順でフォールバック）
    base_branch = None
    for repo_state in state.get("repositories", []):
        if str(repo_state.get("path")) == str(repo_path):
            base_branch = repo_state.get("base_branch")
            break
    if not base_branch:
        # worktree 使用時は source_path で config とマッチさせる
        source_path = None
        for repo_state in state.get("repositories", []):
            if str(repo_state.get("path")) == str(repo_path):
                source_path = repo_state.get("source_path")
                break
        for repo in config.get_all_repositories():
            if (source_path and str(repo.path) == source_path) or \
               str(repo.path) == str(repo_path) or \
               repo.name == str(Path(repo_path).name):
                base_branch = repo.base_branch
                break
    if not base_branch:
        base_branch = config.base_branch

    # PRタイトルとボディを構築
    task_title = state.get("task_title", "")
    task_url = state.get("task_url", "")

    pr_title = task_title or f"feat: {branch_name}"
    pr_body_parts = ["## 概要", "", f"タスク: {task_url}" if task_url else ""]

    # チェリーピック情報を追加
    cherry_picked_from = state.get("cherry_picked_from")
    if cherry_picked_from:
        pr_body_parts.extend([
            "",
            "## Cherry-pick 情報",
            f"- 元ブランチ: `{cherry_picked_from}`",
        ])
        original_pr = git_hosting.get_pr_for_branch(cherry_picked_from)
        if original_pr:
            pr_body_parts.append(f"- 元PR: #{original_pr.number}")
        commits = state.get("cherry_picked_commits", [])
        if commits:
            pr_body_parts.append(f"- コミット数: {len(commits)}")

    pr_body = "\n".join(pr_body_parts)

    # gh pr create --draft を直接実行
    shell = ShellRunner(cwd=repo_path)
    try:
        result = shell.run_gh(
            "pr", "create",
            "--draft",  # 重要: 必ずDraftで作成
            "--title", pr_title,
            "--body", pr_body,
            "--base", base_branch,
            "--head", branch_name,
            check=True,
        )
        pr_url = result.stdout.strip()
        pr_number = int(pr_url.split("/")[-1]) if pr_url else 0
        logger.info(f"Draft PR作成成功: {pr_url}")
        return {"pr_url": pr_url, "pr_number": pr_number}

    except ShellError as e:
        # 既存PRがある場合のエラーハンドリング
        stderr = e.result.stderr or ""
        if "already exists" in stderr.lower():
            logger.warning(f"PR既存: {stderr}")
            # 既存PRを検索して返す
            existing_pr = git_hosting.get_pr_for_branch(branch_name)
            if existing_pr:
                return {"pr_url": existing_pr.url, "pr_number": existing_pr.number}
        raise


def _extract_pr_info_from_result(result, state, repo_name, git_hosting, branch_name):
    """PR作成結果からPR情報（URL/番号/タイトル）を抽出して返す。"""
    import re
    pr_url, pr_number, pr_github_status = result.get("pr_url"), result.get("pr_number"), None

    # URLが取得できなかった場合、再度PRを検索
    if not pr_url:
        created_pr = git_hosting.get_pr_for_branch(branch_name)
        if created_pr:
            pr_url, pr_number = created_pr.url, created_pr.number
            pr_github_status = "draft" if created_pr.draft else created_pr.state
    if not pr_url:
        return None

    try:
        owner, repo = git_hosting.get_repo_info()
    except Exception:
        owner, repo = "", ""

    # PRタイトルを取得
    pr_title = ""
    try:
        pr_info = git_hosting.get_pr_for_branch(branch_name)
        if pr_info:
            pr_title = pr_info.title
            if not pr_number and pr_info.number:
                pr_number = pr_info.number
            if not pr_github_status:
                pr_github_status = "draft" if pr_info.draft else pr_info.state
    except Exception:
        pass

    # PR番号がまだ取得できていない場合はURLから抽出
    if not pr_number and pr_url:
        url_match = re.search(r"/pull/(\d+)", pr_url)
        if url_match:
            pr_number = int(url_match.group(1))
    if not pr_title:
        pr_title = state.get("task_title") or "実装PR"

    return {
        "repo_name": repo_name, "title": pr_title,
        "url": pr_url, "number": pr_number or 0,
        "owner": owner, "repo": repo,
        "status": PRStatus.DRAFT.value,
        "github_status": pr_github_status or "draft",
    }


def _create_pr_for_repository(
    state: WorkflowState, repo_name: str, repo_path,
    base_branch: str, branch_name: str,
) -> dict | None:
    """単一リポジトリのPRを作成または検出し、PR情報の辞書を返す。"""
    repo_path = Path(repo_path)
    if not repo_path.exists():
        print(f"   ⚠️ {repo_name}: パスが存在しません ({repo_path})")
        return None

    git_hosting = GitHubHostingClient(working_dir=repo_path)

    # ブランチのローカル/リモート存在確認
    branch_status = _check_branch_exists(repo_path, branch_name, git_hosting)
    if branch_status is None:
        print(f"   ⏭️ {repo_name}: ブランチ {branch_name} が存在しません（スキップ）")
        return None
    if branch_status is False:
        return None

    # 既存のPRをチェック
    existing = _find_existing_pr(git_hosting, repo_name, branch_name)
    if existing:
        return existing

    # PRが存在しない場合、gh pr create --draft でPRを作成
    print(f"   🔨 {repo_name}: PR作成中...")
    try:
        result = _create_new_pr(state, repo_path, git_hosting, branch_name)
        pr_info = _extract_pr_info_from_result(
            result, state, repo_name, git_hosting, branch_name,
        )
        if pr_info:
            print(f"   ✅ {repo_name}: PR #{pr_info['number']} 作成完了")
            return pr_info
        else:
            print(f"   ⚠️ {repo_name}: PR作成失敗（URLが取得できませんでした）")
    except Exception as e:
        print(f"   ⚠️ {repo_name}: PR作成失敗 - {e}")

    return None


def _trigger_ci_for_new_prs(repos, branch_name: str) -> None:
    """Draft PR 作成後に CI を発火させる。

    Draft PR の opened イベントでは CI がトリガーされないケースがあるため、
    空コミットを push して synchronize イベントを発生させる。
    """
    for repo in repos:
        try:
            shell = ShellRunner(cwd=repo.path)
            shell.run_git(
                "commit", "--allow-empty",
                "-m", "ci: trigger CI checks",
                check=True,
            )
            git_hosting = GitHubHostingClient(working_dir=repo.path)
            if git_hosting.push_branch(branch_name):
                print(f"   🔄 {repo.name}: CI チェックをトリガーしました")
            else:
                logger.warning(f"{repo.name}: CI トリガー用 push 失敗")
        except Exception as e:
            logger.warning(f"{repo.name}: CI トリガー失敗: {e}")


def phase8a_pr_draft_node(state: WorkflowState) -> WorkflowState:
    """Phase 8a: Draft PR作成（複数リポジトリ対応）"""
    state = update_phase_status(state, 8, PhaseStatus.IN_PROGRESS)

    try:
        config = get_config()
        repositories = resolve_runtime_repositories(state, config)
        branch_name = state.get("branch_name", "")

        # ブランチ名が設定されていない場合、最初のリポジトリから取得
        if not branch_name:
            first_repo = repositories[0] if repositories else None
            if first_repo:
                git_hosting = GitHubHostingClient(working_dir=first_repo.path)
                branch_name = git_hosting.get_current_branch()
                if branch_name:
                    state["branch_name"] = branch_name
                    print(f"📋 現在のブランチ: {branch_name}")

        if not branch_name:
            raise ValueError("ブランチ名が設定されていません")

        print(f"📂 PRを作成中... ({len(repositories)}リポジトリ)")

        # 各リポジトリでPRを作成/検出
        pull_requests = state.get("pull_requests", [])
        created_count = 0
        new_pr_repos = []  # CI トリガー対象

        for repo in repositories:
            pr_info = _create_pr_for_repository(
                state=state,
                repo_name=repo.name,
                repo_path=repo.path,
                base_branch=repo.base_branch,
                branch_name=branch_name,
            )

            if pr_info:
                # 既存のリストに追加（重複チェック）
                if not any(pr.get("url") == pr_info["url"] for pr in pull_requests):
                    pull_requests.append(pr_info)
                    created_count += 1
                    new_pr_repos.append(repo)


        state["pull_requests"] = pull_requests

        # Draft PR 作成後に CI を発火させる
        # Draft PR の opened イベントでは CI がトリガーされないため、
        # 空コミットを push して synchronize イベントを発生させる
        if new_pr_repos:
            _trigger_ci_for_new_prs(new_pr_repos, branch_name)

        # NotionタスクにPR情報を追記（差分がある場合のみ）
        if created_count > 0:
            state = record_pr_callout_to_notion(state, phase=8)

        state = add_audit_log(
            state,
            8,
            "draft_pr_created",
            "success",
            {
                "repositories_processed": len(repositories),
                "prs_created": created_count,
                "pull_requests": [
                    {"repo_name": pr["repo_name"], "url": pr["url"], "number": pr["number"]}
                    for pr in pull_requests
                ],
                "cherry_picked_from": state.get("cherry_picked_from"),
            },
        )

        if pull_requests:
            print(f"✅ Phase 8a 完了: {len(pull_requests)}件のPRを作成/検出しました")
            for pr in pull_requests:
                print(f"   📋 {pr['repo_name']}: PR #{pr['number']} - {pr['url']}")
        else:
            print("⚠️ Phase 8a: PRが作成されませんでした（変更がないか、ブランチが存在しません）")

        # Note: Copilotレビュー待ちは統合レビューループ（phase8b_unified_wait）で処理する。
        # Phase 8a はPR作成のみを担当し、後続の phase8b_unified_wait へ進む。

    except Exception as e:
        state = update_phase_status(state, 8, PhaseStatus.FAILED, str(e))
        state = add_audit_log(state, 8, "phase_failed", "error", error=str(e))
        print(f"❌ Phase 8a 失敗: {e}")
        raise

    # Phase 8 (PR作成) を完了にして current_phase を 9 に進める
    state = update_phase_status(state, 8, PhaseStatus.COMPLETED)

    return state
