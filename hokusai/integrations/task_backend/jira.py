"""
Jira Task Client

Jira REST API を使用してJiraタスクを管理する。

Note:
    このクライアントはスケルトン実装です。
    本格的に使用する場合は、Jira REST API の完全な実装が必要です。
"""

from typing import Any

from .base import TaskBackendClient


class JiraTaskClient(TaskBackendClient):
    """Jira をタスク管理として使用するクライアント（未実装）"""

    def __init__(
        self,
        base_url: str | None = None,
        project_key: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
    ):
        """
        初期化

        Args:
            base_url: Jira のベースURL（例: https://your-domain.atlassian.net）
            project_key: プロジェクトキー（例: PROJ）
            email: Jira ユーザーのメールアドレス
            api_token: Jira API トークン
        """
        self.base_url = base_url
        self.project_key = project_key
        self.email = email
        self.api_token = api_token

        if not all([base_url, project_key]):
            raise ValueError(
                "Jira を使用するには base_url と project_key が必要です。"
                "設定ファイルで task_backend.base_url と task_backend.project_key を指定してください。"
            )

    def fetch_task(self, task_url: str) -> dict[str, Any]:
        """
        Jira Issue の情報を取得

        Args:
            task_url: Jira IssueのURLまたはキー（例: PROJ-123）

        Returns:
            タスク情報の辞書
        """
        # TODO: Jira REST API を使用して Issue を取得
        # GET /rest/api/3/issue/{issueIdOrKey}
        raise NotImplementedError(
            "Jira クライアントは未実装です。"
            "jira-python パッケージのインストールと実装が必要です。"
        )

    def update_status(self, task_url: str, status: str) -> None:
        """
        Jira Issue のステータスを更新

        Args:
            task_url: Jira IssueのURL
            status: 新しいステータス
        """
        # TODO: Jira REST API を使用してステータスを更新
        # POST /rest/api/3/issue/{issueIdOrKey}/transitions
        raise NotImplementedError(
            "Jira クライアントは未実装です。"
            "jira-python パッケージのインストールと実装が必要です。"
        )

    def append_progress(self, task_url: str, content: str) -> None:
        """
        Jira Issue にコメントを追加

        Args:
            task_url: Jira IssueのURL
            content: 追記する内容
        """
        # TODO: Jira REST API を使用してコメントを追加
        # POST /rest/api/3/issue/{issueIdOrKey}/comment
        raise NotImplementedError(
            "Jira クライアントは未実装です。"
            "jira-python パッケージのインストールと実装が必要です。"
        )

    def prepend_content(self, task_url: str, content: str) -> None:
        """
        Jira Issue の説明フィールドの先頭にコンテンツを追記

        Args:
            task_url: Jira IssueのURL
            content: 先頭に追記する内容
        """
        # TODO: Jira REST API を使用して説明フィールドを更新
        # PUT /rest/api/3/issue/{issueIdOrKey} with description field
        raise NotImplementedError(
            "Jira クライアントは未実装です。"
            "jira-python パッケージのインストールと実装が必要です。"
        )
