"""Pull Requests DB ドメインクライアント

Phase 8a での PR 作成時に Notion Pull Requests DB へ初期レコードを作る。

初期スコープ:
- PR Number、URL、Repository、Status（初期値）、Created At、Workflow（リレーション）の作成

スコープ外:
- Approved / Merged / Closed の追跡（GitLab polling または webhook が必要なため別フェーズ）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient

logger = get_logger("integrations.notion_dashboard.pull_requests_db")


class PullRequestsDBClient:
    """Notion Pull Requests DB へのページ作成を担当する。"""

    def __init__(self, api: NotionAPIClient, database_id: str):
        if not database_id:
            raise ValueError("Pull Requests DB の database_id は必須です")
        self._api = api
        self._database_id = database_id

    def create_record(
        self,
        *,
        pr_number: int,
        url: str,
        repository: str | None = None,
        workflow_page_id: str | None = None,
        status: str = "Draft",
        created_at: str | None = None,
    ) -> dict:
        """PR 作成時に Notion Pull Requests DB にレコードを作成する。

        Args:
            pr_number: PR 番号
            url: PR URL
            repository: リポジトリ名（Backend / Frontend 等）
            workflow_page_id: 関連ワークフローの Notion page ID（リレーション用）
            status: 初期 Status（既定 "Draft"）
            created_at: 作成日時 ISO 文字列（省略時は現在時刻）
        """
        properties: dict[str, Any] = {
            "PR Number": {"title": [{"type": "text", "text": {"content": str(pr_number)}}]},
            "URL": {"url": url},
            "Status": {"select": {"name": status}},
            "Created At": {"date": {"start": created_at or datetime.now().isoformat()}},
        }

        if repository:
            properties["Repository"] = {"select": {"name": repository}}

        if workflow_page_id:
            properties["Workflow"] = {"relation": [{"id": workflow_page_id}]}

        properties["Last Updated"] = {
            "date": {"start": datetime.now().isoformat()}
        }

        return self._api.create_page({
            "parent": {"database_id": self._database_id},
            "properties": properties,
        })

    def find_by_pr_number(
        self, pr_number: int, repository: str | None = None
    ) -> str | None:
        """既存の PR レコードを PR Number で検索する（重複作成回避）。

        Returns:
            page_id または None
        """
        try:
            filter_ = {
                "property": "PR Number",
                "title": {"equals": str(pr_number)},
            }
            response = self._api.query_database(self._database_id, filter_=filter_)
            results = response.get("results") or []
            if not results:
                return None
            # repository 指定があれば一致するものを優先
            if repository:
                for page in results:
                    repo_prop = page.get("properties", {}).get("Repository", {}).get(
                        "select"
                    )
                    if repo_prop and repo_prop.get("name") == repository:
                        return page.get("id")
            return results[0].get("id")
        except Exception as e:
            logger.debug(f"PR DB 検索失敗: pr_number={pr_number}, error={e}")
            raise
