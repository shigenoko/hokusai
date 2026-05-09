"""
Configuration Models

Data class definitions for workflow configuration.
"""

from dataclasses import dataclass, field
from pathlib import Path

# デフォルトのステータスマッピング
DEFAULT_STATUS_MAPPING = {
    "in_progress": "進行中",
    "reviewing": "対応待ち/レビュー中",
    "done": "完了",
}


@dataclass
class TaskBackendConfig:
    """タスク管理バックエンドの設定"""

    type: str = "notion"  # "notion" | "github_issue" | "jira" | "linear"
    # GitHub Issue 用
    repo: str | None = None
    # Jira 用
    base_url: str | None = None
    project_key: str | None = None
    # 共通
    extra: dict = field(default_factory=dict)


@dataclass
class GitHostingConfig:
    """Gitホスティングサービスの設定"""

    type: str = "github"  # "github" | "gitlab" | "bitbucket"
    # GitLab 用
    base_url: str = "https://gitlab.com"
    project_path: str | None = None
    # Bitbucket 用
    workspace: str | None = None
    repo_slug: str | None = None
    # 共通
    extra: dict = field(default_factory=dict)


@dataclass
class CrossReviewConfig:
    """クロスLLMレビュー設定"""

    enabled: bool = False
    model: str = "codex-mini-latest"
    phases: list[int] = field(default_factory=lambda: [2, 4])
    timeout: int = 300
    on_failure: str = "warn"  # "warn" | "block" | "skip"
    max_correction_rounds: int = 2


# Slack 通知のサポートイベント名
SLACK_NOTIFICATION_EVENTS = (
    "workflow_started",
    "waiting_for_human",
    "workflow_failed",
    "pr_created",
    "workflow_completed",
)


@dataclass
class SlackNotificationConfig:
    """Slack 通知設定

    Webhook URL は YAML には保存せず、環境変数経由で参照する。
    """

    enabled: bool = False
    webhook_url_env: str = "HOKUSAI_SLACK_WEBHOOK_URL"
    events: list[str] = field(
        default_factory=lambda: [
            "waiting_for_human",
            "workflow_failed",
            "pr_created",
            "workflow_completed",
        ]
    )
    timeout: float = 5.0


@dataclass
class NotificationConfig:
    """通知設定（複数チャネルへの拡張ポイント）"""

    slack: SlackNotificationConfig = field(default_factory=SlackNotificationConfig)


@dataclass
class NotionSyncOutboxConfig:
    """Notion 同期 outbox 設定（送信失敗時の再送制御）"""

    enabled: bool = True
    max_retry_attempts: int = 10


@dataclass
class NotionSyncRetryConfig:
    """Notion API リトライ設定"""

    max_attempts: int = 3
    backoff_seconds: float = 5.0


@dataclass
class NotionSyncRateLimitConfig:
    """Notion API レートリミット設定"""

    requests_per_second: float = 2.0
    debounce_ms: int = 5000


@dataclass
class DesignRetryConfig:
    """Figma / Miro API リトライ設定（既存 NotionSyncRetryConfig と同じ値域）"""

    max_attempts: int = 3
    backoff_seconds: float = 5.0


@dataclass
class DesignRateLimitConfig:
    """Figma / Miro API レートリミット設定"""

    requests_per_second: float = 1.5


@dataclass
class FigmaIntegrationConfig:
    """Figma 連携設定。

    MVP は read-only。書き戻し（コメント投稿）は Phase E で追加。
    Token は環境変数経由でのみ扱う。YAML 直書きは _detect_token_like_values で警告。
    """

    enabled: bool = False
    api_token_env: str = "HOKUSAI_FIGMA_API_TOKEN"
    fetch_comments: bool = True
    export_images: bool = True
    cache_ttl_seconds: int = 1800
    timeout: float = 10.0
    on_failure: str = "warn"  # warn | block | skip
    retry: DesignRetryConfig = field(default_factory=DesignRetryConfig)
    rate_limit: DesignRateLimitConfig = field(default_factory=DesignRateLimitConfig)


@dataclass
class MiroIntegrationConfig:
    """Miro 連携設定。

    MVP は read-only。書き戻し（カード/コメント投稿）は Phase E で追加。
    """

    enabled: bool = False
    api_token_env: str = "HOKUSAI_MIRO_API_TOKEN"
    default_team_id_env: str = "HOKUSAI_MIRO_TEAM_ID"
    use_mcp: bool = False
    cache_ttl_seconds: int = 1800
    timeout: float = 10.0
    on_failure: str = "warn"  # warn | block | skip
    retry: DesignRetryConfig = field(default_factory=DesignRetryConfig)
    rate_limit: DesignRateLimitConfig = field(default_factory=DesignRateLimitConfig)


@dataclass
class WebDashboardAuthConfig:
    """HOKUSAI Web Dashboard（Operations Console）の BASIC 認証設定。

    管理者・開発者のみがアクセスできる状態を保つための最小限の認可機構。
    認証情報は環境変数経由で渡し、YAML には書かない。
    """

    enabled: bool = False
    username_env: str = "HOKUSAI_OPS_USERNAME"
    password_env: str = "HOKUSAI_OPS_PASSWORD"
    realm: str = "HOKUSAI Operations Console"


@dataclass
class WebDashboardConfig:
    """HOKUSAI Web Dashboard（Operations Console）設定。"""

    auth: WebDashboardAuthConfig = field(default_factory=WebDashboardAuthConfig)


@dataclass
class NotionDashboardConfig:
    """Notion メインダッシュボード同期設定

    HOKUSAI 専用 Notion Integration の API token 経由で Notion DB に書き込む。
    既存の Notion MCP（Phase 2/3/4 子ページ保存）とは別経路として扱う。
    """

    enabled: bool = False
    api_token_env: str = "HOKUSAI_NOTION_API_TOKEN"
    workflows_db_id_env: str = "HOKUSAI_NOTION_WORKFLOWS_DB_ID"
    pull_requests_db_id_env: str = "HOKUSAI_NOTION_PR_DB_ID"
    service_status_page_id_env: str = "HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID"
    sync_outbox: NotionSyncOutboxConfig = field(default_factory=NotionSyncOutboxConfig)
    retry: NotionSyncRetryConfig = field(default_factory=NotionSyncRetryConfig)
    rate_limit: NotionSyncRateLimitConfig = field(default_factory=NotionSyncRateLimitConfig)


@dataclass
class RepositoryConfig:
    """リポジトリ設定（複数リポジトリ対応）"""

    name: str  # 表示名 (e.g., "Backend", "API")
    path: Path  # リポジトリのパス
    base_branch: str = "main"  # ベースブランチ
    # リポジトリの説明（Phase 5 プロンプトに含まれる）
    description: str | None = None
    # リポジトリ固有のコマンド（省略時は共通設定を使用）
    build_command: str | None = None
    test_command: str | None = None
    lint_command: str | None = None
    # コーディングルールの説明（Phase 5 プロンプト埋め込み用）
    coding_rules: str | None = None
    # worktree 作成後に実行するセットアップコマンド（依存インストール等）
    setup_command: str | None = None
    # デフォルトでブランチ作成・実装の対象とするか
    default_target: bool = True


@dataclass
class WorkflowConfig:
    """ワークフロー設定"""

    # プロジェクト設定（必須: 環境変数または設定ファイルで指定）
    project_root: Path = field(default_factory=lambda: Path.cwd())
    base_branch: str = "main"

    # ビルドコマンド
    build_command: str = "npm run build"
    test_command: str = "npm run test"
    lint_command: str = "npm run lint"

    # サブモジュール設定（オプション）
    submodule_enabled: bool = False
    submodule_path: str = ""

    # リトライ設定
    max_retry_count: int = 10
    retry_delay_seconds: int = 5

    # タイムアウト設定
    skill_timeout: int = 600  # 10分
    command_timeout: int = 300  # 5分

    # 永続化設定
    data_dir: Path = field(default_factory=lambda: Path.home() / ".hokusai")
    database_path: Path = field(
        default_factory=lambda: Path.home() / ".hokusai" / "workflow.db"
    )
    checkpoint_db_path: Path = field(
        default_factory=lambda: Path.home() / ".hokusai" / "checkpoint.db"
    )

    # スキーマ変更検出キーワード
    schema_change_keywords: list = field(
        default_factory=lambda: ["スキーマ変更", "schema変更", "openapi"]
    )

    # タスク管理バックエンド設定
    task_backend: TaskBackendConfig = field(default_factory=TaskBackendConfig)

    # Gitホスティング設定
    git_hosting: GitHostingConfig = field(default_factory=GitHostingConfig)

    # ステータスマッピング
    status_mapping: dict = field(default_factory=lambda: DEFAULT_STATUS_MAPPING.copy())

    # レビューチェックリスト（プロジェクト固有の追加ルール）
    # 新形式: dict[str, dict] - {"P01": {"name": "...", "description": "..."}, ...}
    # 旧形式: list[str] - ["ルール1", "ルール2", ...] も後方互換でサポート
    review_checklist: dict = field(default_factory=dict)

    # Devin.ai チェック設定（experimental / 後方互換のため保持）
    devin_check: dict = field(default_factory=dict)

    # クロスLLMレビュー設定
    cross_review: CrossReviewConfig = field(default_factory=CrossReviewConfig)

    # 通知設定（Slack 等）
    notifications: NotificationConfig = field(default_factory=NotificationConfig)

    # Notion メインダッシュボード同期設定
    notion_dashboard: NotionDashboardConfig = field(default_factory=NotionDashboardConfig)

    # HOKUSAI Web Dashboard（Operations Console）設定
    web_dashboard: WebDashboardConfig = field(default_factory=WebDashboardConfig)

    # Figma 連携設定
    figma: FigmaIntegrationConfig = field(default_factory=FigmaIntegrationConfig)

    # Miro 連携設定
    miro: MiroIntegrationConfig = field(default_factory=MiroIntegrationConfig)

    # 複数リポジトリ設定（Phase 7で全リポジトリをレビュー）
    # 空の場合はproject_rootのみをレビュー
    repositories: list = field(default_factory=list)  # List[RepositoryConfig]

    # Worktree 設定
    worktree_root: Path = field(
        default_factory=lambda: Path.home() / ".hokusai" / "worktrees"
    )

    def __post_init__(self):
        """データディレクトリを作成"""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def get_status(self, internal_status: str) -> str:
        """内部ステータスを外部ステータスに変換"""
        return self.status_mapping.get(internal_status, internal_status)

    def get_all_repositories(self) -> list:
        """全リポジトリ設定を取得

        repositoriesが設定されていればそれを返し、
        設定されていなければproject_rootからデフォルトのリポジトリ設定を生成して返す。

        Returns:
            List[RepositoryConfig]
        """
        if self.repositories:
            return self.repositories

        # 後方互換: repositoriesが空の場合はproject_rootをデフォルトとして使用
        return [RepositoryConfig(
            name="Default",
            path=self.project_root,
            base_branch=self.base_branch,
            build_command=self.build_command,
            test_command=self.test_command,
            lint_command=self.lint_command,
        )]

    def get_target_repositories(self) -> list:
        """ブランチ作成・実装の対象となるリポジトリを取得

        default_target=Trueのリポジトリのみを返す。
        Phase 1（ブランチ作成）とPhase 5（実装）で使用。

        Returns:
            List[RepositoryConfig]
        """
        all_repos = self.get_all_repositories()
        return [repo for repo in all_repos if repo.default_target]
