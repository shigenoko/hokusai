"""
Phase 8: レビュー指摘の自動修正

Claude Codeを使ってレビュー指摘に自動でコード修正を行い、
コミット＆プッシュまで自動実行する。
自動修正に失敗した場合のみ人間にフォールバックする。
"""

import re

from ...config import get_config
from ...integrations.claude_code import ClaudeCodeClient
from ...integrations.git import GitClient
from ...integrations.git_hosting.github import GitHubHostingClient
from ...logging_config import get_logger
from ...state import WorkflowState
from ...utils.repo_resolver import get_runtime_repository
from ...utils.shell import ShellRunner

logger = get_logger("phase8_review_fix")


def _build_review_fix_prompt(
    comments: list[dict],
    pr_number: int,
    repo_name: str = "",
) -> str:
    """レビューコメントから修正プロンプトを構築"""
    from ...prompts import get_prompt

    # コメントリストを整形
    comment_parts = []
    for i, comment in enumerate(comments, 1):
        comment_type = comment.get("comment_type", "review")
        body = comment.get("body", "")
        author = comment.get("author", "")

        comment_parts.append(f"### 指摘 {i}")
        if comment_type == "issue":
            comment_parts.append("- ファイル: (PR全体への指摘)")
            comment_parts.append("- 行: -")
        else:
            path = comment.get("path", "不明")
            line = comment.get("line", "不明")
            comment_parts.append(f"- ファイル: {path}")
            comment_parts.append(f"- 行: {line}")
        comment_parts.append(f"- レビュアー: {author}")
        comment_parts.append(f"- 指摘内容: {body}")
        comment_parts.append("")

    comments_section = "\n".join(comment_parts)

    return get_prompt(
        "phase8.review_fix",
        pr_number=pr_number,
        comments_section=comments_section,
    )


def _parse_fix_summaries(response: str, count: int) -> dict[int, str]:
    """Claude Codeのレスポンスから各指摘の対応内容をパースする。

    Returns:
        {0: "対応内容", 1: "対応内容", ...} (0-indexed)
    """
    summaries: dict[int, str] = {}
    # "指摘1: ..." or "指摘 1: ..." パターンにマッチ
    pattern = re.compile(r"指摘\s*(\d+)\s*[:：]\s*(.+)")
    for match in pattern.finditer(response):
        idx = int(match.group(1)) - 1  # 0-indexed
        summary = match.group(2).strip()
        if 0 <= idx < count and summary:
            summaries[idx] = summary
    return summaries


def _auto_fix_review_comments(
    state: WorkflowState,
    current_pr: dict,
    comments: list[dict],
) -> bool:
    """Claude Codeでレビュー指摘を自動修正し、コミット＆プッシュする。

    Returns:
        成功した場合True
    """
    pr_number = current_pr.get("number")
    repo_name = current_pr.get("repo_name", "")
    branch_name = state.get("branch_name", "")

    # リポジトリのworking_dirを特定（repo_resolver で一元解決）
    config = get_config()
    runtime_repo = get_runtime_repository(state, config, repo_name)
    if runtime_repo:
        repo_path = runtime_repo.path
    else:
        repo_path = config.project_root

    logger.info(
        f"自動修正開始: PR #{pr_number}, {len(comments)}件の指摘, "
        f"repo={repo_name}, path={repo_path}"
    )

    try:
        # Claude Codeで修正実行
        claude = ClaudeCodeClient(working_dir=repo_path)
        prompt = _build_review_fix_prompt(comments, pr_number, repo_name)

        logger.debug(f"修正プロンプト:\n{prompt[:500]}...")
        print(f"🤖 レビュー指摘 {len(comments)}件 を自動修正中...")

        result = claude.execute_prompt(
            prompt=prompt,
            timeout=config.skill_timeout,
            allow_file_operations=True,
        )
        logger.info(f"Claude Code実行完了: {len(result)}文字")

        # 変更確認
        git = GitClient(str(repo_path))
        if not git.has_uncommitted_changes():
            logger.warning("自動修正後に変更なし")
            print("⚠️ 自動修正: コード変更が検出されませんでした")
            return False

        # git add → commit → push
        shell = ShellRunner(cwd=repo_path)
        shell.run_git("add", "-A", check=True)
        shell.run_git(
            "commit", "-m",
            f"fix: address review comments (PR #{pr_number})",
            check=True,
        )

        git_hosting = GitHubHostingClient(working_dir=repo_path)
        push_ok = git_hosting.push_branch(branch_name)
        if not push_ok:
            logger.error("プッシュ失敗")
            print("⚠️ 自動修正: プッシュに失敗しました")
            return False

        # Claude Codeのレスポンスから各指摘への対応内容をパースし fix_summary を設定
        summaries = _parse_fix_summaries(result, len(comments))
        if summaries:
            for idx, summary in summaries.items():
                comments[idx]["fix_summary"] = summary
            logger.info(f"fix_summary設定: {len(summaries)}/{len(comments)}件")
        else:
            # パース失敗時は git diff --stat からサマリーを生成
            try:
                diff_result = shell.run_git("diff", "HEAD~1", "--stat", check=True)
                diff_stat = diff_result.stdout.strip()
                if diff_stat:
                    for comment in comments:
                        comment["fix_summary"] = f"コードを修正しました。\n```\n{diff_stat}\n```"
            except Exception:
                pass

        logger.info("自動修正成功: コミット＆プッシュ完了")
        print("✅ 自動修正完了: コミット＆プッシュしました")
        return True

    except Exception as e:
        logger.error(f"自動修正失敗: {e}")
        print(f"⚠️ 自動修正失敗: {e}")
        return False
