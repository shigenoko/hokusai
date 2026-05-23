"""
Task Backend Base

タスク管理バックエンドの抽象インターフェース。
"""

from abc import ABC, abstractmethod
from typing import Any, Protocol


class ResultValue(Protocol):
    """`.value: str` を持つ enum 風オブジェクトの最小プロトコル。

    `TaskOperationResult.result` の型として使い、`result.result.value` で
    audit log 用の文字列を取り出せることを型レベルで保証する。

    Note: 静的型チェック (mypy/pyright) 専用。`isinstance` での runtime
    検証は PEP 544 の制約上、属性の存在/型を確実に検証できないため
    `@runtime_checkable` は付けない。呼び出し側で runtime 判定が必要な
    場合は `hasattr(result, 'value')` を明示的に使うこと。

    実装例: `hokusai.integrations.task_backend.notion.NotionResult`
    （`str, Enum` を継承し `value` は str）
    """

    value: str


class TaskOperationResult(Protocol):
    """task backend 操作の構造化結果プロトコル。

    `update_status` / `append_progress` / `prepend_content` の戻り値の最小
    インターフェース。呼び出し側は `result.result.value` と `result.reason`
    を audit log の生成に使う。

    Note: `ResultValue` と同様、静的型チェック専用（`@runtime_checkable`
    なし）。呼び出し側 (`phase1_prepare` / `phase10_record` /
    `notion_helpers`) は `hasattr(result, 'result')` で runtime 分岐する。

    実装例: `hokusai.integrations.task_backend.notion.NotionOperationResult`
    """

    result: ResultValue  # `.value: str` を持つ enum 風オブジェクト
    reason: str | None


class TaskBackendClient(ABC):
    """タスク管理バックエンドの抽象インターフェース"""

    @abstractmethod
    def fetch_task(self, task_url: str) -> dict[str, Any]:
        """
        タスク情報を取得

        Args:
            task_url: タスクのURL（Notion URL, GitHub Issue URL等）

        Returns:
            タスク情報の辞書
            {
                "url": str,
                "title": str,
                "status": str,
                "properties": dict,
            }
        """
        ...

    @abstractmethod
    def update_status(
        self, task_url: str, status: str
    ) -> "TaskOperationResult | None":
        """
        ステータスを更新

        Args:
            task_url: タスクのURL
            status: 新しいステータス

        Returns:
            複数の backend が構造化結果（`TaskOperationResult` 準拠 dataclass）を
            返し得る。呼び出し側は `hasattr(result, 'result')` で audit log を
            生成する想定（phase1_prepare.py 参照）。Notion は
            `NotionOperationResult`、GitHub Issue は `GitHubIssueOperationResult`
            を返す（Issue #73 で追加）。`None` を返す実装も互換性のため許容。
        """
        ...

    @abstractmethod
    def append_progress(
        self, task_url: str, content: str
    ) -> "TaskOperationResult | None":
        """
        進捗記録を追記（末尾に追加）

        Args:
            task_url: タスクのURL
            content: 追記する内容（Markdown形式）

        Returns:
            実装依存。`update_status` と同じ規約。
        """
        ...

    @abstractmethod
    def prepend_content(
        self, task_url: str, content: str
    ) -> "TaskOperationResult | None":
        """
        コンテンツを先頭に追記

        Args:
            task_url: タスクのURL
            content: 先頭に追記する内容（Markdown形式）

        Returns:
            実装依存。`update_status` と同じ規約。
        """
        ...

    def get_task_title(self, task_info: dict) -> str:
        """
        タスク情報からタイトルを抽出

        Args:
            task_info: fetch_task() の戻り値

        Returns:
            タスクタイトル
        """
        return task_info.get("title", "")

    # 便利メソッド（デフォルト実装）

    def append_research_report(self, task_url: str, report: str) -> None:
        """事前調査レポートを追記"""
        self.append_progress(task_url, f"## 事前調査レポート\n\n{report}")

    def append_design_document(self, task_url: str, design: str) -> None:
        """設計書を追記"""
        self.append_progress(task_url, f"## 詳細設計書\n\n{design}")

    def append_work_plan(self, task_url: str, plan: str) -> None:
        """作業計画書を追記"""
        self.append_progress(task_url, f"## 作業計画書\n\n{plan}")

    def update_checkboxes(
        self,
        task_url: str,
        completed_items: list[str],
        section_hint: str | None = None,
    ) -> None:
        """
        タスクページ内のチェックボックスを更新

        Args:
            task_url: タスクのURL
            completed_items: 完了したアイテムのリスト（部分一致で検索）
            section_hint: チェックボックスを探すセクションのヒント（例: "開発計画"）
        """
        # デフォルトは何もしない（サブクラスでオーバーライド）
        pass

    def get_checkbox_items(
        self,
        task_url: str,
        section_hint: str | None = None,
    ) -> list[dict]:
        """
        タスクページ内のチェックボックス項目を取得

        Args:
            task_url: タスクのURL
            section_hint: チェックボックスを探すセクションのヒント

        Returns:
            チェックボックス項目のリスト
            [{"text": str, "checked": bool}, ...]
        """
        # デフォルトは空リスト
        return []

    def get_section_content(
        self,
        task_url: str,
        section_name: str,
    ) -> str | None:
        """
        タスクページ内の特定セクションのコンテンツを取得

        Args:
            task_url: タスクのURL
            section_name: セクション名（例: "開発計画", "事前調査結果"）

        Returns:
            セクションのコンテンツ（Markdown形式）、見つからない場合はNone
        """
        # デフォルトはNone（サブクラスでオーバーライド）
        return None
