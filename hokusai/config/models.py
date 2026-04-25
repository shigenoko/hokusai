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
