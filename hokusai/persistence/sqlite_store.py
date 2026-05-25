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
                    updated_at TEXT NOT NULL,
                    profile_name TEXT
                )
            """)

            # Phase E migration: 既存 DB（v0.2.x 以前）に profile_name カラムが
            # 無い場合は ALTER TABLE で追加する。既存 row は NULL のまま残り、
            # display 時に (legacy) としてフォールバックされる。
            #
            # PRAGMA table_info で事前に存在判定する設計理由:
            # - try/except OperationalError を制御フローに使うと、起動の度に
            #   例外コストが発生する（profile_name カラムが既にある通常ケースで
            #   毎回 raise → catch を経由）
            # - エラーメッセージ文字列 "duplicate column name" への依存も避けたい
            #   （SQLite バージョン / ローカライズ次第で変わる可能性）
            # 事前判定により、ALTER TABLE は本当に必要な時（legacy DB の初回起動）
            # にだけ実行され、それ以外は通常経路で完結する
            cursor = conn.execute("PRAGMA table_info(workflows)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            if "profile_name" not in existing_columns:
                # この経路は v0.2.x 以前の DB を初めて v0.3.0 で開いた時のみ通る。
                # ただし、複数プロセスが同じ legacy DB を同時に初回起動した場合に
                # race condition が起きうる（両者が PRAGMA で「無い」と判定 →
                # 両方 ALTER TABLE 実行 → 片方が duplicate column で失敗）。
                # ALTER 部分だけは duplicate column を無害にスキップする。
                try:
                    conn.execute(
                        "ALTER TABLE workflows ADD COLUMN profile_name TEXT"
                    )
                except sqlite3.OperationalError as e:
                    # race で他プロセスが先に ALTER を完了した場合のみ無視。
                    # 他の OperationalError（DB lock 継続 / 破損等）は原因を
                    # 保持するため再 raise。
                    if "duplicate column name" not in str(e).lower():
                        raise

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

            # Notion ダッシュボード同期の outbox / error queue
            # 同期失敗イベントを保持し、復旧の正本として使う。
            # idempotency_key は同一イベントの重複送信を抑止する。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notion_sync_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    workflow_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_outbox_next_attempt
                ON notion_sync_outbox(next_attempt_at)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_outbox_workflow
                ON notion_sync_outbox(workflow_id)
            """)

            # 永続的な失敗（max_retry_attempts 超過）を記録する別テーブル
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notion_sync_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    failed_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sync_errors_workflow
                ON notion_sync_errors(workflow_id)
            """)

            # Phase E (v0.4.0): Figma / Miro 書き戻し（コメント / カード投稿）の
            # outbox / errors / idempotency テーブル。
            # 詳細は docs/hokusai-figma-miro-writeback-implementation-plan.md §5 を参照。
            #
            # 構造は notion_sync_outbox とほぼ同じだが以下が異なる:
            # - profile_name 列を追加（v0.3.0 整合）
            # - next_attempt_at なし（自動 retry なし、Operations Console からの手動再送のみ）
            # - attempt_count（attempts ではなく明示的な命名）
            #
            # 5 テーブル + 9 index:
            #   figma_sync_outbox + 2 idx / figma_sync_errors + 2 idx
            #   miro_sync_outbox + 2 idx / miro_sync_errors + 2 idx
            #   design_writeback_idempotency + 1 idx
            # errors 側の idempotency_key index は is_in_errors() の 3 段階チェック
            # で毎回引くために必要。

            # Figma 同期用 outbox
            conn.execute("""
                CREATE TABLE IF NOT EXISTS figma_sync_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    workflow_id TEXT NOT NULL,
                    profile_name TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_figma_outbox_workflow
                ON figma_sync_outbox(workflow_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_figma_outbox_event
                ON figma_sync_outbox(event_type)
            """)

            # Figma 同期用 errors（30 日経過で hokusai cleanup により自動削除、再送時の参照用）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS figma_sync_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    profile_name TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    failed_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_figma_errors_workflow
                ON figma_sync_errors(workflow_id)
            """)

            # writeback 3 段階チェック（idempotency → outbox → errors）で
            # is_in_errors() が毎回引くため、idempotency_key を index 化する。
            # errors が増えても dispatch / retry のレイテンシが O(1)。
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_figma_errors_idempotency
                ON figma_sync_errors(idempotency_key)
            """)

            # Miro 同期用 outbox（構造は figma と同じ）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS miro_sync_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    workflow_id TEXT NOT NULL,
                    profile_name TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_miro_outbox_workflow
                ON miro_sync_outbox(workflow_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_miro_outbox_event
                ON miro_sync_outbox(event_type)
            """)

            # Miro 同期用 errors（30 日経過で hokusai cleanup により自動削除、再送時の参照用）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS miro_sync_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    profile_name TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    failed_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_miro_errors_workflow
                ON miro_sync_errors(workflow_id)
            """)

            # writeback 3 段階チェック（is_in_errors）用 index。Figma 側と同じ意図。
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_miro_errors_idempotency
                ON miro_sync_errors(idempotency_key)
            """)

            # 冪等キー記録（§9.2 参照、API call 成功後の重複抑止用）
            # Figma / Miro REST API のいずれにも idempotency key 受け渡しの仕組みは
            # 存在しないため、HOKUSAI 側で成功済み idempotency_key を永続化して
            # dispatcher の入口で事前チェックする。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS design_writeback_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    profile_name TEXT,
                    target TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    response_id TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_writeback_idempotency_workflow
                ON design_writeback_idempotency(workflow_id)
            """)

            # Figma / Miro 取得結果のキャッシュ。MVP では fetch ごとに
            # cache_ttl_seconds を比較して上書き保存する。
            # cache_key:
            #   figma: "figma:<file_key>:<node_id_or_root>"
            #   miro:  "miro:<board_id>"
            conn.execute("""
                CREATE TABLE IF NOT EXISTS figma_file_cache (
                    cache_key TEXT PRIMARY KEY,
                    file_key TEXT NOT NULL,
                    node_id TEXT,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_figma_cache_file
                ON figma_file_cache(file_key)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS miro_board_cache (
                    cache_key TEXT PRIMARY KEY,
                    board_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_miro_cache_board
                ON miro_board_cache(board_id)
            """)

            conn.commit()

    def save_workflow(self, workflow_id: str, state: dict[str, Any]) -> None:
        """
        ワークフロー状態を保存

        Args:
            workflow_id: ワークフローID
            state: ワークフロー状態

        state に "profile_name" キーが含まれていれば、Phase E で追加した
        workflows.profile_name カラムに保存する。state にキーが無い場合は
        既存値を維持する（UPDATE では profile_name を上書きしない）。
        """
        now = datetime.now().isoformat()
        state_json = json.dumps(state, ensure_ascii=False, default=str)
        profile_name = state.get("profile_name")

        with self._connect() as conn:
            conn.execute("""
                INSERT INTO workflows (
                    workflow_id, task_url, task_title, branch_name,
                    current_phase, state_json, created_at, updated_at,
                    profile_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    task_title = excluded.task_title,
                    branch_name = excluded.branch_name,
                    current_phase = excluded.current_phase,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at,
                    profile_name = COALESCE(excluded.profile_name, workflows.profile_name)
            """, (
                workflow_id,
                state.get("task_url", ""),
                state.get("task_title", ""),
                state.get("branch_name", ""),
                state.get("current_phase", 1),
                state_json,
                now,
                now,
                profile_name,
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

    def get_workflow_profile_name(self, workflow_id: str) -> str | None:
        """Phase E: workflow に紐づく profile_name を返す。

        Args:
            workflow_id: 対象 workflow

        Returns:
            profile_name（v0.3.0 以降に作成された workflow なら設定される）。
            workflow が存在しない、または v0.2.x 以前の legacy 行なら None。
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT profile_name FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return row[0]

    def workflow_exists(self, workflow_id: str) -> bool:
        """Phase E: workflow がこの DB に存在するか確認。

        他 profile 探索ロジックで使う（ProfileSafetyError の候補列挙）。
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM workflows WHERE workflow_id = ? LIMIT 1",
                (workflow_id,),
            )
            return cursor.fetchone() is not None

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
        return True, "PR ステータスを更新しました: " + ", ".join(changes)

    # =========================================================================
    # Notion ダッシュボード同期 outbox / error queue
    # =========================================================================

    def enqueue_notion_sync(
        self,
        idempotency_key: str,
        workflow_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """同期失敗イベントを outbox に追加する。

        既に同じ idempotency_key が存在する場合は何もしない（冪等）。

        Returns:
            新規追加された場合 True、既存（重複）の場合 False
        """
        now = datetime.now().isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO notion_sync_outbox (
                    idempotency_key, workflow_id, event_type, payload_json,
                    attempts, last_error, created_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, 0, NULL, ?, ?)
                """,
                (idempotency_key, workflow_id, event_type, payload_json, now, now),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_pending_notion_sync(self, limit: int = 100) -> list[dict[str, Any]]:
        """送信待ちの outbox エントリを古い順に取得する。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT id, idempotency_key, workflow_id, event_type, payload_json,
                       attempts, last_error, created_at, next_attempt_at
                FROM notion_sync_outbox
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "idempotency_key": row["idempotency_key"],
                    "workflow_id": row["workflow_id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "attempts": row["attempts"],
                    "last_error": row["last_error"],
                    "created_at": row["created_at"],
                    "next_attempt_at": row["next_attempt_at"],
                }
                for row in rows
            ]

    def mark_notion_sync_succeeded(self, idempotency_key: str) -> None:
        """outbox エントリを送信成功として削除する。"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM notion_sync_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            conn.commit()

    def mark_notion_sync_failed(
        self,
        idempotency_key: str,
        error: str,
        next_attempt_at: str,
    ) -> None:
        """outbox エントリの試行回数とエラーを更新する。"""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE notion_sync_outbox
                SET attempts = attempts + 1,
                    last_error = ?,
                    next_attempt_at = ?
                WHERE idempotency_key = ?
                """,
                (error, next_attempt_at, idempotency_key),
            )
            conn.commit()

    def move_notion_sync_to_error(
        self,
        idempotency_key: str,
        error: str,
    ) -> None:
        """outbox エントリを permanent error として errors テーブルに移す。"""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT idempotency_key, workflow_id, event_type, payload_json, attempts
                FROM notion_sync_outbox
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """
                INSERT INTO notion_sync_errors (
                    idempotency_key, workflow_id, event_type, payload_json,
                    error, attempts, failed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["idempotency_key"],
                    row["workflow_id"],
                    row["event_type"],
                    row["payload_json"],
                    error,
                    row["attempts"],
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM notion_sync_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            conn.commit()

    def has_failed_workflow_started(self, workflow_id: str) -> bool:
        """指定 workflow の `workflow_started` イベントが既に永続失敗（errors 入り）か判定。

        Issue #109 / fail-fast モード用 helper。`notion_sync_errors` テーブルに
        該当 workflow_id × event_type='workflow_started' の行が 1 件でもあれば True。
        dispatcher は新規子イベントを enqueue 前にこれを呼び、True なら outbox を
        skip して errors テーブルに直送する。

        Returns:
            True: workflow_started が永続失敗で errors に入っている
            False: 永続失敗していない（成功済み / pending / 未試行）
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM notion_sync_errors
                WHERE workflow_id = ? AND event_type = 'workflow_started'
                LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
            return row is not None

    def record_permanent_notion_sync_failure(
        self,
        *,
        idempotency_key: str,
        workflow_id: str,
        event_type: str,
        payload: dict[str, Any],
        error: str,
    ) -> None:
        """outbox を経由せず直接 `notion_sync_errors` に行を挿入する。

        Issue #109 / fail-fast モード用。`workflow_started` が既に永続失敗している
        環境で、子イベント（pr_created / phase_changed 等）を発生時点で
        errors テーブルに直送する用途。

        `attempts=0` で記録する（retry を一度も試みていないため）。
        既存の `move_notion_sync_to_error` は outbox 経由のため、新規行を
        直接入れる別 helper として用意する。
        """
        import json

        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notion_sync_errors (
                    idempotency_key, workflow_id, event_type, payload_json,
                    error, attempts, failed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    workflow_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    error,
                    0,
                    now,
                ),
            )
            conn.commit()

    def count_notion_sync_pending(self) -> int:
        """outbox の保留件数。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM notion_sync_outbox"
            ).fetchone()
            return int(row[0]) if row else 0

    def count_notion_sync_errors(self) -> int:
        """permanent error の件数。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM notion_sync_errors"
            ).fetchone()
            return int(row[0]) if row else 0

    def fetch_recent_outbox_with_errors(
        self, limit: int = 5
    ) -> list[dict[str, Any]]:
        """outbox の中で `last_error` が記録されているもののうち next_attempt_at が近い順に N 件を返す。

        Issue #84 / M0.3 で `hokusai status --verbose` に「直近の Notion 同期失敗」
        を抜粋表示する用途。retry 中（永続 error 化前）の問題を可視化する。
        `next_attempt_at` の昇順（古いタイムスタンプ = まもなく次の試行が走る
        もの）で返すことで、ユーザーは「次に何が再試行されるか」を上から順に
        確認できる。

        Args:
            limit: 取得件数（既定 5、CLI でも 5 件 ぐらいで十分）

        Returns:
            `[{"event_type": str, "workflow_id": str, "attempts": int, "last_error": str,
                 "next_attempt_at": str}, ...]` の dict のリスト。next_attempt_at
            昇順で並ぶ（リトライが直近に予定されているものを優先表示。古い
            タイムスタンプ = まもなく次の試行が走るもの、を先頭にする。
            Issue #84 Copilot Round 1 で docstring と ASC 実装の整合を取った）。
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT event_type, workflow_id, attempts, last_error, next_attempt_at
                FROM notion_sync_outbox
                WHERE last_error IS NOT NULL AND last_error != ''
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                {
                    "event_type": row["event_type"],
                    "workflow_id": row["workflow_id"],
                    "attempts": row["attempts"],
                    "last_error": row["last_error"],
                    "next_attempt_at": row["next_attempt_at"],
                }
                for row in cursor.fetchall()
            ]

    # ============================================================
    # Figma / Miro 取得結果のキャッシュ操作
    #
    # Phase A 段階の MVP では: get → expires_at <= now なら miss、
    # それ以外は payload を返す。put は同じ cache_key の上書き。
    # ここでは TTL 計算は呼び出し側に任せ、本ストアは保存と取得のみ提供する。
    # ============================================================

    def get_figma_cache(self, cache_key: str) -> dict[str, Any] | None:
        """Figma キャッシュを取得（期限切れは None を返す）。"""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT cache_key, file_key, node_id, payload_json, fetched_at, expires_at
                FROM figma_file_cache
                WHERE cache_key = ? AND expires_at > ?
                """,
                (cache_key, now),
            ).fetchone()
            if row is None:
                return None
            return {
                "cache_key": row["cache_key"],
                "file_key": row["file_key"],
                "node_id": row["node_id"],
                "payload": json.loads(row["payload_json"]),
                "fetched_at": row["fetched_at"],
                "expires_at": row["expires_at"],
            }

    def put_figma_cache(
        self,
        cache_key: str,
        file_key: str,
        node_id: str | None,
        payload: dict[str, Any],
        expires_at: str,
    ) -> None:
        """Figma キャッシュを upsert する。"""
        now = datetime.now().isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO figma_file_cache (
                    cache_key, file_key, node_id, payload_json, fetched_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    file_key = excluded.file_key,
                    node_id = excluded.node_id,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (cache_key, file_key, node_id, payload_json, now, expires_at),
            )
            conn.commit()

    def get_miro_cache(self, cache_key: str) -> dict[str, Any] | None:
        """Miro キャッシュを取得（期限切れは None を返す）。"""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT cache_key, board_id, payload_json, fetched_at, expires_at
                FROM miro_board_cache
                WHERE cache_key = ? AND expires_at > ?
                """,
                (cache_key, now),
            ).fetchone()
            if row is None:
                return None
            return {
                "cache_key": row["cache_key"],
                "board_id": row["board_id"],
                "payload": json.loads(row["payload_json"]),
                "fetched_at": row["fetched_at"],
                "expires_at": row["expires_at"],
            }

    def put_miro_cache(
        self,
        cache_key: str,
        board_id: str,
        payload: dict[str, Any],
        expires_at: str,
    ) -> None:
        """Miro キャッシュを upsert する。"""
        now = datetime.now().isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO miro_board_cache (
                    cache_key, board_id, payload_json, fetched_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    board_id = excluded.board_id,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (cache_key, board_id, payload_json, now, expires_at),
            )
            conn.commit()

    def clear_figma_cache(self) -> int:
        """Figma キャッシュテーブルを全削除し、削除行数を返す。

        Operations Console の「キャッシュ再取得」経路から呼ばれる公開 API。
        外部から `_connect()` 等の private API に依存せずキャッシュをクリア
        できるようにするため SQLiteStore 側に集約する。
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM figma_file_cache")
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    def clear_miro_cache(self) -> int:
        """Miro キャッシュテーブルを全削除し、削除行数を返す。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM miro_board_cache")
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    # workflow_id をキーに持つ依存テーブル（M2.5 / #100 cascade-delete 用）。
    # workflows テーブル自体を含めず、cascade 対象の dependent table のみ列挙。
    # レガシー DB でテーブル不在の場合は skip するため try/except で個別に処理。
    _WORKFLOW_DEPENDENT_TABLES: tuple[str, ...] = (
        "checkpoints",
        "audit_logs",
        "notion_sync_outbox",
        "notion_sync_errors",
        "figma_sync_outbox",
        "figma_sync_errors",
        "miro_sync_outbox",
        "miro_sync_errors",
        "design_writeback_idempotency",
    )

    def delete_old_completed_workflows(
        self, retention_days: int = 90
    ) -> dict[str, int]:
        """完了済み workflow (current_phase >= 10) のうち `updated_at` が
        `retention_days` 日以上前のものを cascade 削除する（Issue #100 / M2.5）。

        findings §4.1: cleanup 後も `workflows` 行が残って DB が肥大化する
        運用穴への対応。`hokusai cleanup --gc-workflows` から opt-in で
        呼ばれる前提（自動実行はしない）。

        Cascade 削除対象:
        - `workflows`（target）
        - `_WORKFLOW_DEPENDENT_TABLES` に列挙した workflow_id-keyed テーブル
          （checkpoints / audit_logs / notion_sync_outbox / errors / figma_*
          / miro_* / design_writeback_idempotency）

        安全性:
        - `current_phase >= 10` のみ対象。進行中 workflow は絶対に削除しない。
        - `retention_days < 1` は ValueError（最小 1 日の保持を強制）。
        - レガシー DB で dependent table が存在しないケースは
          `sqlite_master` を事前 query して存在テーブルのみ対象とする方式で
          skip する（PR #101 Copilot Round 1 #1 指摘で try/except 握り潰しを
          廃止。これにより DB lock / I/O error / SQL typo 等の真の異常は正しく
          上位に伝播する）。
        - 単一 transaction 内で実行（依存テーブル削除途中で失敗しても
          workflows は残し、全体 rollback される）。

        Args:
            retention_days: 保持期間（日数）。`updated_at` がこの日数以上
                前の completed workflow が削除対象。既定 90 日。

        Returns:
            `{"workflows": N, "checkpoints": N, ...}` 形式の per-table 実
            削除件数辞書（chunk ごとの `cursor.rowcount` を加算した実数で、
            SELECT 後の race condition でズレないようにする。PR #101 Copilot
            Round 2 #2 指摘）。dependent table が `sqlite_master` で不在なら
            0 で含まれる。

        Raises:
            ValueError: `retention_days < 1`
        """
        if retention_days < 1:
            raise ValueError(
                f"retention_days must be >= 1, got {retention_days}"
            )

        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        counts: dict[str, int] = {"workflows": 0}
        for table in self._WORKFLOW_DEPENDENT_TABLES:
            counts[table] = 0

        with self._connect() as conn:
            # 1. 対象 workflow_id を取得（completed + updated_at 古い）
            cursor = conn.execute(
                "SELECT workflow_id FROM workflows "
                "WHERE current_phase >= 10 AND updated_at < ?",
                (cutoff,),
            )
            target_ids = [row[0] for row in cursor.fetchall()]
            if not target_ids:
                return counts

            # 2. SQLite の SQL 変数上限を考慮し、IN クエリは 500 件ずつ分割
            #    （デフォルト SQLITE_MAX_VARIABLE_NUMBER = 999、安全側で 500）
            chunk_size = 500

            # 3. PR #101 Copilot Round 1 #1 指摘: dependent table の DELETE で
            #    sqlite3.OperationalError を無条件に握り潰すと "no such table"
            #    以外（DB lock / I/O / SQL typo）も黙殺して workflows だけ削除
            #    された不整合状態になり得る。sqlite_master で実在テーブル一覧
            #    を事前取得し、存在するものだけ DELETE することで、それ以外の
            #    エラーは正しく上位に伝播するようにする。
            existing_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

            # 4. 依存テーブルの cascade 削除（実在するもののみ）
            for table in self._WORKFLOW_DEPENDENT_TABLES:
                if table not in existing_tables:
                    # レガシー DB で table 不在 → skip
                    counts[table] = 0
                    continue
                deleted = 0
                for i in range(0, len(target_ids), chunk_size):
                    chunk = target_ids[i:i + chunk_size]
                    placeholders = ",".join("?" * len(chunk))
                    sub_cursor = conn.execute(
                        f"DELETE FROM {table} "
                        f"WHERE workflow_id IN ({placeholders})",
                        chunk,
                    )
                    deleted += sub_cursor.rowcount
                counts[table] = deleted

            # 5. 最後に workflows 自体を削除（PR #101 Copilot Round 2 #2 指摘:
            #    len(target_ids) ではなく実際の rowcount を加算し、SELECT 後の
            #    race condition で別プロセスが消した場合の counts ズレを防ぐ）
            workflows_deleted = 0
            for i in range(0, len(target_ids), chunk_size):
                chunk = target_ids[i:i + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    f"DELETE FROM workflows "
                    f"WHERE workflow_id IN ({placeholders})",
                    chunk,
                )
                workflows_deleted += cursor.rowcount
            counts["workflows"] = workflows_deleted

            conn.commit()

        return counts
