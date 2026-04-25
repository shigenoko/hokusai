"""
Phase 8: 既存PR検索

ブランチに紐づく既存PRの検索機能。
"""

from ...integrations.factory import get_git_hosting_client
from ...integrations.git_hosting.github import GitHubHostingClient
from ...state import PRStatus, PullRequestInfo


def _get_git_client_for_pr(pr: PullRequestInfo) -> GitHubHostingClient:
    """
    PRに対応するGitクライアントを取得

    Args:
        pr: PR情報

    Returns:
        GitHubHostingクライアント
    """
    owner = pr.get("owner")
    repo = pr.get("repo")
    if owner and repo:
        return GitHubHostingClient(owner=owner, repo=repo)
    return get_git_hosting_client()


def _find_existing_pr(
    git_hosting, repo_name: str, branch_name: str
) -> dict | None:
    """
    既存PRを検索し、PR情報を返す

    Args:
        git_hosting: GitHubHostingClientインスタンス
        repo_name: リポジトリ表示名
        branch_name: フィーチャーブランチ名

    Returns:
        PR情報の辞書（既存PRがない場合はNone）
    """
    existing_pr = git_hosting.get_pr_for_branch(branch_name)
    if not existing_pr:
        return None

    print(f"   📋 {repo_name}: 既存PR検出 #{existing_pr.number}")
    pr_github_status = "draft" if existing_pr.draft else existing_pr.state

    try:
        owner, repo = git_hosting.get_repo_info()
    except Exception:
        owner, repo = "", ""

    return {
        "repo_name": repo_name,
        "title": existing_pr.title,
        "url": existing_pr.url,
        "number": existing_pr.number,
        "owner": owner,
        "repo": repo,
        "status": PRStatus.DRAFT.value,
        "github_status": pr_github_status or "draft",
    }
