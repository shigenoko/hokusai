"""
GitLab Hosting Client

GitLab CLI (glab) または GitLab API を使用してGitLab操作を行うクライアント。
"""

import json

from ...utils.shell import ShellError, ShellRunner
from .base import GitHostingClient, PullRequest, ReviewComment


class GitLabHostingClient(GitHostingClient):
    """GitLab CLI または API を使用するクライアント"""

    def __init__(
        self,
        base_url: str = "https://gitlab.com",
        project_path: str | None = None,
    ):
        """
        初期化

        Args:
            base_url: GitLab のベースURL（自己ホストの場合）
            project_path: プロジェクトパス（group/project形式）
        """
        self.base_url = base_url
        self.project_path = project_path
        self._use_glab = self._check_glab_available()

    def _check_glab_available(self) -> bool:
        """glab CLI が利用可能かチェック"""
        try:
            shell = ShellRunner()
            shell.run(["glab", "--version"], check=True)
            return True
        except (ShellError, FileNotFoundError):
            return False

    def _get_project_arg(self) -> list[str]:
        """プロジェクト引数を取得"""
        if self.project_path:
            return ["-R", self.project_path]
        return []

    def get_repo_info(self) -> tuple[str, str]:
        """
        現在のリポジトリのgroup/projectを取得

        Returns:
            (group, project) のタプル
        """
        if self.project_path:
            parts = self.project_path.split("/")
            if len(parts) >= 2:
                return "/".join(parts[:-1]), parts[-1]

        # git remote から取得
        try:
            shell = ShellRunner()
            result = shell.run_git("remote", "get-url", "origin", check=True)
            url = result.stdout.strip()

            # SSH or HTTPS URL をパース
            # git@gitlab.com:group/project.git
            # https://gitlab.com/group/project.git
            if ":" in url and "@" in url:
                # SSH
                path = url.split(":")[-1].replace(".git", "")
            else:
                # HTTPS
                path = "/".join(url.split("/")[-2:]).replace(".git", "")

            parts = path.split("/")
            return "/".join(parts[:-1]), parts[-1]
        except Exception:
            return "", ""

    def create_draft_pull_request(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequest:
        """
        Draft マージリクエストを作成

        Args:
            title: MRタイトル
            body: MR本文（Markdown）
            head_branch: マージ元ブランチ
            base_branch: マージ先ブランチ

        Returns:
            作成されたMR情報
        """
        if self._use_glab:
            shell = ShellRunner()
            result = shell.run(
                [
                    "glab",
                    "mr",
                    "create",
                    "--draft",
                    "--title",
                    title,
                    "--description",
                    body,
                    "--source-branch",
                    head_branch,
                    "--target-branch",
                    base_branch,
                    "--yes",
                    *self._get_project_arg(),
                ],
                check=True,
            )
            # URLからMR番号を抽出
            mr_url = result.stdout.strip().split("\n")[-1]
            mr_number = int(mr_url.split("/")[-1])

            return PullRequest(
                number=mr_number,
                url=mr_url,
                title=title,
                state="opened",
                draft=True,
            )
        else:
            raise NotImplementedError(
                "この機能には glab CLI が必要です。"
                "インストール: https://gitlab.com/gitlab-org/cli"
            )

    def mark_ready_for_review(self, pr_number: int) -> None:
        """
        Draft MRをReady for Reviewに変更

        Args:
            pr_number: MR番号
        """
        if self._use_glab:
            shell = ShellRunner()
            shell.run(
                [
                    "glab",
                    "mr",
                    "update",
                    str(pr_number),
                    "--ready",
                    *self._get_project_arg(),
                ],
                check=True,
            )
        else:
            raise NotImplementedError(
                "この機能には glab CLI が必要です。"
                "インストール: https://gitlab.com/gitlab-org/cli"
            )

    def get_review_comments(
        self,
        pr_number: int,
        exclude_authors: list[str] | None = None,
    ) -> list[ReviewComment]:
        """
        レビューコメント（ディスカッション）を取得

        Args:
            pr_number: MR番号
            exclude_authors: 除外する著者のリスト

        Returns:
            レビューコメントのリスト
        """
        if self._use_glab:
            try:
                shell = ShellRunner()
                result = shell.run(
                    [
                        "glab",
                        "mr",
                        "note",
                        "list",
                        str(pr_number),
                        "--output",
                        "json",
                        *self._get_project_arg(),
                    ],
                    check=True,
                )

                notes = json.loads(result.stdout) if result.stdout.strip() else []
                exclude_authors = exclude_authors or []

                comments = []
                for note in notes:
                    author = note.get("author", {}).get("username", "").lower()

                    # 除外著者をスキップ
                    skip = False
                    for exclude in exclude_authors:
                        if exclude.lower() in author:
                            skip = True
                            break

                    if not skip and note.get("type") == "DiffNote":
                        comments.append(
                            ReviewComment(
                                id=note.get("id"),
                                body=note.get("body", ""),
                                path=note.get("position", {}).get("new_path"),
                                line=note.get("position", {}).get("new_line"),
                                author=author,
                            )
                        )

                return comments
            except Exception as e:
                print(f"⚠️ GitLabコメント取得エラー: {e}")
                return []
        else:
            return []

    def reply_to_comment(
        self,
        pr_number: int,
        comment_id: int,
        body: str,
    ) -> bool:
        """
        コメントに返信

        Args:
            pr_number: MR番号
            comment_id: コメントID
            body: 返信内容

        Returns:
            成功した場合True
        """
        if self._use_glab:
            try:
                shell = ShellRunner()
                shell.run(
                    [
                        "glab",
                        "mr",
                        "note",
                        str(pr_number),
                        "--message",
                        body,
                        *self._get_project_arg(),
                    ],
                    check=True,
                )
                return True
            except ShellError as e:
                print(f"⚠️ GitLabコメント返信失敗: {e.result.stderr}")
                return False
        return False

    def resolve_thread(self, thread_id: str) -> bool:
        """
        スレッドを解決

        Args:
            thread_id: スレッドID

        Returns:
            成功した場合True

        Note:
            glab CLI では直接スレッド解決ができないため、
            API を直接呼び出す必要があります。
        """
        # glab CLI ではスレッド解決がサポートされていないため、
        # 将来的に API 直接呼び出しを実装
        print("⚠️ GitLabのスレッド解決は未実装です")
        return False

    def get_thread_id_for_comment(
        self,
        pr_number: int,
        comment_id: int,
    ) -> str | None:
        """
        コメントIDからスレッドIDを取得

        Args:
            pr_number: MR番号
            comment_id: コメントID

        Returns:
            スレッドID（GitLabでは discussion_id）
        """
        # GitLab では note から discussion_id を取得する必要がある
        # 現時点では未実装
        return None

    def is_changes_requested(self, pr_number: int) -> bool:
        """
        変更要求があるかどうかを確認

        Args:
            pr_number: MR番号

        Returns:
            変更要求がある場合True
        """
        if self._use_glab:
            try:
                shell = ShellRunner()
                result = shell.run(
                    [
                        "glab",
                        "mr",
                        "view",
                        str(pr_number),
                        "--output",
                        "json",
                        *self._get_project_arg(),
                    ],
                    check=True,
                )

                data = json.loads(result.stdout)
                # GitLab では "blocked_by_user_notes" や "discussions" を確認
                # 簡易的に未解決のディスカッションがあるかチェック
                return data.get("has_conflicts", False)
            except Exception:
                return False
        return False

    def get_pr_for_branch(self, branch_name: str) -> PullRequest | None:
        """
        ブランチに紐づくMRを取得

        Args:
            branch_name: ブランチ名

        Returns:
            MR情報（存在しない場合はNone）
        """
        if self._use_glab:
            try:
                shell = ShellRunner()
                result = shell.run(
                    [
                        "glab",
                        "mr",
                        "list",
                        "--source-branch",
                        branch_name,
                        "--output",
                        "json",
                        *self._get_project_arg(),
                    ],
                    check=True,
                )

                mrs = json.loads(result.stdout) if result.stdout.strip() else []
                if mrs:
                    mr = mrs[0]
                    return PullRequest(
                        number=mr.get("iid"),
                        url=mr.get("web_url"),
                        title=mr.get("title", ""),
                        state=mr.get("state", "opened"),
                        draft=mr.get("draft", False),
                    )
                return None
            except Exception:
                return None
        return None
