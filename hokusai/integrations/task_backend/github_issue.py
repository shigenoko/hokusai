"""
GitHub Issue Task Client

GitHub CLI (gh) を使用してGitHub Issueをタスク管理として使用する。
"""

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ...logging_config import get_logger
from ...utils.shell import ShellError, ShellRunner
from .base import TaskBackendClient

logger = get_logger("task_backend.github_issue")


class GitHubIssueResult(str, Enum):
    """GitHub Issue 操作の結果ステータス（Notion パターンに準じる）"""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class GitHubIssueOperationResult:
    """GitHub Issue 操作の構造化結果（Issue #73）。

    `update_status` が ShellError を raise すると HOKUSAI を初めて入れた
    リポジトリ（「進行中」等のラベルが未作成）で workflow が phase1 で
    止まるため、Notion / Linear と同じく結果を構造化して返し、呼び出し
    側で audit log + graceful degrade できるようにする。
    """

    result: GitHubIssueResult
    operation: str  # e.g. "update_status"
    reason: str | None = None  # error/skip reason

    @property
    def is_success(self) -> bool:
        return self.result == GitHubIssueResult.SUCCESS


class GitHubIssueClient(TaskBackendClient):
    """GitHub Issue をタスク管理として使用するクライアント"""

    def __init__(self, repo: str | None = None):
        """
        初期化

        Args:
            repo: リポジトリ（owner/repo形式）。Noneの場合は現在のリポジトリを使用
        """
        self.repo = repo

    def _get_repo_arg(self) -> list[str]:
        """リポジトリ引数を取得"""
        if self.repo:
            return ["-R", self.repo]
        return []

    def _extract_issue_number(self, task_url: str) -> int:
        """URLからIssue番号を抽出"""
        # https://github.com/owner/repo/issues/123 形式
        match = re.search(r"/issues/(\d+)", task_url)
        if match:
            return int(match.group(1))

        # 数字のみの場合
        if task_url.isdigit():
            return int(task_url)

        raise ValueError(f"Invalid GitHub Issue URL: {task_url}")

    def fetch_task(self, task_url: str) -> dict[str, Any]:
        """
        GitHub Issueの情報を取得

        Args:
            task_url: GitHub IssueのURL

        Returns:
            タスク情報の辞書
        """
        try:
            issue_number = self._extract_issue_number(task_url)

            shell = ShellRunner()
            result = shell.run_gh(
                "issue",
                "view",
                str(issue_number),
                "--json",
                "number,title,state,labels,body,url",
                *self._get_repo_arg(),
                check=True,
            )

            data = json.loads(result.stdout)

            # ラベルからステータスを抽出
            labels = [label.get("name", "") for label in data.get("labels", [])]
            status = self._labels_to_status(labels)

            return {
                "url": data.get("url", task_url),
                "title": data.get("title", ""),
                "status": status,
                "properties": {
                    "number": data.get("number"),
                    "state": data.get("state"),
                    "labels": labels,
                    "body": data.get("body", ""),
                },
            }

        except ShellError as e:
            print(f"⚠️ GitHub Issue取得エラー: {e.result.stderr}")
            return {
                "url": task_url,
                "title": "",
                "status": "",
                "properties": {},
            }
        except Exception as e:
            print(f"⚠️ GitHub Issue取得エラー: {e}")
            return {
                "url": task_url,
                "title": "",
                "status": "",
                "properties": {},
            }

    def _labels_to_status(self, labels: list[str]) -> str:
        """ラベルからステータスを推定"""
        label_lower = [label.lower() for label in labels]

        if "in-progress" in label_lower or "in progress" in label_lower:
            return "in_progress"
        if "reviewing" in label_lower or "review" in label_lower:
            return "reviewing"
        if "done" in label_lower or "completed" in label_lower:
            return "done"

        return "open"

    def update_status(
        self, task_url: str, status: str
    ) -> GitHubIssueOperationResult:
        """
        GitHub Issueのステータスを更新（ラベルで管理）

        ラベル不在等で `gh issue edit --add-label` が失敗しても **例外を投げず**、
        `GitHubIssueOperationResult(result=FAILED, reason=...)` を返して
        workflow を継続させる（Issue #73）。HOKUSAI を初めて入れたリポジトリ
        は「進行中」等のラベルを持たないことが多く、status 同期失敗で phase1
        を止めるのは過剰反応。Notion / Linear と同じ graceful degrade パターン。

        Args:
            task_url: GitHub IssueのURL
            status: 新しいステータス（ラベル名として使用）

        Returns:
            GitHubIssueOperationResult: 成功時 SUCCESS、ラベル不在等の失敗時
            は FAILED + reason、URL 不正等で issue 番号を解決できないなら
            FAILED + reason
        """
        try:
            issue_number = self._extract_issue_number(task_url)
        except Exception as e:
            reason = f"Issue 番号を解決できません: {e}"
            logger.warning(reason)
            # 表示メッセージは「失敗（継続）」に統一し、結果（FAILED）と
            # 整合させる（PR #74 Copilot Round 1 指摘）。
            print(f"⚠️ GitHub Issueラベル更新失敗（継続）: {reason}")
            return GitHubIssueOperationResult(
                result=GitHubIssueResult.FAILED,
                operation="update_status",
                reason=reason,
            )

        shell = ShellRunner()

        # 既存のステータス関連ラベルを削除（存在しない場合はそのまま継続）
        status_labels = ["in-progress", "reviewing", "done", "open"]
        for label in status_labels:
            try:
                shell.run_gh(
                    "issue",
                    "edit",
                    str(issue_number),
                    "--remove-label",
                    label,
                    *self._get_repo_arg(),
                    check=False,  # ラベルが存在しない場合もあるのでエラーを無視
                )
            except Exception:
                pass

        # 新しいステータスラベルを追加。失敗しても workflow は止めず、
        # FAILED 結果を返して呼び出し側で audit に記録する。
        # ShellError（ラベル不在等）だけでなく FileNotFoundError（gh CLI
        # 未インストール）/ TimeoutExpired / その他予期せぬ例外も graceful
        # degrade する（PR #74 Copilot Round 1 指摘 / Notion パターン準拠）。
        try:
            shell.run_gh(
                "issue",
                "edit",
                str(issue_number),
                "--add-label",
                status,
                *self._get_repo_arg(),
                check=True,
            )
        except ShellError as e:
            reason = (
                f"ラベル '{status}' を追加できません（リポジトリに当該ラベルが"
                f"存在しない可能性）: {e.result.stderr.strip() or e.result.stdout.strip()}"
            )
            logger.warning(reason)
            print(f"⚠️ GitHub Issueラベル更新失敗（継続）: {reason}")
            return GitHubIssueOperationResult(
                result=GitHubIssueResult.FAILED,
                operation="update_status",
                reason=reason,
            )
        except Exception as e:
            # gh CLI 未インストール (FileNotFoundError) / timeout
            # (subprocess.TimeoutExpired) / その他想定外の例外でも継続。
            # 型名は記録するがメッセージ全文は伝播させず、reason に短く要約。
            reason = (
                f"ラベル '{status}' 追加で想定外の例外 ({type(e).__name__}): {e}"
            )
            logger.warning(reason)
            print(f"⚠️ GitHub Issueラベル更新失敗（継続）: {reason}")
            return GitHubIssueOperationResult(
                result=GitHubIssueResult.FAILED,
                operation="update_status",
                reason=reason,
            )

        print(f"📝 GitHub Issueラベルを更新: {status}")
        return GitHubIssueOperationResult(
            result=GitHubIssueResult.SUCCESS, operation="update_status"
        )

    def append_progress(self, task_url: str, content: str) -> None:
        """
        GitHub Issueにコメントを追加

        Args:
            task_url: GitHub IssueのURL
            content: 追記する内容（Markdown形式）
        """
        try:
            issue_number = self._extract_issue_number(task_url)

            shell = ShellRunner()
            shell.run_gh(
                "issue",
                "comment",
                str(issue_number),
                "-b",
                content,
                *self._get_repo_arg(),
                check=True,
            )

            print("📝 GitHub Issueにコメントを追加")

        except ShellError as e:
            print(f"⚠️ GitHub Issueコメント追加エラー: {e.result.stderr}")
            raise
        except Exception as e:
            print(f"⚠️ GitHub Issueコメント追加エラー: {e}")
            raise

    def prepend_content(self, task_url: str, content: str) -> None:
        """
        GitHub Issue本文の先頭にコンテンツを追記

        Args:
            task_url: GitHub IssueのURL
            content: 先頭に追記する内容（Markdown形式）
        """
        try:
            issue_number = self._extract_issue_number(task_url)
            shell = ShellRunner()

            # 現在の本文を取得
            result = shell.run_gh(
                "issue",
                "view",
                str(issue_number),
                "--json",
                "body",
                *self._get_repo_arg(),
                check=True,
            )

            data = json.loads(result.stdout)
            current_body = data.get("body", "")

            # 先頭に追記
            new_body = f"{content}\n\n---\n\n{current_body}" if current_body else content

            # 本文を更新
            shell.run_gh(
                "issue",
                "edit",
                str(issue_number),
                "-b",
                new_body,
                *self._get_repo_arg(),
                check=True,
            )

            print("📝 GitHub Issue本文の先頭にコンテンツを追記")

        except ShellError as e:
            print(f"⚠️ GitHub Issue本文更新エラー: {e.result.stderr}")
            raise
        except Exception as e:
            print(f"⚠️ GitHub Issue本文更新エラー: {e}")
            raise
