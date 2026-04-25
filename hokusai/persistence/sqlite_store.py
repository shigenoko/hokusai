"""
SQLite Store

ワークフロー状態をSQLiteに保存する。
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class SQLiteStore:
    """SQLiteベースのワークフロー状態ストア"""

    def __init__(self, db_path: str | Path | None = None):
        """
        初期化

        Args:
            db_path: データベースファイルのパス
        """
        if db_path is None:
            db_dir = Path.home() / ".hokusai"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "workflow.db"

        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """WAL モード + busy_timeout 付きの接続を返す"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        """データベースを初期化"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    task_url TEXT NOT NULL,
                    task_title TEXT,
                    branch_name TEXT,
                    current_phase INTEGER DEFAULT 1,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    phase INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    phase INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_workflows_task_url
                ON workflows(task_url)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_checkpoints_workflow
                ON checkpoints(workflow_id, phase)
            """)

            conn.commit()

    def save_workflow(self, workflow_id: str, state: dict[str, Any]) -> None:
        """
        ワークフロー状態を保存

        Args:
            workflow_id: ワークフローID
            state: ワークフロー状態
        """
        now = datetime.now().isoformat()
        state_json = json.dumps(state, ensure_ascii=False, default=str)

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO workflows (
                    workflow_id, task_url, task_title, branch_name,
                    current_phase, state_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    task_title = excluded.task_title,
                    branch_name = excluded.branch_name,
                    current_phase = excluded.current_phase,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
            """, (
                workflow_id,
                state.get("task_url", ""),
                state.get("task_title", ""),
                state.get("branch_name", ""),
                state.get("current_phase", 1),
                state_json,
                now,
                now,
            ))
            conn.commit()

    def load_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """
        ワークフロー状態を読み込む

        Args:
            workflow_id: ワークフローID

        Returns:
            ワークフロー状態、存在しない場合はNone
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT state_json FROM workflows WHERE workflow_id = ?",
                (workflow_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            state = json.loads(row[0])
            # JSONシリアライズで文字列になったキーを整数に変換
            state = self._convert_keys_to_int(state)
            # 旧PRフィールドの移行
            state = self._migrate_legacy_pr_fields(state)
            # マルチリポジトリフィールドの欠損補完
            state = self._migrate_multi_repo_fields(state)
            return state

    def _convert_keys_to_int(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        phases, verificationなどの辞書キーを整数に変換

        JSONシリアライズで整数キーが文字列になる問題を修正
        """
        # phases辞書のキーを整数に変換
        if "phases" in state and isinstance(state["phases"], dict):
            state["phases"] = {
                int(k): v for k, v in state["phases"].items()
            }

        # verification辞書は文字列キーのままでOK

        # phase_subpages辞書のキーを整数に変換
        if "phase_subpages" in state and isinstance(state["phase_subpages"], dict):
            state["phase_subpages"] = {
                int(k): v for k, v in state["phase_subpages"].items()
            }

        # cross_review_results辞書のキーを整数に変換
        if "cross_review_results" in state and isinstance(state["cross_review_results"], dict):
            state["cross_review_results"] = {
                int(k): v for k, v in state["cross_review_results"].items()
            }

        # 現行の phase_page_* 辞書キーを整数に変換
        for key in (
            "phase_page_decision",
            "phase_page_last_human_note_at",
            "phase_page_recommended_action",
        ):
            if key in state and isinstance(state[key], dict):
                state[key] = {int(k): v for k, v in state[key].items()}

        return state

    def _migrate_legacy_pr_fields(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        旧PR関連フィールドを新しいpull_requestsリストに移行

        既存のデータベースレコードにbackend_pr_url等の旧フィールドが残っている場合、
        pull_requestsリストが空であればPullRequestInfoエントリとして変換する。
        移行後、旧フィールドはstateから削除する。
        """
        backend_pr_url = state.get("backend_pr_url")
        pull_requests = state.get("pull_requests", [])

        if backend_pr_url and not pull_requests:
            # 旧フィールドからPullRequestInfoを構築
            pr_entry = {
                "repo_name": "Backend",
                "title": state.get("backend_pr_title", ""),
                "url": backend_pr_url,
                "number": state.get("backend_pr_number", 0),
                "status": None,
                "github_status": None,
                "owner": None,
                "repo": None,
                "copilot_comments": None,
                "human_comments": None,
                "copilot_review_passed": None,
                "human_review_passed": None,
            }
            state["pull_requests"] = [pr_entry]
            if "current_pr_index" not in state:
                state["current_pr_index"] = 0

        # 旧フィールドを削除（存在する場合）
        state.pop("backend_pr_url", None)
        state.pop("backend_pr_number", None)
        state.pop("backend_pr_title", None)

        return state

    def _migrate_multi_repo_fields(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        マルチリポジトリ対応フィールドの欠損補完マイグレーション

        旧stateで以下のフィールドが欠損している場合、デフォルト値を設定:
        - repository_status: {} (deprecated - 後方互換性のため維持)
        - verification_errors: []
        - repositories: [] (単一情報源)
        - phase_page_*: {} (現行のフェーズページ補助情報)
        """
        # @deprecated: repository_status は後方互換性のため維持
        if "repository_status" not in state:
            state["repository_status"] = {}

        if "verification_errors" not in state:
            state["verification_errors"] = []

        if "repositories" not in state:
            state["repositories"] = []

        # worktree フィールドの後方互換: source_path / worktree_created が未設定の場合に補完
        for repo in state.get("repositories", []):
            if "source_path" not in repo:
                repo["source_path"] = repo.get("path", "")
            if "worktree_created" not in repo:
                repo["worktree_created"] = False

        if "phase_page_decision" not in state:
            state["phase_page_decision"] = {}
        if "phase_page_last_human_note_at" not in state:
            state["phase_page_last_human_note_at"] = {}
        if "phase_page_recommended_action" not in state:
            state["phase_page_recommended_action"] = {}

        # legacy 読込互換: 旧独立状態機械は読めれば十分で、再導入しない
        state.pop("phase_page_status", None)
        state.pop("phase_page_last_review_round", None)

        # Phase 10 (進捗記録) の後方互換性: 旧ワークフローにはPhase 10がない
        phases = state.get("phases", {})
        if 10 not in phases and "10" not in phases:
            phases[10] = {"status": "pending", "started_at": None,
                          "completed_at": None, "error_message": None, "retry_count": 0}
            state["phases"] = phases

        return state

    def find_workflow_by_task_url(self, task_url: str) -> dict[str, Any] | None:
        """
        タスクURLでワークフローを検索

        Args:
            task_url: NotionタスクURL

        Returns:
            最新のワークフロー状態、存在しない場合はNone
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT state_json FROM workflows
                WHERE task_url = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (task_url,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            state = json.loads(row[0])
            # JSONシリアライズで文字列になったキーを整数に変換
            state = self._convert_keys_to_int(state)
            # 旧PRフィールドの移行
            state = self._migrate_legacy_pr_fields(state)
            # マルチリポジトリフィールドの欠損補完
            state = self._migrate_multi_repo_fields(state)
            return state

    def list_active_workflows(self) -> list[dict[str, Any]]:
        """
        アクティブなワークフローの一覧を取得

        Returns:
            進行中のワークフロー一覧
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT workflow_id, task_url, task_title, current_phase, updated_at
                FROM workflows
                WHERE current_phase < 10
                ORDER BY updated_at DESC
                """
            )
            return [
                {
                    "workflow_id": row[0],
                    "task_url": row[1],
                    "task_title": row[2],
                    "current_phase": row[3],
                    "updated_at": row[4],
                }
                for row in cursor.fetchall()
            ]

    def save_checkpoint(
        self,
        workflow_id: str,
        phase: int,
        state: dict[str, Any],
    ) -> None:
        """
        チェックポイントを保存

        Args:
            workflow_id: ワークフローID
            phase: フェーズ番号
            state: 保存する状態
        """
        now = datetime.now().isoformat()
        state_json = json.dumps(state, ensure_ascii=False, default=str)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (workflow_id, phase, state_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (workflow_id, phase, state_json, now)
            )
            conn.commit()

    def load_checkpoint(
        self,
        workflow_id: str,
        phase: int | None = None,
    ) -> dict[str, Any] | None:
        """
        チェックポイントを読み込む

        Args:
            workflow_id: ワークフローID
            phase: フェーズ番号（省略時は最新）

        Returns:
            チェックポイントの状態、存在しない場合はNone
        """
        with self._connect() as conn:
            if phase is not None:
                cursor = conn.execute(
                    """
                    SELECT state_json FROM checkpoints
                    WHERE workflow_id = ? AND phase = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (workflow_id, phase)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT state_json FROM checkpoints
                    WHERE workflow_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (workflow_id,)
                )
            row = cursor.fetchone()
            if row is None:
                return None
            state = json.loads(row[0])
            # JSONシリアライズで文字列になったキーを整数に変換
            state = self._convert_keys_to_int(state)
            # 旧PRフィールドの移行
            state = self._migrate_legacy_pr_fields(state)
            # マルチリポジトリフィールドの欠損補完
            state = self._migrate_multi_repo_fields(state)
            return state

    def add_audit_log(
        self,
        workflow_id: str,
        phase: int,
        action: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """
        監査ログを追加

        Args:
            workflow_id: ワークフローID
            phase: フェーズ番号
            action: アクション名
            status: ステータス
            details: 詳細情報
        """
        now = datetime.now().isoformat()
        details_json = json.dumps(details, ensure_ascii=False, default=str) if details else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (workflow_id, phase, action, status, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (workflow_id, phase, action, status, details_json, now)
            )
            conn.commit()

    def get_audit_logs(self, workflow_id: str) -> list[dict[str, Any]]:
        """
        監査ログを取得

        Args:
            workflow_id: ワークフローID

        Returns:
            監査ログの一覧
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT phase, action, status, details_json, created_at
                FROM audit_logs
                WHERE workflow_id = ?
                ORDER BY created_at ASC
                """,
                (workflow_id,)
            )
            return [
                {
                    "phase": row[0],
                    "action": row[1],
                    "status": row[2],
                    "details": json.loads(row[3]) if row[3] else None,
                    "created_at": row[4],
                }
                for row in cursor.fetchall()
            ]

    def delete_workflow(self, workflow_id: str) -> None:
        """
        ワークフローを削除

        Args:
            workflow_id: ワークフローID
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM audit_logs WHERE workflow_id = ?",
                (workflow_id,)
            )
            conn.execute(
                "DELETE FROM checkpoints WHERE workflow_id = ?",
                (workflow_id,)
            )
            conn.execute(
                "DELETE FROM workflows WHERE workflow_id = ?",
                (workflow_id,)
            )
            conn.commit()

    def update_pr_status(
        self,
        workflow_id: str,
        pr_number: int,
        status: str | None = None,
        github_status: str | None = None,
        copilot_review_passed: bool | None = None,
        copilot_comments: list | None = None,
    ) -> tuple[bool, str]:
        """
        PRのステータスを更新

        Args:
            workflow_id: ワークフローID
            pr_number: PR番号
            status: ワークフロー内ステータス（pending, approved, changes_requested）
            github_status: GitHubステータス（draft, open, merged, closed）
            copilot_review_passed: Copilotレビュー結果
            copilot_comments: Copilotコメントリスト

        Returns:
            (成功フラグ, メッセージ)
        """
        state = self.load_workflow(workflow_id)
        if state is None:
            return False, f"ワークフロー '{workflow_id}' が見つかりません"

        pull_requests = state.get("pull_requests", [])
        if not pull_requests:
            return False, "PRが登録されていません"

        # PR番号で対象を検索
        target_pr = None
        for pr in pull_requests:
            if pr.get("number") == pr_number:
                target_pr = pr
                break

        if target_pr is None:
            pr_numbers = [pr.get("number") for pr in pull_requests]
            return False, f"PR #{pr_number} が見つかりません（登録済み: {pr_numbers}）"

        # ステータス更新
        changes = []
        if status is not None:
            old_status = target_pr.get("status")
            target_pr["status"] = status
            changes.append(f"status: {old_status} → {status}")

        if github_status is not None:
            old_github_status = target_pr.get("github_status")
            target_pr["github_status"] = github_status
            changes.append(f"github_status: {old_github_status} → {github_status}")

        if copilot_review_passed is not None:
            old_copilot = target_pr.get("copilot_review_passed")
            target_pr["copilot_review_passed"] = copilot_review_passed
            changes.append(f"copilot_review_passed: {old_copilot} → {copilot_review_passed}")

        if copilot_comments is not None:
            target_pr["copilot_comments"] = copilot_comments
            changes.append(f"copilot_comments: {len(copilot_comments)}件")

        if not changes:
            return False, "更新するステータスが指定されていません"

        # 保存
        self.save_workflow(workflow_id, state)

        return True, f"PR #{pr_number} を更新: {', '.join(changes)}"
