"""
Git Hosting Base

Gitホスティングサービスの抽象インターフェース。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PullRequest:
    """プルリクエスト/マージリクエストの情報"""

    number: int
    url: str
    title: str
    state: str  # "open", "closed", "merged"
    draft: bool = False


@dataclass
class ReviewComment:
    """レビューコメントの情報"""

    id: int
    body: str
    path: str | None = None
    line: int | None = None
    author: str = ""
    replied: bool = False
    resolved: bool = False
    thread_id: str | None = None
    fix_summary: str | None = None
    comment_type: str = "review"  # "review" | "issue"

    def to_dict(self) -> dict:
        """辞書に変換（状態保存用）"""
        return {
            "id": self.id,
            "body": self.body,
            "path": self.path,
            "line": self.line,
            "author": self.author,
            "replied": self.replied,
            "resolved": self.resolved,
            "thread_id": self.thread_id,
            "fix_summary": self.fix_summary,
            "comment_type": self.comment_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewComment":
        """辞書から作成"""
        return cls(
            id=data.get("id", 0),
            body=data.get("body", ""),
            path=data.get("path"),
            line=data.get("line"),
            author=data.get("author", ""),
            replied=data.get("replied", False),
            resolved=data.get("resolved", False),
            thread_id=data.get("thread_id"),
            fix_summary=data.get("fix_summary"),
            comment_type=data.get("comment_type", "review"),
        )


class GitHostingClient(ABC):
    """Gitホスティングサービスの抽象インターフェース"""

    @abstractmethod
    def get_repo_info(self) -> tuple[str, str]:
        """
        リポジトリ情報を取得

        Returns:
            (owner, repo) のタプル
        """
        ...

    @abstractmethod
    def create_draft_pull_request(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequest:
        """
        Draft プルリクエストを作成

        Args:
            title: PRタイトル
            body: PR本文（Markdown）
            head_branch: マージ元ブランチ
            base_branch: マージ先ブランチ

        Returns:
            作成されたPR情報
        """
        ...

    @abstractmethod
    def mark_ready_for_review(self, pr_number: int) -> None:
        """
        Draft PRをReady for Reviewに変更

        Args:
            pr_number: PR番号
        """
        ...

    @abstractmethod
    def get_review_comments(
        self,
        pr_number: int,
        exclude_authors: list[str] | None = None,
    ) -> list[ReviewComment]:
        """
        レビューコメントを取得

        Args:
            pr_number: PR番号
            exclude_authors: 除外する著者のリスト

        Returns:
            レビューコメントのリスト
        """
        ...

    @abstractmethod
    def reply_to_comment(
        self,
        pr_number: int,
        comment_id: int,
        body: str,
    ) -> bool:
        """
        コメントに返信

        Args:
            pr_number: PR番号
            comment_id: コメントID
            body: 返信内容

        Returns:
            成功した場合True
        """
        ...

    @abstractmethod
    def resolve_thread(self, thread_id: str) -> bool:
        """
        スレッドを解決（Resolve conversation）

        Args:
            thread_id: スレッドID

        Returns:
            成功した場合True
        """
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    def is_changes_requested(self, pr_number: int) -> bool:
        """
        CHANGES_REQUESTED ステータスかどうかを確認

        Args:
            pr_number: PR番号

        Returns:
            変更要求がある場合True
        """
        ...

    @abstractmethod
    def get_pr_for_branch(self, branch_name: str) -> PullRequest | None:
        """
        ブランチに紐づくPRを取得

        Args:
            branch_name: ブランチ名

        Returns:
            PR情報（存在しない場合はNone）
        """
        ...

    def get_pr_approval_status(self, pr_number: int) -> dict:
        """
        PRの承認状態を取得（GitHub Approved / CI チェック）

        Args:
            pr_number: PR番号

        Returns:
            {
                "has_approval": bool,      # APPROVED reviewが存在する
                "checks_passing": bool | None,  # CIチェック結果 (None=不明/未実行)
            }

        Note:
            デフォルト実装では承認状態を「不明」として返す。
            各プロバイダー（GitHub, GitLab等）でオーバーライドして使用する。
        """
        return {
            "has_approval": False,
            "checks_passing": None,
        }

    def branch_exists_on_remote(self, branch_name: str) -> bool:
        """
        ブランチがリモートに存在するか確認

        Args:
            branch_name: ブランチ名

        Returns:
            存在する場合True
        """
        # デフォルト実装（サブクラスでオーバーライド可能）
        return False

    def push_branch(self, branch_name: str) -> bool:
        """
        ブランチをリモートにプッシュ

        Args:
            branch_name: ブランチ名

        Returns:
            成功した場合True
        """
        # デフォルト実装（サブクラスでオーバーライド可能）
        return False

    def get_pr_commit_count(self, pr_number: int) -> int | None:
        """PRのコミット数を取得

        Returns:
            コミット数（取得できない場合はNone）
        """
        return None

    def update_pr_body(self, pr_number: int, body: str) -> bool:
        """
        PR本文を更新

        Args:
            pr_number: PR番号
            body: 新しいPR本文（Markdown）

        Returns:
            成功した場合True
        """
        return False

    def get_pr_body(self, pr_number: int) -> str | None:
        """
        PR本文を取得

        Args:
            pr_number: PR番号

        Returns:
            PR本文（取得できない場合はNone）
        """
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
        return []

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
        return False

    def get_current_branch(self) -> str | None:
        """
        現在のブランチ名を取得

        Returns:
            ブランチ名（取得できない場合はNone）
        """
        # デフォルト実装
        return None
