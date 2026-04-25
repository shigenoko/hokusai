"""
GitHub Issue Task Client

GitHub CLI (gh) を使用してGitHub Issueをタスク管理として使用する。
"""

import json
import re
from typing import Any

from ...utils.shell import ShellError, ShellRunner
from .base import TaskBackendClient


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

    def update_status(self, task_url: str, status: str) -> None:
        """
        GitHub Issueのステータスを更新（ラベルで管理）

        Args:
            task_url: GitHub IssueのURL
            status: 新しいステータス（ラベル名として使用）
        """
        try:
            issue_number = self._extract_issue_number(task_url)
            shell = ShellRunner()

            # 既存のステータス関連ラベルを削除
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

            # 新しいステータスラベルを追加
            shell.run_gh(
                "issue",
                "edit",
                str(issue_number),
                "--add-label",
                status,
                *self._get_repo_arg(),
                check=True,
            )

            print(f"📝 GitHub Issueラベルを更新: {status}")

        except ShellError as e:
            print(f"⚠️ GitHub Issueラベル更新エラー: {e.result.stderr}")
            raise
        except Exception as e:
            print(f"⚠️ GitHub Issueラベル更新エラー: {e}")
            raise

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
