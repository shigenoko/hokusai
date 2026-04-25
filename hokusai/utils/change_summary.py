"""
変更サマリー生成ユーティリティ

Git 差分を元にリポジトリ単位の変更サマリー（Markdown）を生成する。
Phase 8e（PR 本文用）と Phase 10（タスクページ用）の両方で使用。
"""

from pathlib import Path

from ..integrations.git import GitClient
from ..logging_config import get_logger

logger = get_logger("change_summary")

# 大きな差分の制限
MAX_FILES = 30
MAX_DIFF_LINES_PER_FILE = 150
MAX_TOTAL_DIFF_CHARS = 50000


def build_repo_change_summary(
    repo_path: str | Path,
    base_branch: str,
    head_branch: str = "HEAD",
    repo_name: str | None = None,
    max_files: int = MAX_FILES,
) -> str:
    """単一リポジトリの変更サマリーを生成する。

    Git diff から変更ファイル一覧と diff stat を取得し、
    ファイルごとの箇条書き Markdown を生成する。

    Args:
        repo_path: リポジトリパス
        base_branch: ベースブランチ（例: "origin/beta"）
        head_branch: ヘッドブランチ（デフォルト: "HEAD"）
        repo_name: リポジトリ表示名（省略時はパスから推定）
        max_files: 最大ファイル数

    Returns:
        Markdown 形式の変更サマリー（空差分時は空文字列）
    """
    git = GitClient(str(repo_path))
    name = repo_name or Path(repo_path).name

    # origin/ プレフィクスを確保
    base_ref = base_branch if base_branch.startswith("origin/") else f"origin/{base_branch}"

    # 変更ファイル一覧を取得
    changed_files = git.get_diff_files(base_ref, head_branch)
    if not changed_files:
        return ""

    # diff stat を取得
    stat = git.get_diff_stat(base_ref, head_branch)

    # ファイル数が上限を超える場合
    total_file_count = len(changed_files)
    truncated = False
    if total_file_count > max_files:
        truncated = True
        changed_files = changed_files[:max_files]

    # ファイルごとの diff を収集（サマリー素材）
    file_diffs: list[tuple[str, str]] = []
    total_chars = 0
    for filepath in changed_files:
        if total_chars > MAX_TOTAL_DIFF_CHARS:
            break
        diff = git.get_file_diff(base_ref, head_branch, filepath, max_lines=MAX_DIFF_LINES_PER_FILE)
        file_diffs.append((filepath, diff))
        total_chars += len(diff)

    # Markdown 出力を組み立て
    lines = [f"### {name}"]
    if stat:
        lines.append(f"```\n{stat}\n```")
    lines.append("")

    for filepath, diff in file_diffs:
        # diff の追加/削除行数を簡易カウント
        additions = sum(1 for line in diff.split("\n") if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in diff.split("\n") if line.startswith("-") and not line.startswith("---"))
        stat_label = f"(+{additions}, -{deletions})" if additions or deletions else ""
        lines.append(f"- `{filepath}` {stat_label}")

    if truncated:
        lines.append(f"\n> ⚠️ {max_files}件のみ表示（全{total_file_count}件）")

    return "\n".join(lines)


def build_pr_change_summary(
    state: dict,
) -> dict[str, str]:
    """全リポジトリの PR 用変更サマリーを生成する。

    Args:
        state: WorkflowState

    Returns:
        {repo_name: markdown_summary} の辞書
    """
    summaries: dict[str, str] = {}

    for repo in state.get("repositories", []):
        repo_name = repo.get("name", "")
        repo_path = repo.get("path", "")
        base_branch = repo.get("base_branch", "main")

        if not repo_path:
            continue

        try:
            summary = build_repo_change_summary(
                repo_path=repo_path,
                base_branch=base_branch,
                repo_name=repo_name,
            )
            if summary:
                summaries[repo_name] = summary
        except Exception as e:
            logger.warning(f"変更サマリー生成失敗 ({repo_name}): {e}")

    return summaries


def build_combined_change_summary(state: dict) -> str:
    """全リポジトリの変更サマリーを結合した Markdown を生成する。

    Args:
        state: WorkflowState

    Returns:
        結合された Markdown テキスト（差分なしの場合は空文字列）
    """
    summaries = build_pr_change_summary(state)
    if not summaries:
        return ""

    parts = []
    for _repo_name, summary in summaries.items():
        parts.append(summary)

    return "\n\n".join(parts)
