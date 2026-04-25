"""
Bitbucket Hosting Client

Bitbucket API を使用してBitbucket操作を行うクライアント。

Note:
    このクライアントはスケルトン実装です。
    本格的に使用する場合は、Bitbucket API の完全な実装が必要です。
"""

from .base import GitHostingClient, PullRequest, ReviewComment


class BitbucketHostingClient(GitHostingClient):
    """Bitbucket API を使用するクライアント（未実装）"""

    def __init__(
        self,
        workspace: str | None = None,
        repo_slug: str | None = None,
    ):
        """
        初期化

        Args:
            workspace: Bitbucket ワークスペース
            repo_slug: リポジトリスラッグ
        """
        self.workspace = workspace
        self.repo_slug = repo_slug

        if not all([workspace, repo_slug]):
            raise ValueError(
                "Bitbucket を使用するには workspace と repo_slug が必要です。"
                "設定ファイルで git_hosting.workspace と git_hosting.repo_slug を指定してください。"
            )

    def get_repo_info(self) -> tuple[str, str]:
        """
        リポジトリ情報を取得

        Returns:
            (workspace, repo_slug) のタプル
        """
        return self.workspace or "", self.repo_slug or ""

    def create_draft_pull_request(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequest:
        """
        Draft プルリクエストを作成

        Note:
            Bitbucket では Draft PR がサポートされていないため、
            通常のPRとして作成されます。
        """
        # TODO: Bitbucket API を使用してPRを作成
        # POST /2.0/repositories/{workspace}/{repo_slug}/pullrequests
        raise NotImplementedError(
            "Bitbucket クライアントは未実装です。"
            "Bitbucket API の実装が必要です。"
        )

    def mark_ready_for_review(self, pr_number: int) -> None:
        """
        Draft PRをReady for Reviewに変更

        Note:
            Bitbucket では Draft PR がサポートされていないため、
            この操作は何もしません。
        """
        pass

    def get_review_comments(
        self,
        pr_number: int,
        exclude_authors: list[str] | None = None,
    ) -> list[ReviewComment]:
        """
        レビューコメントを取得
        """
        # TODO: Bitbucket API を使用してコメントを取得
        # GET /2.0/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}/comments
        raise NotImplementedError(
            "Bitbucket クライアントは未実装です。"
            "Bitbucket API の実装が必要です。"
        )

    def reply_to_comment(
        self,
        pr_number: int,
        comment_id: int,
        body: str,
    ) -> bool:
        """
        コメントに返信
        """
        # TODO: Bitbucket API を使用してコメントに返信
        raise NotImplementedError(
            "Bitbucket クライアントは未実装です。"
            "Bitbucket API の実装が必要です。"
        )

    def resolve_thread(self, thread_id: str) -> bool:
        """
        スレッドを解決

        Note:
            Bitbucket ではスレッド解決の概念が異なります。
        """
        return False

    def get_thread_id_for_comment(
        self,
        pr_number: int,
        comment_id: int,
    ) -> str | None:
        """
        コメントIDからスレッドIDを取得
        """
        return None

    def is_changes_requested(self, pr_number: int) -> bool:
        """
        変更要求があるかどうかを確認
        """
        # TODO: Bitbucket API を使用してレビュー状態を確認
        raise NotImplementedError(
            "Bitbucket クライアントは未実装です。"
            "Bitbucket API の実装が必要です。"
        )

    def get_pr_for_branch(self, branch_name: str) -> PullRequest | None:
        """
        ブランチに紐づくPRを取得
        """
        # TODO: Bitbucket API を使用してPRを検索
        raise NotImplementedError(
            "Bitbucket クライアントは未実装です。"
            "Bitbucket API の実装が必要です。"
        )
