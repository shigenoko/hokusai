"""
Integration Factory

設定に基づいてクライアントインスタンスを生成するファクトリ。
"""

from ..config import get_config
from .git_hosting.base import GitHostingClient
from .task_backend.base import TaskBackendClient

# シングルトンキャッシュ
_task_client: TaskBackendClient | None = None
_git_hosting_client: GitHostingClient | None = None


def get_task_client() -> TaskBackendClient:
    """
    設定に基づいてタスク管理クライアントを取得

    Returns:
        TaskBackendClient の実装インスタンス

    Raises:
        ValueError: 未知のバックエンドタイプが指定された場合
    """
    global _task_client

    if _task_client is not None:
        return _task_client

    config = get_config()
    backend_type = config.task_backend.type

    if backend_type == "notion":
        from .task_backend.notion import NotionTaskClient

        _task_client = NotionTaskClient()
    elif backend_type == "github_issue":
        from .task_backend.github_issue import GitHubIssueClient

        _task_client = GitHubIssueClient(repo=config.task_backend.repo)
    elif backend_type in ("jira", "linear"):
        raise NotImplementedError(
            f"タスクバックエンド '{backend_type}' は未実装です。"
            f"対応済みバックエンド: notion, github_issue"
        )
    else:
        raise ValueError(f"Unknown task backend type: {backend_type}")

    return _task_client


def get_git_hosting_client() -> GitHostingClient:
    """
    設定に基づいてGitホスティングクライアントを取得

    Returns:
        GitHostingClient の実装インスタンス

    Raises:
        ValueError: 未知のホスティングタイプが指定された場合
    """
    global _git_hosting_client

    if _git_hosting_client is not None:
        return _git_hosting_client

    config = get_config()
    hosting_type = config.git_hosting.type

    if hosting_type == "github":
        from .git_hosting.github import GitHubHostingClient

        _git_hosting_client = GitHubHostingClient()
    elif hosting_type == "gitlab":
        from .git_hosting.gitlab import GitLabHostingClient

        _git_hosting_client = GitLabHostingClient(
            base_url=config.git_hosting.base_url,
            project_path=config.git_hosting.project_path,
        )
    elif hosting_type == "bitbucket":
        raise NotImplementedError(
            f"Gitホスティング '{hosting_type}' は未実装です。"
            f"対応済みバックエンド: github, gitlab"
        )
    else:
        raise ValueError(f"Unknown git hosting type: {hosting_type}")

    return _git_hosting_client


def reset_clients() -> None:
    """
    クライアントキャッシュをリセット（テスト用）
    """
    global _task_client, _git_hosting_client
    _task_client = None
    _git_hosting_client = None
