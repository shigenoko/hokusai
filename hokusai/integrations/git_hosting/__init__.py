"""
Git Hosting Integrations

Gitホスティングサービス（GitHub, GitLab, Bitbucket等）の抽象化レイヤー。
"""

from .base import GitHostingClient, PullRequest, ReviewComment
from .bitbucket import BitbucketHostingClient
from .github import GitHubHostingClient
from .gitlab import GitLabHostingClient

__all__ = [
    "GitHostingClient",
    "PullRequest",
    "ReviewComment",
    "GitHubHostingClient",
    "GitLabHostingClient",
    "BitbucketHostingClient",
]
