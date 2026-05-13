"""Notion メインダッシュボード同期

HOKUSAI 専用 Notion Integration を経由して、ワークフロー状態を Notion DB に同期する。
既存の Notion MCP（Phase 2/3/4 子ページ保存）とは別経路として扱う。
"""

from .client import NotionAPIClient, NotionAPIError, NotionRateLimitError
from .dispatcher import NotionSyncDispatcher
from .identification import (
    build_notion_identification,
    clear_bot_info_cache,
    get_bot_display_name,
    get_bot_info,
    mask_db_id,
    notion_db_url,
)
from .pull_requests_db import PullRequestsDBClient
from .setup import (
    NotionSetupError,
    detect_shell_rc,
    is_valid_env_var_name,
    persist_env_vars,
    setup_notion_workspace,
)
from .workflows_db import WorkflowsDBClient

__all__ = [
    "NotionAPIClient",
    "NotionAPIError",
    "NotionRateLimitError",
    "NotionSetupError",
    "NotionSyncDispatcher",
    "PullRequestsDBClient",
    "WorkflowsDBClient",
    "build_notion_identification",
    "clear_bot_info_cache",
    "detect_shell_rc",
    "get_bot_display_name",
    "get_bot_info",
    "is_valid_env_var_name",
    "mask_db_id",
    "notion_db_url",
    "persist_env_vars",
    "setup_notion_workspace",
]
