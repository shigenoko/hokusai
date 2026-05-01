"""
Configuration Manager

Singleton accessor and environment variable management for workflow configuration.
"""

import os
from pathlib import Path

from .loaders import (
    _parse_cross_review_config,
    _parse_git_hosting_config,
    _parse_notifications_config,
    _parse_repositories,
    _parse_review_checklist,
    _parse_task_backend_config,
    load_config_from_file,
)
from .models import DEFAULT_STATUS_MAPPING, WorkflowConfig


def create_config_from_env_and_file(
    config_file: str | Path | None = None,
) -> WorkflowConfig:
    """環境変数と設定ファイルから設定を作成

    Args:
        config_file: 設定ファイルのパス（指定された場合はこれを優先）
    """
    # デフォルト設定
    config_dict = {}

    # 設定ファイルを探す
    if config_file:
        # 明示的に指定された場合
        config_path = Path(config_file).expanduser()
        if not config_path.exists():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {config_path}")
        config_dict = load_config_from_file(config_path)
    else:
        # デフォルトの検索順序
        config_paths = [
            Path.cwd() / "claude-workflow.yaml",
            Path.cwd() / "claude-workflow.yml",
            Path.home() / ".claude-workflow.yaml",
        ]

        for config_path in config_paths:
            if config_path.exists():
                config_dict = load_config_from_file(config_path)
                break

    # 環境変数でオーバーライド
    if os.environ.get("WORKFLOW_PROJECT_ROOT"):
        config_dict["project_root"] = Path(os.environ["WORKFLOW_PROJECT_ROOT"])
    if os.environ.get("WORKFLOW_BASE_BRANCH"):
        config_dict["base_branch"] = os.environ["WORKFLOW_BASE_BRANCH"]
    if os.environ.get("WORKFLOW_DATA_DIR"):
        config_dict["data_dir"] = Path(os.environ["WORKFLOW_DATA_DIR"])
    if os.environ.get("WORKFLOW_WORKTREE_ROOT"):
        config_dict["worktree_root"] = Path(os.environ["WORKFLOW_WORKTREE_ROOT"])

    # パス型フィールドの変換（~を展開）
    if "project_root" in config_dict and isinstance(config_dict["project_root"], str):
        config_dict["project_root"] = Path(config_dict["project_root"]).expanduser()
    if "data_dir" in config_dict and isinstance(config_dict["data_dir"], str):
        config_dict["data_dir"] = Path(config_dict["data_dir"]).expanduser()
    if "worktree_root" in config_dict and isinstance(config_dict["worktree_root"], str):
        config_dict["worktree_root"] = Path(config_dict["worktree_root"]).expanduser()

    # task_backend と git_hosting をパース
    task_backend = _parse_task_backend_config(config_dict)
    git_hosting = _parse_git_hosting_config(config_dict)

    # status_mapping をパース
    status_mapping = DEFAULT_STATUS_MAPPING.copy()
    if "status_mapping" in config_dict and isinstance(config_dict["status_mapping"], dict):
        status_mapping.update(config_dict["status_mapping"])

    # review_checklist をパース（新旧形式対応）
    review_checklist = _parse_review_checklist(config_dict)

    # devin_check をパース（experimental / 後方互換のため保持）
    devin_check = {}
    if "devin_check" in config_dict and isinstance(config_dict["devin_check"], dict):
        devin_check = config_dict["devin_check"]

    # cross_review をパース
    cross_review = _parse_cross_review_config(config_dict)

    # repositories をパース（複数リポジトリ対応）
    default_base_branch = config_dict.get("base_branch", "main")
    repositories = _parse_repositories(config_dict, default_base_branch)

    # notifications をパース（Slack 等）
    notifications = _parse_notifications_config(config_dict)

    # 不要なキーを削除
    for key in ["task_backend", "git_hosting", "status_mapping", "review_checklist", "devin_check", "cross_review", "repositories", "notifications"]:
        config_dict.pop(key, None)

    return WorkflowConfig(
        task_backend=task_backend,
        git_hosting=git_hosting,
        status_mapping=status_mapping,
        review_checklist=review_checklist,
        devin_check=devin_check,
        cross_review=cross_review,
        repositories=repositories,
        notifications=notifications,
        **config_dict,
    )


# グローバル設定インスタンス
_config: WorkflowConfig | None = None


def get_config() -> WorkflowConfig:
    """設定を取得"""
    global _config
    if _config is None:
        _config = create_config_from_env_and_file()
    return _config


def set_config(config: WorkflowConfig) -> None:
    """設定を設定"""
    global _config
    _config = config


def reset_config() -> None:
    """設定をリセット（テスト用）"""
    global _config
    _config = None
