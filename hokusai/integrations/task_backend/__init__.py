"""
Task Backend Integrations

タスク管理バックエンド（Notion, GitHub Issue, Jira等）の抽象化レイヤー。
"""

from .base import TaskBackendClient
from .github_issue import GitHubIssueClient
from .jira import JiraTaskClient
from .linear import LinearTaskClient
from .notion import NotionTaskClient

__all__ = [
    "TaskBackendClient",
    "NotionTaskClient",
    "GitHubIssueClient",
    "JiraTaskClient",
    "LinearTaskClient",
]
