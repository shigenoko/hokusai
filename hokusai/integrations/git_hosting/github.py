"""
GitHub Hosting Client

GitHub CLI (gh) を使用してGitHub操作を行うクライアント。
"""

import json
from pathlib import Path

from ...config import get_config
from ...utils.shell import ShellError, ShellRunner
from ..claude_code import ClaudeCodeClient
from .base import GitHostingClient, PullRequest, ReviewComment


class GitHubHostingClient(GitHostingClient):
    """GitHub CLI を使用するクライアント"""

    def __init__(
        self,
        claude_client: ClaudeCodeClient | None = None,
        owner: str | None = None,
        repo: str | None = None,
        working_dir: Path | None = None,
    ):
        """
        初期化

        Args:
            claude_client: ClaudeCodeClientのインスタンス（PR作成スキル用）
            owner: リポジトリオーナー（指定時は固定、未指定時は動的取得）
            repo: リポジトリ名（指定時は固定、未指定時は動的取得）
            working_dir: 作業ディレクトリ（指定時はこれを使用、未指定時はconfig.project_root）
        """
        self.claude = claude_client or ClaudeCodeClient()
        self._owner = owner
        self._repo = repo
        self._working_dir = working_dir

    def _get_project_root(self) -> Path:
        """プロジェクトルートを取得"""
        if self._working_dir:
            return self._working_dir
        config = get_config()
        return config.project_root

    def _get_shell(self) -> ShellRunner:
        """プロジェクトルート用のShellRunnerを取得"""
        return ShellRunner(cwd=self._get_project_root())

    def get_repo_info(self) -> tuple[str, str]:
        """
        現在のリポジトリのowner/repoを取得

        Returns:
            (owner, repo) のタプル
        """
        # 明示的に指定されている場合はそれを返す
        if self._owner and self._repo:
            return self._owner, self._repo

        shell = self._get_shell()
        result = shell.run_gh("repo", "view", "--json", "owner,name", check=True)
        data = json.loads(result.stdout)
        return data["owner"]["login"], data["name"]

    def create_draft_pull_request(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequest:
        """
        Draft PRを作成（gh CLI使用）

        Note:
            gh pr create --draft コマンドを使用してPRを作成します。
        """
        shell = self._get_shell()
        result = shell.run_gh(
            "pr",
            "create",
            "--draft",
            "--title",
            title,
            "--body",
            body,
            "--head",
            head_branch,
            "--base",
            base_branch,
            check=True,
        )
        # URLからPR番号を抽出
        pr_url = result.stdout.strip()
        pr_number = int(pr_url.split("/")[-1])

        return PullRequest(
            number=pr_number,
            url=pr_url,
            title=title,
            state="open",
            draft=True,
        )

    def mark_ready_for_review(self, pr_number: int) -> None:
        """
        Draft PRをReady for Reviewに変更

        Args:
            pr_number: PR番号
        """
        shell = self._get_shell()
        shell.run_gh("pr", "ready", str(pr_number), check=True)

    def get_review_comments(
        self,
        pr_number: int,
        exclude_authors: list[str] | None = None,
    ) -> list[ReviewComment]:
        """
        レビューコメントを取得

        Args:
            pr_number: PR番号
            exclude_authors: 除外する著者のリスト（例: ["copilot"]）

        Returns:
            レビューコメントのリスト
        """
        owner, repo = self.get_repo_info()

        shell = ShellRunner()
        result = shell.run_gh(
            "api", f"repos/{owner}/{repo}/pulls/{pr_number}/comments", check=True
        )

        all_comments = json.loads(result.stdout)
        exclude_authors = exclude_authors or []

        # 返信済みコメントIDのセットを構築
        # in_reply_to_id が設定されているコメントは返信なので、
        # その返信先のコメントIDを収集する
        replied_comment_ids: set[int] = set()
        for comment in all_comments:
            reply_to_id = comment.get("in_reply_to_id")
            if reply_to_id is not None:
                replied_comment_ids.add(reply_to_id)

        comments = []
        for comment in all_comments:
            # 返信コメントはスキップ（元の指摘コメントのみを対象にする）
            if comment.get("in_reply_to_id") is not None:
                continue

            author = comment.get("user", {}).get("login", "").lower()

            # 除外著者をスキップ
            skip = False
            for exclude in exclude_authors:
                if exclude.lower() in author:
                    skip = True
                    break

            if not skip:
                comment_id = comment.get("id")
                comments.append(
                    ReviewComment(
                        id=comment_id,
                        body=comment.get("body", ""),
                        path=comment.get("path"),
                        line=comment.get("line"),
                        author=author,
                        # GitHubから直接返信の有無を検出
                        replied=comment_id in replied_comment_ids,
                    )
                )

        return comments

    def reply_to_comment(
        self,
        pr_number: int,
        comment_id: int,
        body: str,
    ) -> bool:
        """
        レビューコメントに返信

        Args:
            pr_number: PR番号
            comment_id: コメントID
            body: 返信内容

        Returns:
            成功した場合True
        """
        try:
            owner, repo = self.get_repo_info()
            shell = ShellRunner()
            shell.run_gh(
                "api",
                f"repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
                "-f",
                f"body={body}",
                check=True,
            )
            return True
        except ShellError as e:
            print(f"⚠️ コメント返信失敗 (ID: {comment_id}): {e.result.stderr}")
            return False

    def resolve_thread(self, thread_id: str) -> bool:
        """
        レビュースレッドを解決（Resolve conversation）

        Args:
            thread_id: スレッドID

        Returns:
            成功した場合True
        """
        try:
            query = f"""
            mutation {{
              resolveReviewThread(input: {{threadId: "{thread_id}"}}) {{
                thread {{ isResolved }}
              }}
            }}
            """
            shell = ShellRunner()
            shell.run_gh("api", "graphql", "-f", f"query={query}", check=True)
            return True
        except ShellError as e:
            print(f"⚠️ スレッド解決失敗 (ID: {thread_id}): {e.result.stderr}")
            return False

    def get_thread_id_for_comment(
        self,
        pr_number: int,
        comment_id: int,
    ) -> str | None:
        """
        コメントIDからスレッドIDを取得

        Args:
            pr_number: PR番号
            comment_id: コメントID

        Returns:
            スレッドID（取得できない場合はNone）
        """
        try:
            owner, repo = self.get_repo_info()
            query = f"""
            query {{
              repository(owner: "{owner}", name: "{repo}") {{
                pullRequest(number: {pr_number}) {{
                  reviewThreads(first: 100) {{
                    nodes {{
                      id
                      comments(first: 100) {{
                        nodes {{ databaseId }}
                      }}
                    }}
                  }}
                }}
              }}
            }}
            """
            shell = ShellRunner()
            result = shell.run_gh("api", "graphql", "-f", f"query={query}", check=True)
            data = json.loads(result.stdout)
            threads = data["data"]["repository"]["pullRequest"]["reviewThreads"][
                "nodes"
            ]

            for thread in threads:
                for comment in thread["comments"]["nodes"]:
                    if comment["databaseId"] == comment_id:
                        return thread["id"]
            return None
        except Exception as e:
            print(f"⚠️ スレッドID取得失敗: {e}")
            return None

    def is_changes_requested(self, pr_number: int) -> bool:
        """
        CHANGES_REQUESTED ステータスかどうかを確認

        Args:
            pr_number: PR番号

        Returns:
            変更要求がある場合True
        """
        try:
            shell = self._get_shell()
            result = shell.run_gh(
                "pr", "view", str(pr_number), "--json", "reviews", check=True
            )
            pr_data = json.loads(result.stdout)

            for review in pr_data.get("reviews", []):
                author = review.get("author", {}).get("login", "").lower()
                if "copilot" not in author:
                    if review.get("state") == "CHANGES_REQUESTED":
                        return True
            return False
        except Exception:
            return False

    def get_pr_for_branch(self, branch_name: str) -> PullRequest | None:
        """
        ブランチに紐づくPRを取得

        Args:
            branch_name: ブランチ名

        Returns:
            PR情報（存在しない場合はNone）
        """
        try:
            shell = self._get_shell()
            result = shell.run_gh(
                "pr",
                "list",
                "--head",
                branch_name,
                "--json",
                "number,url,title,state,isDraft",
                "--jq",
                ".[0]",
                check=True,
            )
            if result.stdout.strip():
                data = json.loads(result.stdout)
                return PullRequest(
                    number=data.get("number"),
                    url=data.get("url"),
                    title=data.get("title", ""),
                    state=data.get("state", "open").lower(),
                    draft=data.get("isDraft", False),
                )
            return None
        except Exception:
            return None

    def branch_exists_on_remote(self, branch_name: str) -> bool:
        """
        ブランチがリモートに存在するか確認

        Args:
            branch_name: ブランチ名

        Returns:
            存在する場合True
        """
        try:
            shell = self._get_shell()
            result = shell.run_git(
                "ls-remote", "--heads", "origin", branch_name, check=True
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def push_branch(self, branch_name: str) -> bool:
        """
        ブランチをリモートにプッシュ

        Args:
            branch_name: ブランチ名

        Returns:
            成功した場合True
        """
        try:
            shell = self._get_shell()
            shell.run_git("push", "-u", "origin", branch_name, check=True)
            return True
        except ShellError as e:
            print(f"⚠️ ブランチプッシュ失敗: {e.result.stderr}")
            return False

    def update_pr_body(self, pr_number: int, body: str) -> bool:
        """
        PR本文を更新

        Args:
            pr_number: PR番号
            body: 新しいPR本文（Markdown）

        Returns:
            成功した場合True
        """
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
                f.write(body)
                tmp_path = f.name
            try:
                shell = self._get_shell()
                shell.run_gh(
                    "pr", "edit", str(pr_number),
                    "--body-file", tmp_path,
                    check=True,
                )
                return True
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        except ShellError as e:
            print(f"⚠️ PR本文更新失敗: {e.result.stderr}")
            return False

    def get_pr_body(self, pr_number: int) -> str | None:
        """
        PR本文を取得

        Args:
            pr_number: PR番号

        Returns:
            PR本文（取得できない場合はNone）
        """
        try:
            shell = self._get_shell()
            result = shell.run_gh(
                "pr", "view", str(pr_number),
                "--json", "body",
                "--jq", ".body",
                check=True,
            )
            return result.stdout
        except Exception:
            return None

    def get_issue_comments(
        self,
        pr_number: int,
        exclude_authors: list[str] | None = None,
    ) -> list[ReviewComment]:
        """
        PR の issue comment（PR全体へのコメント）を取得

        Args:
            pr_number: PR番号
            exclude_authors: 除外する著者のリスト

        Returns:
            ReviewComment のリスト（comment_type="issue"）
        """
        import re

        owner, repo = self.get_repo_info()
        shell = ShellRunner()
        result = shell.run_gh(
            "api", f"repos/{owner}/{repo}/issues/{pr_number}/comments", check=True
        )

        all_comments = json.loads(result.stdout)
        exclude_authors = exclude_authors or []

        # hokusai が投稿した返信コメントから、元コメントIDを収集
        # パターン: <!-- hokusai-reply-to: 12345 -->
        # 著者名に依存せず、返信マーカーの有無で判定する
        # （投稿アカウントが "hokusai" を含まない場合があるため）
        reply_pattern = re.compile(r"<!-- hokusai-reply-to: (\d+) -->")
        replied_comment_ids: set[int] = set()
        reply_comment_ids: set[int] = set()  # 返信マーカーを含むコメント自体のID

        for comment in all_comments:
            comment_body = comment.get("body", "")
            match = reply_pattern.search(comment_body)
            if match:
                replied_comment_ids.add(int(match.group(1)))
                reply_comment_ids.add(comment.get("id"))

        comments = []
        for comment in all_comments:
            comment_id = comment.get("id")

            # hokusai の返信コメント（マーカー付き）はスキップ
            if comment_id in reply_comment_ids:
                continue

            author = comment.get("user", {}).get("login", "").lower()

            # 除外著者をスキップ
            skip = False
            for exclude in exclude_authors:
                if exclude.lower() in author:
                    skip = True
                    break
            if skip:
                continue

            comments.append(
                ReviewComment(
                    id=comment_id,
                    body=comment.get("body", ""),
                    path=None,
                    line=None,
                    author=author,
                    replied=comment_id in replied_comment_ids,
                    comment_type="issue",
                )
            )

        return comments

    def reply_to_issue_comment(
        self,
        pr_number: int,
        body: str,
    ) -> bool:
        """
        issue comment として返信を投稿

        Args:
            pr_number: PR番号
            body: 返信内容

        Returns:
            成功した場合True
        """
        try:
            owner, repo = self.get_repo_info()
            shell = ShellRunner()
            shell.run_gh(
                "api",
                f"repos/{owner}/{repo}/issues/{pr_number}/comments",
                "-f",
                f"body={body}",
                check=True,
            )
            return True
        except ShellError as e:
            print(f"⚠️ issue comment 投稿失敗: {e.result.stderr}")
            return False

    def get_current_branch(self) -> str | None:
        """
        現在のブランチ名を取得

        Returns:
            ブランチ名（取得できない場合はNone）
        """
        try:
            shell = self._get_shell()
            result = shell.run_git("branch", "--show-current", check=True)
            return result.stdout.strip() or None
        except Exception:
            return None

    def get_pr_commit_count(self, pr_number: int) -> int | None:
        """PRのコミット数を取得"""
        try:
            owner, repo = self.get_repo_info()
            shell = self._get_shell()
            result = shell.run_gh(
                "api", f"repos/{owner}/{repo}/pulls/{pr_number}",
                "--jq", ".commits",
                check=True,
            )
            return int(result.stdout.strip())
        except Exception:
            return None

    def get_pr_approval_status(self, pr_number: int) -> dict:
        """
        PRの承認状態とCIチェック状態を取得

        Args:
            pr_number: PR番号

        Returns:
            {
                "has_approval": bool,           # APPROVED reviewが存在する
                "checks_passing": bool | None,  # CIチェック結果 (None=不明/未実行)
            }
        """
        try:
            shell = self._get_shell()
            result = shell.run_gh(
                "pr", "view", str(pr_number),
                "--json", "reviews,statusCheckRollup",
                check=True,
            )
            pr_data = json.loads(result.stdout)

            # APPROVED reviewの確認（botを除く）
            has_approval = False
            for review in pr_data.get("reviews", []):
                author = review.get("author", {}).get("login", "").lower()
                if "bot" in author or "copilot" in author:
                    continue
                if review.get("state") == "APPROVED":
                    has_approval = True
                    break

            # CIチェック状態の確認
            checks = pr_data.get("statusCheckRollup", [])
            if not checks:
                checks_passing = None
            else:
                all_passed = all(
                    c.get("conclusion") == "SUCCESS" or c.get("status") == "COMPLETED"
                    for c in checks
                )
                any_failed = any(
                    c.get("conclusion") in ("FAILURE", "ERROR", "CANCELLED")
                    for c in checks
                )
                if any_failed:
                    checks_passing = False
                elif all_passed:
                    checks_passing = True
                else:
                    checks_passing = None  # まだ実行中

            return {
                "has_approval": has_approval,
                "checks_passing": checks_passing,
            }
        except Exception as e:
            print(f"⚠️ PR承認状態取得失敗: {e}")
            return {
                "has_approval": False,
                "checks_passing": None,
            }

    def get_pr_status_from_github(
        self, pr_number: int
    ) -> dict | None:
        """
        GitHubからPRの状態とCopilotレビュー状態を取得

        Args:
            pr_number: PR番号

        Returns:
            {
                'github_status': 'draft' | 'open' | 'merged' | 'closed',
                'copilot_review_passed': bool | None,
                'copilot_comments': list[dict] | None,
            }
            取得失敗時はNone
        """
        try:
            shell = self._get_shell()
            owner, repo = self.get_repo_info()

            # PRの基本情報を取得
            result = shell.run_gh(
                "pr", "view", str(pr_number),
                "--repo", f"{owner}/{repo}",
                "--json", "state,isDraft,mergedAt",
                check=True,
            )
            pr_data = json.loads(result.stdout)

            # github_statusを判定
            if pr_data.get("mergedAt"):
                github_status = "merged"
            elif pr_data.get("state") == "CLOSED":
                github_status = "closed"
            elif pr_data.get("isDraft"):
                github_status = "draft"
            else:
                github_status = "open"

            # Copilotレビュー状態を取得（GraphQL API使用）
            copilot_review_passed = None
            copilot_comments = []

            graphql_query = """
            query($owner: String!, $repo: String!, $number: Int!) {
              repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                  reviews(first: 10) {
                    nodes {
                      author { login }
                      state
                    }
                  }
                  reviewThreads(first: 50) {
                    nodes {
                      isResolved
                      comments(first: 5) {
                        nodes {
                          id
                          author { login }
                          body
                          path
                          line
                        }
                      }
                    }
                  }
                }
              }
            }
            """
            result = shell.run_gh(
                "api", "graphql",
                "-f", f"query={graphql_query}",
                "-F", f"owner={owner}",
                "-F", f"repo={repo}",
                "-F", f"number={pr_number}",
                check=True,
            )
            graphql_data = json.loads(result.stdout)
            pr_graphql = graphql_data.get("data", {}).get("repository", {}).get("pullRequest", {})

            # Copilotレビューを検索
            reviews = pr_graphql.get("reviews", {}).get("nodes", [])
            copilot_reviewed = False
            for review in reviews:
                author = review.get("author", {}).get("login", "").lower()
                if "copilot" in author:
                    copilot_reviewed = True
                    break

            # Copilotコメントを収集
            review_threads = pr_graphql.get("reviewThreads", {}).get("nodes", [])
            all_resolved = True
            for thread in review_threads:
                comments = thread.get("comments", {}).get("nodes", [])
                for comment in comments:
                    author = comment.get("author", {}).get("login", "").lower()
                    if "copilot" in author:
                        copilot_comments.append({
                            "id": comment.get("id"),
                            "body": comment.get("body", "")[:200],  # 長すぎる場合は切り詰め
                            "path": comment.get("path"),
                            "line": comment.get("line"),
                            "resolved": thread.get("isResolved", False),
                            "replied": len(comments) > 1,
                        })
                        if not thread.get("isResolved", False):
                            all_resolved = False

            # Copilotレビュー結果を判定
            if copilot_reviewed or copilot_comments:
                copilot_review_passed = all_resolved

            return {
                "github_status": github_status,
                "copilot_review_passed": copilot_review_passed,
                "copilot_comments": copilot_comments if copilot_comments else None,
            }

        except Exception as e:
            print(f"⚠️ PR状態取得失敗: {e}")
            return None
