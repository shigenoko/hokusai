"""
Configuration Manager

Singleton accessor and environment variable management for workflow configuration.
"""

import os
from pathlib import Path

from .loaders import (
    _parse_cross_review_config,
    _parse_figma_config,
    _parse_git_hosting_config,
    _parse_miro_config,
    _parse_notifications_config,
    _parse_notion_dashboard_config,
    _parse_repositories,
    _parse_review_checklist,
    _parse_task_backend_config,
    _parse_web_dashboard_config,
    load_config_from_file,
)
from .models import DEFAULT_STATUS_MAPPING, WorkflowConfig


def create_config_from_env_and_file(
    config_file: str | Path | None = None,
    *,
    profile_name: str | None = None,
) -> WorkflowConfig:
    """環境変数と設定ファイルから設定を作成

    Args:
        config_file: 設定ファイルのパス（指定された場合はこれを優先）
        profile_name: profile 名（指定時は ~/.hokusai/profiles.yaml から
            該当 profile の config_path を解決）

    Raises:
        ConflictingProfileAndConfigError: config_file と profile_name 同時指定
        ProfileError / ProfileRegistryNotFoundError / ProfileNotFoundError / 等:
            profile 解決に失敗した場合（hokusai.config.profiles の例外を伝搬）

    Notes:
        config_file と profile_name は排他。両方指定された場合は
        ConflictingProfileAndConfigError を投げる（実装計画書 §4.3）。
    """
    # 排他チェック（実装計画書 §4.3）
    from .profiles import (
        assert_profile_config_exclusive,
        resolve_profile_to_config_path,
    )

    assert_profile_config_exclusive(profile_name, config_file)

    # profile 指定がある場合は registry から config_path を解決
    profile_data_dir_default: Path | None = None
    if profile_name:
        profile, resolved_path = resolve_profile_to_config_path(profile_name)
        config_file = resolved_path
        # Phase C: registry 側で data_dir が指定されていれば、後段で
        # config_dict に未指定の path フィールドを補完するために保持
        profile_data_dir_default = profile.data_dir

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

    # 環境変数でオーバーライド（~ を含む値でも正しく展開されるよう expanduser() を必ず通す）
    if os.environ.get("WORKFLOW_PROJECT_ROOT"):
        config_dict["project_root"] = Path(os.environ["WORKFLOW_PROJECT_ROOT"]).expanduser()
    if os.environ.get("WORKFLOW_BASE_BRANCH"):
        config_dict["base_branch"] = os.environ["WORKFLOW_BASE_BRANCH"]
    if os.environ.get("WORKFLOW_DATA_DIR"):
        config_dict["data_dir"] = Path(os.environ["WORKFLOW_DATA_DIR"]).expanduser()
    if os.environ.get("WORKFLOW_WORKTREE_ROOT"):
        config_dict["worktree_root"] = Path(os.environ["WORKFLOW_WORKTREE_ROOT"]).expanduser()

    # パス型フィールドの変換（~を展開）
    # 上の env override は Path オブジェクトで入る場合があるため、str / Path 両方をハンドル
    for path_key in ("project_root", "data_dir", "worktree_root", "database_path", "checkpoint_db_path"):
        val = config_dict.get(path_key)
        if isinstance(val, str):
            config_dict[path_key] = Path(val).expanduser()
        elif isinstance(val, Path) and "~" in str(val):
            # env override が "~" を含む場合の safety net（既に展開済みだが二重防御）
            config_dict[path_key] = val.expanduser()

    # Phase C: profile registry の data_dir から path フィールドを補完
    # 補完ルール:
    #   - config file に明示があれば config file 優先
    #   - 環境変数 WORKFLOW_* で上書きされていればそれ優先（既に config_dict に入っている）
    #   - どちらも無く registry に data_dir があれば、それを基点に補完
    if profile_data_dir_default is not None:
        base = profile_data_dir_default
        config_dict.setdefault("data_dir", base)
        config_dict.setdefault("database_path", base / "workflow.db")
        config_dict.setdefault("checkpoint_db_path", base / "checkpoint.db")
        config_dict.setdefault("worktree_root", base / "worktrees")

    # data_dir が解決された結果として作成されるディレクトリを保証
    # （WorkflowConfig.__post_init__ が data_dir を作るが、補完した周辺パスの親も用意）
    #
    # database_path / checkpoint_db_path はファイルパスなので「親ディレクトリ」を作成。
    # worktree_root は git worktree が中に配置するディレクトリパスなので、
    # それ自体を作成する必要がある（parent だけ作っても worktree 作成側で失敗する）。
    for path_key in ("database_path", "checkpoint_db_path"):
        p = config_dict.get(path_key)
        if isinstance(p, Path):
            p.parent.mkdir(parents=True, exist_ok=True)
    worktree_root_val = config_dict.get("worktree_root")
    if isinstance(worktree_root_val, Path):
        worktree_root_val.mkdir(parents=True, exist_ok=True)

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

    # notion_dashboard をパース
    notion_dashboard = _parse_notion_dashboard_config(config_dict)

    # web_dashboard をパース（Operations Console アクセス制限）
    web_dashboard = _parse_web_dashboard_config(config_dict)

    # figma / miro をパース（design integration）
    figma = _parse_figma_config(config_dict)
    miro = _parse_miro_config(config_dict)

    # 不要なキーを削除
    for key in [
        "task_backend",
        "git_hosting",
        "status_mapping",
        "review_checklist",
        "devin_check",
        "cross_review",
        "repositories",
        "notifications",
        "notion_dashboard",
        "web_dashboard",
        "figma",
        "miro",
    ]:
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
        notion_dashboard=notion_dashboard,
        web_dashboard=web_dashboard,
        figma=figma,
        miro=miro,
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
