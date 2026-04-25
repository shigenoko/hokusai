"""
Runtime Repository Resolver

config の静的リポジトリ定義と state の実行時パスを統合し、
各 Phase が参照すべきリポジトリ情報を一元的に解決する。
"""

from dataclasses import dataclass
from pathlib import Path

from ..logging_config import get_logger

logger = get_logger("repo_resolver")


@dataclass
class RuntimeRepository:
    """実行時リポジトリ情報

    config の静的設定と state の動的パスを統合した情報。
    各 Phase はこの情報のみを使ってリポジトリを操作する。
    """
    name: str
    path: Path
    source_path: Path
    branch: str
    base_branch: str
    build_command: str | None
    test_command: str | None
    lint_command: str | None
    coding_rules: str | None
    setup_command: str | None
    description: str | None
    worktree_created: bool


def resolve_runtime_repositories(state: dict, config) -> list[RuntimeRepository]:
    """
    state と config からランタイムリポジトリ一覧を解決する

    state["repositories"] が存在する場合はそちらの path を優先し、
    config からビルド/テスト/lint コマンドを補完する。

    state["repositories"] が空の場合は config から直接構築する（後方互換）。

    Args:
        state: WorkflowState
        config: WorkflowConfig

    Returns:
        解決済みの RuntimeRepository リスト
    """
    state_repos = state.get("repositories", [])
    config_repos = config.get_all_repositories()

    # config を name でインデックス化
    config_by_name = {repo.name: repo for repo in config_repos}

    if state_repos:
        result = []
        for repo_state in state_repos:
            name = repo_state.get("name", "")
            config_repo = config_by_name.get(name)

            result.append(RuntimeRepository(
                name=name,
                path=Path(repo_state.get("path", "")),
                source_path=Path(repo_state.get("source_path", repo_state.get("path", ""))),
                branch=repo_state.get("branch", ""),
                base_branch=repo_state.get("base_branch", ""),
                build_command=config_repo.build_command if config_repo else None,
                test_command=config_repo.test_command if config_repo else None,
                lint_command=config_repo.lint_command if config_repo else None,
                coding_rules=config_repo.coding_rules if config_repo else None,
                setup_command=config_repo.setup_command if config_repo else None,
                description=config_repo.description if config_repo else None,
                worktree_created=repo_state.get("worktree_created", False),
            ))
        return result

    # 後方互換: state に repositories がない場合は config から構築
    branch = state.get("branch_name", "")
    base_branch = state.get("base_branch", config.base_branch)

    return [
        RuntimeRepository(
            name=repo.name,
            path=repo.path,
            source_path=repo.path,
            branch=branch,
            base_branch=repo.base_branch or base_branch,
            build_command=repo.build_command,
            test_command=repo.test_command,
            lint_command=repo.lint_command,
            coding_rules=repo.coding_rules,
            setup_command=repo.setup_command,
            description=repo.description,
            worktree_created=False,
        )
        for repo in config_repos
    ]


def get_runtime_repository(
    state: dict,
    config,
    repo_name: str,
) -> RuntimeRepository | None:
    """
    名前を指定して単一のランタイムリポジトリを取得

    Args:
        state: WorkflowState
        config: WorkflowConfig
        repo_name: リポジトリ名

    Returns:
        マッチする RuntimeRepository、見つからない場合は None
    """
    repos = resolve_runtime_repositories(state, config)
    for repo in repos:
        if repo.name == repo_name:
            return repo
    return None
