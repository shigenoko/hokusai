"""Notion 同期 dispatcher

同期イベントを受け、まず直接送信を試みる。
失敗した場合は SQLite outbox に積み、後で再送できるようにする。

設計方針:
- 通常パス: イベント受信 → Notion API 呼び出し → 成功でリターン
- 失敗パス: outbox に積み（冪等キーで重複抑止）、warn ログを出して呼び出し元には例外を上げない（best effort）
- 再送パス（Operations Console から呼ばれる想定）: outbox の保留分を順に再送、max_retry_attempts 超過は errors テーブルへ
- ワークフロー本体は Notion 障害で止まらないことを保証する
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient, NotionAPIError, NotionRateLimitError
from .project_memory_db import ProjectMemoryDBClient
from .pull_requests_db import PullRequestsDBClient
from .review_issues_db import ReviewIssuesDBClient
from .work_items_db import WorkItemsDBClient
from .workflow_gates_db import WorkflowGatesDBClient
from .workflows_db import WorkflowsDBClient

logger = get_logger("integrations.notion_dashboard.dispatcher")


# Review Issue 発火イベント。Phase 6 verification failure / Phase 7 final review 等
# から dispatch される。payload 構造は _handle_review_issue_raised を参照。
EVENT_REVIEW_ISSUE_RAISED = "review_issue_raised"
# Work Item upsert イベント（Workgraph Phase 2 / Issue #38）。Phase 4 plan ノード
# が work_plan から抽出した Work Item を Notion に同期する際に dispatch される。
# payload 構造は _handle_work_item_upsert を参照。
# **Status 遷移は本イベントでは扱わない**: upsert ハンドラは Notion 側 Status を
# 意図的に温存する（再 dispatch で人手や Phase 5 の状態遷移を巻き戻さないため）。
# 明示的な状態遷移には別イベント EVENT_WORK_ITEM_STATUS_CHANGE を使う。
EVENT_WORK_ITEM_UPSERT = "work_item_upsert"
# Work Item status 遷移専用イベント（Issue #38）。upsert は Status を温存する
# ため、Phase 5 implement の in_progress → done のような明示的遷移はこちらを
# 使う。payload は (workflow_id / title / phase / status) の最小セット +
# dedupe_key（省略時は build_dedupe_key で再生成）。dedupe_key で Work Item を
# 同定し、Notion 側で見つからない場合は warning で skip する（race condition
# で後段から発生したケースは Phase 5 完了時点で必ず Work Item が存在する前提）。
EVENT_WORK_ITEM_STATUS_CHANGE = "work_item_status_change"
# Work Item claim / lease release イベント（Workgraph Phase 3 / Issue #42）。
# Phase 5 implement 開始時に claim を、正常完了時に release を enqueue する。
# payload は (workflow_id / title / phase / dedupe_key / claimed_by /
# claim_type / lease_duration_seconds)。Status 遷移と同じく find_by_dedupe_key
# で Work Item を同定し、見つからない場合は race condition なら 503 defer、
# それ以外は warning skip。失敗時に lease を残すことで期限切れ再割当を
# 機能させる（要件 §6.6: lease が期限切れの場合、人間または HOKUSAI が再
# 割当できる）。
EVENT_WORK_ITEM_CLAIM = "work_item_claim"
EVENT_WORK_ITEM_LEASE_RELEASE = "work_item_lease_release"
# Workflow Gate イベント（Workgraph Phase 4 / Issue #44）。Phase 4 plan ノード
# や Phase 6 verify / Phase 7 review が gate を Notion に登録する際に
# dispatch される。状態遷移（pending → open / blocked / expired）は別 event
# `workflow_gate_status_change` で扱う（upsert は Status 温存）。
EVENT_GATE_UPSERT = "workflow_gate_upsert"
EVENT_GATE_STATUS_CHANGE = "workflow_gate_status_change"
# Project Memory イベント（Workgraph Phase 5 / Issue #46）。Agent や人間が
# project memory（rule / decision / avoidance / handover note 等）を起こす
# 際に dispatch される。状態遷移（draft → active / deprecated / rejected）は
# `project_memory_status_change` で扱う（upsert は Status 温存、要件 §8.5
# の「Agent 自動生成は draft 起票」を保ったまま人間承認を巻き戻さない）。
EVENT_PROJECT_MEMORY_UPSERT = "project_memory_upsert"
EVENT_PROJECT_MEMORY_STATUS_CHANGE = "project_memory_status_change"


class NotionSyncDispatcher:
    """Notion 同期イベントを直接送信／outbox 経由で送信する薄い dispatcher。

    Args:
        store: SQLiteStore（outbox / errors の永続化）
        config: NotionDashboardConfig
    """

    def __init__(self, store, config):
        self._store = store
        self._config = config
        self._api: NotionAPIClient | None = None
        self._workflows_db: WorkflowsDBClient | None = None
        self._pull_requests_db: PullRequestsDBClient | None = None
        self._review_issues_db: ReviewIssuesDBClient | None = None
        self._work_items_db: WorkItemsDBClient | None = None
        self._workflow_gates_db: WorkflowGatesDBClient | None = None
        self._project_memory_db: ProjectMemoryDBClient | None = None
        # workflow_id → page_id の positive-cache（PR #37 Copilot 7 回目指摘）。
        # Phase 6/7 drain で同一 workflow の review_issue を複数 dispatch する際、
        # 各 dispatch で Workflows DB に lookup query を投げないよう抑止する。
        # 「ページが存在しない」negative 結果はキャッシュしない（workflow_started
        # 再送で後から作成される可能性があるため）。新たな workflow_started イベント
        # を扱う際は対応エントリを invalidate する。
        self._workflow_page_id_cache: dict[str, str] = {}

    def is_configured(self) -> bool:
        """設定が enabled で、必要な環境変数が揃っているかを返す。"""
        if not self._config.enabled:
            return False
        if not os.environ.get(self._config.api_token_env):
            return False
        if not os.environ.get(self._config.workflows_db_id_env):
            return False
        return True

    def resolve_workflow_page_url(self, workflow_id: str) -> str | None:
        """workflow_id に対応する Notion ページ URL を解決する。

        Slack 通知のディープリンク用。is_configured=False や API 失敗で None。
        """
        if not self.is_configured():
            return None
        try:
            return self._get_workflows_client().get_workflow_page_url(workflow_id)
        except Exception as e:
            logger.debug(f"Notion ページ URL 解決失敗: workflow_id={workflow_id}, error={e}")
            return None

    def dispatch(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> bool:
        """イベントを Notion へ送信する。

        Returns:
            送信成功 True、enabled=False または送信失敗（outbox に積んだ）で False
        """
        if not self.is_configured():
            return False

        workflow_id = payload.get("workflow_id")
        if not workflow_id:
            logger.debug(f"Notion 同期: workflow_id 欠落のためスキップ: event={event_type}")
            return False

        if idempotency_key is None:
            idempotency_key = self._build_idempotency_key(event_type, payload)

        try:
            self._send_to_notion(event_type, payload)
            # 既に outbox にあれば成功として削除
            self._store.mark_notion_sync_succeeded(idempotency_key)
            return True
        except (NotionAPIError, NotionRateLimitError, OSError) as e:
            self._enqueue_failure(idempotency_key, workflow_id, event_type, payload, e)
            return False
        except Exception as e:
            # 想定外の例外も呼び出し元には伝播させない（best effort）
            logger.warning(
                f"Notion 同期で予期しない例外: event={event_type}, "
                f"error={type(e).__name__}"
            )
            self._enqueue_failure(idempotency_key, workflow_id, event_type, payload, e)
            return False

    def retry_pending(self, *, limit: int = 50) -> dict[str, int]:
        """outbox の保留分を順に再送する。Operations Console から呼ばれる想定。

        Returns:
            {"succeeded": N, "failed": N, "moved_to_error": N}
        """
        if not self.is_configured():
            return {"succeeded": 0, "failed": 0, "moved_to_error": 0}

        succeeded = 0
        failed = 0
        moved_to_error = 0
        max_attempts = self._config.sync_outbox.max_retry_attempts

        for entry in self._store.list_pending_notion_sync(limit=limit):
            key = entry["idempotency_key"]
            attempts = entry["attempts"]
            event_type = entry["event_type"]
            payload = entry["payload"]

            try:
                # 自エントリは「これから削除される」状態として、Sync Errors の集計から除外
                # これで最後の保留 1 件を再送成功した瞬間に Notion 上の Sync Errors が空になる
                self._send_to_notion(
                    event_type, payload, exclude_idempotency_key=key
                )
                self._store.mark_notion_sync_succeeded(key)
                succeeded += 1
            except Exception as e:
                error_message = self._safe_error_message(e)
                if attempts + 1 >= max_attempts:
                    self._store.move_notion_sync_to_error(key, error_message)
                    moved_to_error += 1
                else:
                    next_at = (datetime.now() + timedelta(
                        seconds=self._config.retry.backoff_seconds * (attempts + 1)
                    )).isoformat()
                    self._store.mark_notion_sync_failed(key, error_message, next_at)
                    failed += 1

        return {
            "succeeded": succeeded,
            "failed": failed,
            "moved_to_error": moved_to_error,
        }

    def _send_to_notion(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        exclude_idempotency_key: str | None = None,
    ) -> None:
        """イベント種別に応じて適切なドメインクライアントへルーティング。

        - pr_created: Pull Requests DB に新規レコード作成（重複は find_by_pr_number で抑止）
        - その他のワークフロー系イベント: Workflows DB に反映

        Args:
            exclude_idempotency_key: outbox 件数を集計するときに除外する自己エントリの key。
                retry_pending() からの呼び出しでは、当該 outbox エントリを「これから削除する」
                状態にあるため、サマリ計算では除外する必要がある。
        """
        # workflow_started を扱う前に対応 workflow の page id cache を invalidate
        # （Copilot 7 回目指摘の正確性確保。新規 page が作られた可能性があるため）。
        if event_type == "workflow_started":
            wid_invalidate = payload.get("workflow_id")
            if wid_invalidate:
                self._workflow_page_id_cache.pop(wid_invalidate, None)

        if event_type == "pr_created":
            payload = self._enrich_with_sync_status(
                payload, exclude_idempotency_key=exclude_idempotency_key
            )
            self._handle_pr_created(payload)
            return

        if event_type == EVENT_REVIEW_ISSUE_RAISED:
            # Review Issues DB 系は Workflows DB の Last Sync / Sync Errors とは
            # 別軸の同期。enrich は不要。retry_pending() からの呼び出しでは、
            # 自己 entry を race 検出から除外するため exclude_idempotency_key
            # を forward する（PR #37 Copilot 5 回目指摘）。
            self._handle_review_issue_raised(
                payload, exclude_idempotency_key=exclude_idempotency_key
            )
            return

        if event_type == EVENT_WORK_ITEM_UPSERT:
            # Work Items DB 系も Workflows DB の sync_errors とは別軸の同期。
            # Review Issues と同じく enrich 不要。retry 経由なら自己 entry を
            # race 検出から除外。
            self._handle_work_item_upsert(
                payload, exclude_idempotency_key=exclude_idempotency_key
            )
            return

        if event_type == EVENT_WORK_ITEM_STATUS_CHANGE:
            self._handle_work_item_status_change(payload)
            return

        if event_type == EVENT_WORK_ITEM_CLAIM:
            self._handle_work_item_claim(payload)
            return

        if event_type == EVENT_WORK_ITEM_LEASE_RELEASE:
            self._handle_work_item_lease_release(payload)
            return

        if event_type == EVENT_GATE_UPSERT:
            self._handle_workflow_gate_upsert(payload)
            return

        if event_type == EVENT_GATE_STATUS_CHANGE:
            self._handle_workflow_gate_status_change(payload)
            return

        if event_type == EVENT_PROJECT_MEMORY_UPSERT:
            self._handle_project_memory_upsert(payload)
            return

        if event_type == EVENT_PROJECT_MEMORY_STATUS_CHANGE:
            self._handle_project_memory_status_change(payload)
            return

        # 後方互換: 旧 Service Status sync が outbox に積んだ
        # service_status_checked エントリは Notion 連携廃止済みなので
        # no-op として扱い、retry_pending() で drain できるようにする。
        if event_type == "service_status_checked":
            logger.info(
                "service_status_checked は廃止済みのため no-op で drain します"
            )
            return

        # workflow_started / phase_changed / phase_artifact_linked /
        # terminal_status_changed は Workflows DB へ
        # Last Sync / Sync Errors を含めて書き戻す
        payload = self._enrich_with_sync_status(
            payload, exclude_idempotency_key=exclude_idempotency_key
        )
        client = self._get_workflows_client()
        client.apply_event(event_type, payload)

    def _enrich_with_sync_status(
        self,
        payload: dict[str, Any],
        *,
        exclude_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """payload に last_sync と sync_errors を補う（Notion 側の表示用サマリ）。

        - last_sync: 現在時刻（同期送信成功した瞬間として扱う）
        - sync_errors: 当該 workflow_id に紐づく outbox / errors の件数を文字列化

        Args:
            exclude_idempotency_key: 件数集計から除外する outbox エントリの冪等キー。
                retry_pending() で「再送中の自エントリ」を除外するために使う。
        """
        workflow_id = payload.get("workflow_id")
        if not workflow_id:
            return payload

        enriched = dict(payload)
        enriched["last_sync"] = datetime.now().isoformat()

        try:
            pending = self._count_pending_for(
                workflow_id, exclude_key=exclude_idempotency_key
            )
            errors = self._count_errors_for(workflow_id)
        except Exception:
            pending = 0
            errors = 0

        if pending == 0 and errors == 0:
            enriched["sync_errors"] = ""
        else:
            parts: list[str] = []
            if pending > 0:
                parts.append(f"保留 {pending} 件")
            if errors > 0:
                parts.append(f"永続失敗 {errors} 件")
            enriched["sync_errors"] = " / ".join(parts)
        return enriched

    def _count_pending_for(
        self, workflow_id: str, *, exclude_key: str | None = None
    ) -> int:
        """SQLite outbox 上の当該 workflow_id の保留件数。

        exclude_key 指定時は、その idempotency_key を持つエントリを除外して数える。
        """
        if self._store is None:
            return 0
        # SQLite に直接 SQL で集計（list_pending を全件読むより軽い）
        try:
            with self._store._connect() as conn:  # type: ignore[attr-defined]
                if exclude_key:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM notion_sync_outbox "
                        "WHERE workflow_id = ? AND idempotency_key != ?",
                        (workflow_id, exclude_key),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM notion_sync_outbox WHERE workflow_id = ?",
                        (workflow_id,),
                    ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def _count_pending_workflow_page_events_for(
        self, workflow_id: str, *, exclude_key: str | None = None
    ) -> int:
        """workflow Notion ページの存在に影響する pending イベントだけを数える。

        以下は workflow page と独立した同期なので除外する。これがないと、
        retry_pending() で各イベントを再送する時、自己 entry を含む
        `_count_pending_for` が常に > 0 を返してしまい、永久に自己 deferral
        ループに陥り max_attempts 到達まで errors テーブルへ移動できない
        （PR #37 Copilot 5 回目指摘 / PR #45 Copilot 2 回目で gate イベント
        も同じ理由で除外対象に追加）:

        - `review_issue_raised`: Phase 6/7 の指摘同期（#36）
        - `work_item_*` (upsert / status_change / claim / lease_release):
          Work Items DB 関連（#38, #42）
        - `workflow_gate_*` (upsert / status_change): Workflow Gates DB
          関連（#44）
        - `service_status_checked`: 廃止済の旧 Service Status sync

        exclude_key を渡せば、retry_pending() 経由で「これから削除される」
        自己 entry をさらに除外できる。
        """
        if self._store is None:
            return 0
        # 各イベントは workflow page sync とは独立した同期なので、同一
        # workflow_id の workflow_started が pending の際の deferred ループを
        # 避けるため除外する。
        excluded_types = (
            "review_issue_raised",
            "work_item_upsert",
            "work_item_status_change",
            "work_item_claim",
            "work_item_lease_release",
            "workflow_gate_upsert",
            "workflow_gate_status_change",
            "project_memory_upsert",
            "project_memory_status_change",
            "service_status_checked",
        )
        try:
            with self._store._connect() as conn:  # type: ignore[attr-defined]
                placeholders = ",".join(["?"] * len(excluded_types))
                sql = (
                    "SELECT COUNT(*) FROM notion_sync_outbox "
                    f"WHERE workflow_id = ? AND event_type NOT IN ({placeholders})"
                )
                params: list[Any] = [workflow_id, *excluded_types]
                if exclude_key:
                    sql += " AND idempotency_key != ?"
                    params.append(exclude_key)
                row = conn.execute(sql, tuple(params)).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def _count_errors_for(self, workflow_id: str) -> int:
        """SQLite errors テーブル上の当該 workflow_id の件数。"""
        if self._store is None:
            return 0
        try:
            with self._store._connect() as conn:  # type: ignore[attr-defined]
                row = conn.execute(
                    "SELECT COUNT(*) FROM notion_sync_errors WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def _handle_pr_created(self, payload: dict[str, Any]) -> None:
        """PR 作成イベントを Pull Requests DB に反映する。

        Pull Requests DB の database_id が未設定の場合はスキップ。
        Workflows DB 側の GitLab MR URL も併せて更新する。
        Workflows DB 上のページ ID を取得し、Pull Requests DB レコードに
        Workflow relation として紐付ける。
        """
        prs = payload.get("pull_requests") or []
        pr_db_id = os.environ.get(self._config.pull_requests_db_id_env, "").strip()

        # Workflows DB を先に更新（MR URL を最後の PR で代表）
        # apply_event の戻り値（Notion page object）から page_id を取り出して relation に使う
        workflow_page_id: str | None = None
        if prs:
            last_pr = prs[-1]
            workflow_payload: dict[str, Any] = {
                "workflow_id": payload.get("workflow_id"),
                "gitlab_mr_url": last_pr.get("url"),
            }
            # _send_to_notion 経由で enrich された last_sync / sync_errors を引き継ぐ
            # （workflow_payload を新規構築している箇所で消えないように）
            if "last_sync" in payload:
                workflow_payload["last_sync"] = payload["last_sync"]
            if "sync_errors" in payload:
                workflow_payload["sync_errors"] = payload["sync_errors"]
            page_obj = self._get_workflows_client().apply_event(
                "pr_created", workflow_payload
            )
            if isinstance(page_obj, dict):
                page_id = page_obj.get("id")
                if isinstance(page_id, str) and page_id:
                    workflow_page_id = page_id

        # Pull Requests DB が設定されていなければここで終わり
        if not pr_db_id:
            return

        pr_client = self._get_pull_requests_client(pr_db_id)
        for pr in prs:
            pr_number = pr.get("number")
            if pr_number is None:
                continue
            repository = pr.get("repository") or pr.get("repo")
            existing = pr_client.find_by_pr_number(pr_number, repository=repository)
            if existing is not None:
                continue  # 重複作成しない
            pr_client.create_record(
                pr_number=pr_number,
                url=pr.get("url", ""),
                repository=repository,
                workflow_page_id=workflow_page_id,
                status=pr.get("status", "Draft"),
                created_at=pr.get("created_at"),
            )

    def _handle_review_issue_raised(
        self,
        payload: dict[str, Any],
        *,
        exclude_idempotency_key: str | None = None,
    ) -> None:
        """Review Issue 発火イベントを Review Issues DB に upsert する。

        Review Issues DB の database_id が未設定の場合はスキップ。Workflow relation
        は workflows_db への lookup で workflow_page_id を取得して張る。
        以下の挙動は意図的:
        - `_find_page_id` の API エラー（rate limit / network / invalid DB ID 等）
          は握り潰さず `dispatch()` まで伝播させ、outbox 経由でリトライさせる
          （Copilot 3 回目指摘）。
        - 対応 workflow ページが Notion 上に存在せず、かつ outbox に同 workflow_id
          の workflow page sync イベントが pending（workflow_started 等）の場合は、
          race condition として deferして NotionAPIError(503) で outbox に積み直す
          （Copilot 4 回目指摘）。
        - pending workflow page sync が無く workflow ページも存在しない場合に限り、
          relation 無しで Review Issue を作成（best effort, genuine miss）。

        Args:
            payload: review_issue_raised の payload（workflow_id / source / message /
                severity / status / rule / file / repository / operator /
                dedupe_key / title）
            exclude_idempotency_key: retry_pending() からの再送呼び出し時、自己
                entry を race 検出の pending 集計から除外するためのキー
                （Copilot 5 回目指摘）。
        """
        review_db_id = os.environ.get(self._config.review_issues_db_id_env, "").strip()
        if not review_db_id:
            logger.debug(
                "Review Issues DB ID が未設定のため Review Issue 同期をスキップ"
            )
            return

        source = payload.get("source")
        message = payload.get("message")
        if not source or not message:
            logger.warning(
                "review_issue_raised に source / message が無いためスキップ"
            )
            return

        workflow_id = payload.get("workflow_id")
        workflow_page_id: str | None = None
        if workflow_id:
            workflow_page_id = self._lookup_workflow_page_id(workflow_id)
            if workflow_page_id is None:
                # workflow page が見つからない場合、workflow page sync イベント
                # （workflow_started / phase_changed / pr_created /
                # phase_artifact_linked / terminal_status_changed）が outbox に
                # 残っているかで動作を分ける。`review_issue_raised` / 廃止済の
                # `service_status_checked` は workflow page と無関係なので集計
                # から除外する（Copilot 5 回目指摘の循環参照対応）。
                # retry_pending() 経由なら自己 entry もキーで除外。
                pending_count = self._count_pending_workflow_page_events_for(
                    workflow_id, exclude_key=exclude_idempotency_key
                )
                if pending_count > 0:
                    raise NotionAPIError(
                        503,
                        f"workflow page not yet synced for workflow_id="
                        f"{workflow_id}; deferring review_issue_raised dispatch "
                        f"({pending_count} pending workflow page events)",
                        code="workflow_page_pending",
                    )

        client = self._get_review_issues_client(review_db_id)
        client.upsert_record(
            source=str(source),
            message=str(message),
            severity=str(payload.get("severity") or "medium"),
            status=str(payload.get("status") or "open"),
            rule=payload.get("rule"),
            file=payload.get("file"),
            repository=payload.get("repository"),
            workflow_id=workflow_id,
            workflow_page_id=workflow_page_id,
            operator=payload.get("operator"),
            dedupe_key=payload.get("dedupe_key"),
            title=payload.get("title"),
        )

    def _handle_work_item_upsert(
        self,
        payload: dict[str, Any],
        *,
        exclude_idempotency_key: str | None = None,
    ) -> None:
        """Work Item upsert イベントを Work Items DB に反映する（Issue #38）。

        Review Issues と同じ workflow_page race 対策を持つ:
        - 対応 workflow ページが Notion 上に存在せず、かつ outbox に同 workflow_id
          の workflow page sync イベントが pending（workflow_started 等）の場合は、
          race condition として deferして NotionAPIError(503) で outbox に積み直す。
        - pending workflow page sync が無く workflow ページも存在しない場合に限り、
          Workflow relation 無しで Work Item を upsert（best effort, genuine miss）。

        Args:
            payload: work_item_upsert の payload（workflow_id / title / phase /
                status / operator / description / dedupe_key /
                dependency_page_ids / blocking_review_issue_page_ids）
            exclude_idempotency_key: retry_pending() 経由の再送時、自己 entry を
                race 検出の pending 集計から除外するキー
        """
        work_items_db_id = os.environ.get(
            self._config.work_items_db_id_env, ""
        ).strip()
        if not work_items_db_id:
            logger.debug(
                "Work Items DB ID が未設定のため Work Item 同期をスキップ"
            )
            return

        title = payload.get("title")
        if not title:
            logger.warning(
                "work_item_upsert に title が無いためスキップ"
            )
            return

        workflow_id = payload.get("workflow_id")
        workflow_page_id: str | None = None
        if workflow_id:
            workflow_page_id = self._lookup_workflow_page_id(workflow_id)
            if workflow_page_id is None:
                pending_count = self._count_pending_workflow_page_events_for(
                    workflow_id, exclude_key=exclude_idempotency_key
                )
                if pending_count > 0:
                    raise NotionAPIError(
                        503,
                        f"workflow page not yet synced for workflow_id="
                        f"{workflow_id}; deferring work_item_upsert dispatch "
                        f"({pending_count} pending workflow page events)",
                        code="workflow_page_pending",
                    )

        client = self._get_work_items_client(work_items_db_id)
        client.upsert_work_item(
            title=str(title),
            phase=payload.get("phase"),
            status=str(payload.get("status") or "pending"),
            workflow_id=workflow_id,
            workflow_page_id=workflow_page_id,
            operator=payload.get("operator"),
            description=payload.get("description"),
            dependency_page_ids=payload.get("dependency_page_ids") or [],
            blocking_review_issue_page_ids=payload.get(
                "blocking_review_issue_page_ids"
            )
            or [],
            dedupe_key=payload.get("dedupe_key"),
        )

    def _handle_work_item_status_change(self, payload: dict[str, Any]) -> None:
        """Work Item の Status のみを明示的に上書きする（Issue #38）。

        upsert_work_item は再 dispatch で Status を巻き戻さないために温存
        するため、Phase 5 implement の `in_progress` → `done` のような遷移には
        本ハンドラを使う。dedupe_key（または workflow_id + phase + title）で
        Work Item を同定し、見つからなければ warning で skip する（Phase 5 完了
        時点で必ず Phase 4 enqueue 済 Work Item が Notion 側に存在する前提）。

        Args:
            payload: workflow_id / title / phase / status / dedupe_key
        """
        from .work_items_db import build_dedupe_key

        work_items_db_id = os.environ.get(
            self._config.work_items_db_id_env, ""
        ).strip()
        if not work_items_db_id:
            logger.debug(
                "Work Items DB ID 未設定のため status change をスキップ"
            )
            return

        status = payload.get("status")
        if not status:
            logger.warning(
                "work_item_status_change に status が無いためスキップ"
            )
            return

        dedupe_key = payload.get("dedupe_key")
        if not dedupe_key:
            # dedupe_key 自動生成は title が必須（空文字を許すと
            # build_dedupe_key の入力が `workflow_id|phase|""` になり、
            # 同 workflow/phase の全 Work Item が同一 dedupe_key に潰れて
            # 別 Work Item を誤って更新するリスクがあるため）。PR #41
            # Copilot 4 回目指摘で title 必須化。
            title = payload.get("title")
            if not title:
                logger.warning(
                    "work_item_status_change で dedupe_key も title も無いため "
                    "Work Item を同定できずスキップ: workflow_id=%s, phase=%s",
                    payload.get("workflow_id"), payload.get("phase"),
                )
                return
            dedupe_key = build_dedupe_key(
                workflow_id=payload.get("workflow_id"),
                phase=payload.get("phase"),
                title=str(title),
            )

        client = self._get_work_items_client(work_items_db_id)
        page_id = client.find_by_dedupe_key(dedupe_key)
        if page_id is None:
            # Work Item が見つからない場合、対応する `work_item_upsert` が
            # outbox に残っているかを確認する。残っていれば、Phase 4 の upsert
            # が Notion 障害等で pending のまま Phase 5 の status_change が
            # 先に走った race condition なので、deferして outbox 再送に任せる
            # （`workflow_started` ↔ `review_issue_raised` と同じ deferred 戦略。
            # PR #41 Copilot 7 回目指摘で silent drop を防止）。
            workflow_id = payload.get("workflow_id")
            # outbox に **同じ dedupe_key の** work_item_upsert が pending かを
            # inline で確認する（PR #41 Copilot 8 回目指摘で範囲を絞り込み）。
            # 旧版は `workflow_id × event_type` で集計していたため、対象 Work
            # Item と無関係な別 item の upsert が残っているだけで status_change
            # が永久 defer されるリスクがあった。idempotency_key は
            # `{wid}:work_item_upsert:{dedupe_key}` 形式なので exact match で
            # 同定する（drain 層 `_prepare_work_item_dispatch` の生成則と一致）。
            # 失敗時は `0` 扱いで defer をスキップし、genuine miss 側に倒す。
            pending_upsert = 0
            if workflow_id and self._store is not None:
                expected_key = f"{workflow_id}:work_item_upsert:{dedupe_key}"
                try:
                    with self._store._connect() as conn:  # type: ignore[attr-defined]
                        row = conn.execute(
                            "SELECT COUNT(*) FROM notion_sync_outbox "
                            "WHERE idempotency_key = ?",
                            (expected_key,),
                        ).fetchone()
                        pending_upsert = int(row[0]) if row else 0
                except Exception:
                    pending_upsert = 0
            if pending_upsert > 0:
                raise NotionAPIError(
                    503,
                    f"work_item_upsert not yet synced for workflow_id={workflow_id}; "
                    f"deferring work_item_status_change dispatch "
                    f"({pending_upsert} pending work_item_upsert events)",
                    code="work_item_upsert_pending",
                )
            # genuine miss（upsert も無く Notion 側にも page が存在しない）。
            # 後続に影響しないので warning で skip。
            logger.warning(
                "Work Item の status 遷移先 page が見つからない: "
                f"workflow_id={workflow_id}, "
                f"title={payload.get('title')!r}, dedupe_key={dedupe_key[:8]}..."
            )
            return
        client.update_status(page_id, str(status))

    def _handle_work_item_claim(self, payload: dict[str, Any]) -> None:
        """Work Item の claim イベント（Workgraph Phase 3 / Issue #42）。

        Phase 5 implement 開始時に Agent / 人間が Work Item を取得した事実を
        Notion に書き込む。dedupe_key で Work Item を同定し、見つからない
        場合は status_change と同じ defer / skip 戦略を適用する。

        Args:
            payload:
                workflow_id / title / phase: Work Item 同定用（dedupe_key 省略時に使う）
                dedupe_key: 省略時は build_dedupe_key で再生成
                claimed_by: Claim 主体（"claude_code" / "alice@example.com" 等、必須）
                claim_type: "agent" / "human"（既定 agent）
                lease_duration_seconds: lease 期限（既定 3600）
        """
        page_id, client = self._resolve_work_item_for_lease_event(
            payload, event_label="work_item_claim"
        )
        if page_id is None:
            return
        claimed_by = payload.get("claimed_by")
        if not claimed_by:
            logger.warning("work_item_claim に claimed_by が無いためスキップ")
            return
        from .work_items_db import (
            CLAIM_TYPE_AGENT,
            CLAIM_TYPE_HUMAN,
            DEFAULT_LEASE_DURATION_SECONDS,
        )

        # **入力検証（poison message 防止）**:
        # PR #43 Copilot 2 回目指摘で、不正な payload（非数値 lease_duration
        # や enum 外の claim_type 等）が WorkItemsDBClient.claim_work_item の
        # ValueError を引き起こすと、dispatch() の catch-all で outbox に積み
        # 直されて永続リトライループになるリスクがあった。dispatcher 層で
        # warning + skip して outbox に入れないよう先回りで検証する。

        # claim_type: agent / human のみ許可
        raw_claim_type = payload.get("claim_type")
        if raw_claim_type is None:
            claim_type = CLAIM_TYPE_AGENT
        else:
            claim_type_str = str(raw_claim_type)
            if claim_type_str not in (CLAIM_TYPE_AGENT, CLAIM_TYPE_HUMAN):
                logger.warning(
                    "work_item_claim の claim_type が不正なのでスキップ: %r",
                    raw_claim_type,
                )
                return
            claim_type = claim_type_str

        # lease_duration_seconds: None は DEFAULT。明示値は int 変換を
        # 試み、失敗 / 0 以下なら warning + skip（client 側 ValueError を
        # 待たない）。
        raw_duration = payload.get("lease_duration_seconds")
        if raw_duration is None:
            lease_duration_seconds = DEFAULT_LEASE_DURATION_SECONDS
        else:
            try:
                lease_duration_seconds = int(raw_duration)
            except (TypeError, ValueError):
                logger.warning(
                    "work_item_claim の lease_duration_seconds が非数値 "
                    "なのでスキップ: %r",
                    raw_duration,
                )
                return
            if lease_duration_seconds <= 0:
                logger.warning(
                    "work_item_claim の lease_duration_seconds が 0 以下 "
                    "なのでスキップ: %d",
                    lease_duration_seconds,
                )
                return

        client.claim_work_item(
            page_id,
            claimed_by=str(claimed_by),
            claim_type=claim_type,
            lease_duration_seconds=lease_duration_seconds,
        )

    def _handle_work_item_lease_release(self, payload: dict[str, Any]) -> None:
        """Work Item の lease release イベント（Workgraph Phase 3 / Issue #42）。

        Phase 5 implement の正常完了時に呼ばれ、Lease Status を `released` に
        遷移させる。Claimed By / Lease Token は監査用に温存する。
        """
        page_id, client = self._resolve_work_item_for_lease_event(
            payload, event_label="work_item_lease_release"
        )
        if page_id is None:
            return
        client.release_lease(page_id)

    def _handle_workflow_gate_upsert(self, payload: dict[str, Any]) -> None:
        """Workflow Gate upsert イベント（Workgraph Phase 4 / Issue #44）。

        必須 payload: name / gate_type。任意: workflow_id / required_by_phase /
        status / approver / decision_reason / due_at / work_item_dedupe_key /
        dedupe_key / workflow_page_id / pull_request_page_id /
        work_item_page_id / review_issue_page_id.

        入力検証（poison message 防止）:
        - name / gate_type 必須、未指定なら warning + skip
        - gate_type / status が enum 外なら warning + skip
        - dedupe_key が str 以外なら warning + skip（list/dict 等で Notion
          query 構築 TypeError を防ぐ、PR #43 で学んだパターン）
        """
        from .workflow_gates_db import (
            DEFAULT_GATE_STATUS,
            is_valid_gate_status,
            is_valid_gate_type,
        )

        workflow_gates_db_id = os.environ.get(
            self._config.workflow_gates_db_id_env, ""
        ).strip()
        if not workflow_gates_db_id:
            logger.debug(
                "Workflow Gates DB ID 未設定のため workflow_gate_upsert をスキップ"
            )
            return

        name = payload.get("name")
        gate_type = payload.get("gate_type")
        if not name or not gate_type:
            logger.warning(
                "workflow_gate_upsert に name または gate_type が無いためスキップ"
            )
            return
        if not is_valid_gate_type(gate_type):
            logger.warning(
                "workflow_gate_upsert の gate_type が enum 外なのでスキップ: %r",
                gate_type,
            )
            return
        status = payload.get("status") or DEFAULT_GATE_STATUS
        if not is_valid_gate_status(status):
            logger.warning(
                "workflow_gate_upsert の status が enum 外なのでスキップ: %r",
                status,
            )
            return

        dedupe_key = payload.get("dedupe_key")
        if dedupe_key is not None and not isinstance(dedupe_key, str):
            logger.warning(
                "workflow_gate_upsert の dedupe_key が str でないためスキップ: %r",
                dedupe_key,
            )
            return

        # required_by_phase の型検証
        raw_phase = payload.get("required_by_phase")
        if raw_phase is None:
            required_by_phase = None
        else:
            try:
                required_by_phase = int(raw_phase)
            except (TypeError, ValueError):
                logger.warning(
                    "workflow_gate_upsert の required_by_phase が int 変換 "
                    "不能なのでスキップ: %r",
                    raw_phase,
                )
                return

        # **workflow_id 必須化 + 型検証**（PR #45 Copilot 3/4 回目指摘）:
        # dedupe_key が未指定で workflow_id も無い場合、build_dedupe_key が
        # workflow_id を空文字扱いにし、別 workflow 間で dedupe_key が衝突
        # するリスクがあるため、dedupe_key 自動生成パスでは workflow_id を
        # 必須にする。さらに workflow_id / work_item_dedupe_key が str 以外
        # （list/dict 等）だと build_dedupe_key の join で TypeError 経由
        # poison message 化するため、型も検証する。明示 dedupe_key 指定パスは
        # 呼び出し側が衝突回避責任を持つ前提で skip しない。
        workflow_id = payload.get("workflow_id")
        if not dedupe_key:
            if not workflow_id:
                logger.warning(
                    "workflow_gate_upsert で dedupe_key も workflow_id も無い "
                    "ため別 workflow 間 dedupe_key 衝突のリスクがありスキップ: "
                    "gate_type=%s, required_by_phase=%s",
                    gate_type, required_by_phase,
                )
                return
            if not isinstance(workflow_id, str):
                logger.warning(
                    "workflow_gate_upsert で workflow_id が str でないため "
                    "スキップ: %r",
                    workflow_id,
                )
                return
            raw_wi_dkey = payload.get("work_item_dedupe_key")
            if raw_wi_dkey is not None and not isinstance(raw_wi_dkey, str):
                logger.warning(
                    "workflow_gate_upsert で work_item_dedupe_key が str で "
                    "ないためスキップ: %r",
                    raw_wi_dkey,
                )
                return

        client = self._get_workflow_gates_client(workflow_gates_db_id)
        client.upsert_gate(
            name=str(name),
            gate_type=str(gate_type),
            status=str(status),
            required_by_phase=required_by_phase,
            workflow_id=workflow_id,
            workflow_page_id=payload.get("workflow_page_id"),
            pull_request_page_id=payload.get("pull_request_page_id"),
            work_item_page_id=payload.get("work_item_page_id"),
            review_issue_page_id=payload.get("review_issue_page_id"),
            approver=payload.get("approver"),
            decision_reason=payload.get("decision_reason"),
            due_at=payload.get("due_at"),
            work_item_dedupe_key=payload.get("work_item_dedupe_key"),
            dedupe_key=dedupe_key,
        )

    def _handle_workflow_gate_status_change(
        self, payload: dict[str, Any]
    ) -> None:
        """Workflow Gate Status 遷移イベント（Workgraph Phase 4 / Issue #44）。

        Phase 6 verify / Phase 7 review / 外部承認 hook から「gate を open に
        した」「blocked にした」等の状態遷移を Notion に反映する。dedupe_key
        または (workflow_id, gate_type, required_by_phase, work_item_dedupe_key)
        で gate を同定し、見つからなければ warning + skip。
        """
        from .workflow_gates_db import build_dedupe_key, is_valid_gate_status

        workflow_gates_db_id = os.environ.get(
            self._config.workflow_gates_db_id_env, ""
        ).strip()
        if not workflow_gates_db_id:
            logger.debug(
                "Workflow Gates DB ID 未設定のため "
                "workflow_gate_status_change をスキップ"
            )
            return

        status = payload.get("status")
        if not status:
            logger.warning(
                "workflow_gate_status_change に status が無いためスキップ"
            )
            return
        if not is_valid_gate_status(status):
            logger.warning(
                "workflow_gate_status_change の status が enum 外なのでスキップ: %r",
                status,
            )
            return

        dedupe_key = payload.get("dedupe_key")
        if dedupe_key is not None and not isinstance(dedupe_key, str):
            logger.warning(
                "workflow_gate_status_change の dedupe_key が str でない: %r",
                dedupe_key,
            )
            return

        if not dedupe_key:
            gate_type = payload.get("gate_type")
            if not gate_type:
                logger.warning(
                    "workflow_gate_status_change で dedupe_key も gate_type も "
                    "無いため Gate を同定不能スキップ"
                )
                return
            # gate_type の enum 検証（PR #45 Copilot 2 回目指摘）。typo / poison
            # payload で誤 dedupe_key を build_dedupe_key が生成すると
            # find_by_dedupe_key で常に miss → 状態遷移が silent に失敗する
            # ため、upsert と同じく enum 外なら warning + skip。
            from .workflow_gates_db import is_valid_gate_type
            if not is_valid_gate_type(gate_type):
                logger.warning(
                    "workflow_gate_status_change の gate_type が enum 外 "
                    "なのでスキップ: %r",
                    gate_type,
                )
                return
            # **workflow_id 必須化 + 型検証**（PR #45 Copilot 3/4 回目指摘、
            # upsert と同じ理由）: dedupe_key 自動生成パスで workflow_id が
            # 無いと別 workflow 間衝突。さらに workflow_id /
            # work_item_dedupe_key が str 以外だと build_dedupe_key の join
            # で TypeError → poison message 化するため、型も検証する。
            workflow_id_for_key = payload.get("workflow_id")
            if not workflow_id_for_key:
                logger.warning(
                    "workflow_gate_status_change で dedupe_key も workflow_id "
                    "も無いため別 workflow 間衝突のリスクがありスキップ"
                )
                return
            if not isinstance(workflow_id_for_key, str):
                logger.warning(
                    "workflow_gate_status_change で workflow_id が str で "
                    "ないためスキップ: %r",
                    workflow_id_for_key,
                )
                return
            raw_wi_dkey = payload.get("work_item_dedupe_key")
            if raw_wi_dkey is not None and not isinstance(raw_wi_dkey, str):
                logger.warning(
                    "workflow_gate_status_change で work_item_dedupe_key が "
                    "str でないためスキップ: %r",
                    raw_wi_dkey,
                )
                return
            # required_by_phase の型検証（upsert と同じ）
            raw_phase = payload.get("required_by_phase")
            if raw_phase is None:
                phase_for_key = None
            else:
                try:
                    phase_for_key = int(raw_phase)
                except (TypeError, ValueError):
                    logger.warning(
                        "workflow_gate_status_change で required_by_phase の "
                        "int 変換不能スキップ: %r",
                        raw_phase,
                    )
                    return
            dedupe_key = build_dedupe_key(
                workflow_id=workflow_id_for_key,
                gate_type=str(gate_type),
                required_by_phase=phase_for_key,
                work_item_dedupe_key=payload.get("work_item_dedupe_key"),
            )

        client = self._get_workflow_gates_client(workflow_gates_db_id)
        page_id = client.find_by_dedupe_key(dedupe_key)
        if page_id is None:
            logger.warning(
                "workflow_gate_status_change の対象 page が見つからない: "
                "workflow_id=%s, gate_type=%s, dedupe_key=%s...",
                payload.get("workflow_id"),
                payload.get("gate_type"),
                dedupe_key[:8],
            )
            return
        client.update_status(
            page_id,
            str(status),
            approver=payload.get("approver"),
            decision_reason=payload.get("decision_reason"),
        )

    def _handle_project_memory_upsert(self, payload: dict[str, Any]) -> None:
        """Project Memory upsert イベント（Workgraph Phase 5 / Issue #46）。

        必須 payload: name / memory_type / content。任意: workflow_id /
        status / profile / summary / applies_to / workflow_page_id /
        pull_request_page_id / approved_by / approved_at / expires_at /
        dedupe_key.

        入力検証（poison message 防止）:
        - name / memory_type / content 必須、未指定なら warning + skip
        - memory_type / status が enum 外なら warning + skip
        - dedupe_key が str 以外なら warning + skip
        - dedupe_key 未指定で workflow_id が空 / 非 str なら衝突防止で skip
        """
        from .project_memory_db import (
            DEFAULT_MEMORY_STATUS,
            is_valid_memory_status,
            is_valid_memory_type,
        )

        project_memory_db_id = os.environ.get(
            self._config.project_memory_db_id_env, ""
        ).strip()
        if not project_memory_db_id:
            logger.debug(
                "Project Memory DB ID 未設定のため project_memory_upsert をスキップ"
            )
            return

        name = payload.get("name")
        memory_type = payload.get("memory_type")
        content = payload.get("content")
        if not name or not memory_type or not content:
            logger.warning(
                "project_memory_upsert に name / memory_type / content の "
                "いずれかが無いためスキップ"
            )
            return
        if not is_valid_memory_type(memory_type):
            logger.warning(
                "project_memory_upsert の memory_type が enum 外なのでスキップ: %r",
                memory_type,
            )
            return
        status = payload.get("status") or DEFAULT_MEMORY_STATUS
        if not is_valid_memory_status(status):
            logger.warning(
                "project_memory_upsert の status が enum 外なのでスキップ: %r",
                status,
            )
            return

        dedupe_key = payload.get("dedupe_key")
        if dedupe_key is not None and not isinstance(dedupe_key, str):
            logger.warning(
                "project_memory_upsert の dedupe_key が str でないためスキップ: %r",
                dedupe_key,
            )
            return

        # dedupe_key 自動生成パスで workflow_id 必須化 + 型検証（PR #45 で
        # 確立した workflow_gate と同じ guard 戦略）。明示 dedupe_key 指定
        # パスは呼び出し側責任で衝突回避。
        workflow_id = payload.get("workflow_id")
        if not dedupe_key:
            if not workflow_id:
                logger.warning(
                    "project_memory_upsert で dedupe_key も workflow_id も無い "
                    "ため別 workflow 間 dedupe_key 衝突のリスクがありスキップ: "
                    "memory_type=%s, name=%s",
                    memory_type, name,
                )
                return
            if not isinstance(workflow_id, str):
                logger.warning(
                    "project_memory_upsert で workflow_id が str でないため "
                    "スキップ: %r",
                    workflow_id,
                )
                return

        client = self._get_project_memory_client(project_memory_db_id)
        client.upsert_memory(
            name=str(name),
            memory_type=str(memory_type),
            content=str(content),
            summary=payload.get("summary"),
            status=str(status),
            profile=payload.get("profile"),
            applies_to=payload.get("applies_to") or [],
            workflow_id=workflow_id,
            workflow_page_id=payload.get("workflow_page_id"),
            pull_request_page_id=payload.get("pull_request_page_id"),
            approved_by=payload.get("approved_by"),
            approved_at=payload.get("approved_at"),
            expires_at=payload.get("expires_at"),
            dedupe_key=dedupe_key,
        )

    def _handle_project_memory_status_change(
        self, payload: dict[str, Any]
    ) -> None:
        """Project Memory Status 遷移イベント（Workgraph Phase 5 / Issue #46）。

        Agent / 人間が memory を承認 / 廃止 / 却下した際に dispatch される。
        dedupe_key または (workflow_id, memory_type, name) で memory を同定し、
        見つからなければ warning + skip。
        """
        from .project_memory_db import (
            build_dedupe_key,
            is_valid_memory_status,
            is_valid_memory_type,
        )

        project_memory_db_id = os.environ.get(
            self._config.project_memory_db_id_env, ""
        ).strip()
        if not project_memory_db_id:
            logger.debug(
                "Project Memory DB ID 未設定のため "
                "project_memory_status_change をスキップ"
            )
            return

        status = payload.get("status")
        if not status:
            logger.warning(
                "project_memory_status_change に status が無いためスキップ"
            )
            return
        if not is_valid_memory_status(status):
            logger.warning(
                "project_memory_status_change の status が enum 外なので "
                "スキップ: %r",
                status,
            )
            return

        dedupe_key = payload.get("dedupe_key")
        if dedupe_key is not None and not isinstance(dedupe_key, str):
            logger.warning(
                "project_memory_status_change の dedupe_key が str でない: %r",
                dedupe_key,
            )
            return

        if not dedupe_key:
            memory_type = payload.get("memory_type")
            name = payload.get("name")
            if not memory_type or not name:
                logger.warning(
                    "project_memory_status_change で dedupe_key も "
                    "memory_type/name も無いため同定不能スキップ"
                )
                return
            if not is_valid_memory_type(memory_type):
                logger.warning(
                    "project_memory_status_change の memory_type が enum 外 "
                    "なのでスキップ: %r",
                    memory_type,
                )
                return
            workflow_id_for_key = payload.get("workflow_id")
            if not workflow_id_for_key:
                logger.warning(
                    "project_memory_status_change で dedupe_key も workflow_id "
                    "も無いため別 workflow 間衝突のリスクがありスキップ"
                )
                return
            if not isinstance(workflow_id_for_key, str):
                logger.warning(
                    "project_memory_status_change で workflow_id が str で "
                    "ないためスキップ: %r",
                    workflow_id_for_key,
                )
                return
            dedupe_key = build_dedupe_key(
                workflow_id=workflow_id_for_key,
                memory_type=str(memory_type),
                name=str(name),
            )

        client = self._get_project_memory_client(project_memory_db_id)
        page_id = client.find_by_dedupe_key(dedupe_key)
        if page_id is None:
            logger.warning(
                "project_memory_status_change の対象 page が見つからない: "
                "workflow_id=%s, memory_type=%s, dedupe_key=%s...",
                payload.get("workflow_id"),
                payload.get("memory_type"),
                dedupe_key[:8],
            )
            return
        client.update_status(
            page_id,
            str(status),
            approved_by=payload.get("approved_by"),
            approved_at=payload.get("approved_at"),
        )

    def _resolve_work_item_for_lease_event(
        self, payload: dict[str, Any], *, event_label: str
    ) -> tuple[str | None, Any]:
        """claim / lease_release で共通する Work Item 同定ロジック。

        - Work Items DB ID 未設定 → no-op で (None, None) を返す
        - dedupe_key 自動生成（title 必須）
        - find_by_dedupe_key が miss → status_change と同じ defer / skip 戦略
          （同一 dedupe_key の work_item_upsert が outbox に pending なら 503
          で defer、それ以外は warning + skip）

        Returns:
            (page_id, client) のタプル。page_id が None なら呼び出し側は早期 return。
        """
        from .work_items_db import build_dedupe_key

        work_items_db_id = os.environ.get(
            self._config.work_items_db_id_env, ""
        ).strip()
        if not work_items_db_id:
            logger.debug(
                "Work Items DB ID 未設定のため %s をスキップ", event_label
            )
            return None, None

        dedupe_key = payload.get("dedupe_key")
        # **dedupe_key の型検証**（PR #43 Copilot 4 回目指摘）: payload 経由で
        # str 以外（list / dict / int 等）が渡ると後段の `find_by_dedupe_key`
        # の query filter 構築で TypeError → dispatch() catch-all → outbox
        # 再投入の poison message 化リスクがあるため、dispatcher 層で先に
        # 型を検証する。明示指定があって str でなければ skip（自動生成パスに
        # 落とさない: 「dedupe_key を明示したが型が間違っている」のは呼び出し
        # 側のバグなのでサイレント補正せず明示的に skip + warning する）。
        if dedupe_key is not None and not isinstance(dedupe_key, str):
            logger.warning(
                "%s で dedupe_key が str でないためスキップ: %r",
                event_label, dedupe_key,
            )
            return None, None
        if not dedupe_key:
            title = payload.get("title")
            if not title:
                logger.warning(
                    "%s で dedupe_key も title も無いため同定不能スキップ: "
                    "workflow_id=%s, phase=%s",
                    event_label,
                    payload.get("workflow_id"),
                    payload.get("phase"),
                )
                return None, None
            # **workflow_id / phase の型検証**（PR #43 Copilot 3 回目指摘）:
            # payload 由来の不正型（int の workflow_id / 非数値 phase 等）が
            # build_dedupe_key の join で TypeError を投げると、dispatch() の
            # catch-all 経由で outbox に poison message として残るため、
            # dispatcher 層で先回り正規化 / 検証する。
            raw_wid = payload.get("workflow_id")
            workflow_id_for_key: str | None
            if raw_wid is None:
                workflow_id_for_key = None
            elif isinstance(raw_wid, (str, int)):
                # str はそのまま、int は文字列化（HOKUSAI ID は str 想定だが
                # numeric な test fixture も許容する寛容な正規化）
                workflow_id_for_key = str(raw_wid) or None
            else:
                logger.warning(
                    "%s で workflow_id が str/int でないためスキップ: %r",
                    event_label, raw_wid,
                )
                return None, None
            raw_phase = payload.get("phase")
            phase_for_key: int | None
            if raw_phase is None:
                phase_for_key = None
            else:
                try:
                    phase_for_key = int(raw_phase)
                except (TypeError, ValueError):
                    logger.warning(
                        "%s で phase を int に変換できないためスキップ: %r",
                        event_label, raw_phase,
                    )
                    return None, None
            dedupe_key = build_dedupe_key(
                workflow_id=workflow_id_for_key,
                phase=phase_for_key,
                title=str(title),
            )

        client = self._get_work_items_client(work_items_db_id)
        page_id = client.find_by_dedupe_key(dedupe_key)
        if page_id is None:
            workflow_id = payload.get("workflow_id")
            pending_upsert = 0
            if workflow_id and self._store is not None:
                expected_key = f"{workflow_id}:work_item_upsert:{dedupe_key}"
                try:
                    with self._store._connect() as conn:  # type: ignore[attr-defined]
                        row = conn.execute(
                            "SELECT COUNT(*) FROM notion_sync_outbox "
                            "WHERE idempotency_key = ?",
                            (expected_key,),
                        ).fetchone()
                        pending_upsert = int(row[0]) if row else 0
                except Exception:
                    pending_upsert = 0
            if pending_upsert > 0:
                raise NotionAPIError(
                    503,
                    f"work_item_upsert not yet synced for workflow_id="
                    f"{workflow_id}; deferring {event_label} dispatch "
                    f"({pending_upsert} pending work_item_upsert events)",
                    code="work_item_upsert_pending",
                )
            logger.warning(
                "%s の対象 page が見つからない: workflow_id=%s, "
                "title=%r, dedupe_key=%s...",
                event_label,
                workflow_id,
                payload.get("title"),
                dedupe_key[:8],
            )
            return None, None
        return page_id, client

    def _enqueue_failure(
        self,
        idempotency_key: str,
        workflow_id: str,
        event_type: str,
        payload: dict[str, Any],
        error: Exception,
    ) -> None:
        message = self._safe_error_message(error)
        try:
            is_new = self._store.enqueue_notion_sync(
                idempotency_key=idempotency_key,
                workflow_id=workflow_id,
                event_type=event_type,
                payload=payload,
            )
            if is_new:
                logger.warning(
                    f"Notion 同期失敗 → outbox に追加: event={event_type}, "
                    f"workflow_id={workflow_id}, error={message}"
                )
                # 試行回数を 1 にする
                next_at = (datetime.now() + timedelta(
                    seconds=self._config.retry.backoff_seconds
                )).isoformat()
                self._store.mark_notion_sync_failed(idempotency_key, message, next_at)
            else:
                logger.debug(
                    f"Notion 同期失敗（既に outbox にあり）: event={event_type}, "
                    f"workflow_id={workflow_id}"
                )
        except Exception as enqueue_error:
            # outbox 書き込みすら失敗した場合は、ワークフロー本体は止めずログのみ
            logger.error(
                f"Notion 同期 outbox 書き込み失敗: {type(enqueue_error).__name__}"
            )

    @staticmethod
    def _safe_error_message(error: Exception) -> str:
        """エラーメッセージから token を含む可能性のある詳細を排除。"""
        if isinstance(error, NotionAPIError):
            return f"NotionAPIError({error.status}): {error.message}"
        if isinstance(error, NotionRateLimitError):
            return f"NotionRateLimit(retry_after={error.retry_after:.1f}s)"
        return f"{type(error).__name__}"

    @staticmethod
    def _build_idempotency_key(event_type: str, payload: dict[str, Any]) -> str:
        """workflow_id:event_type:phase:revision の冪等キーを構築。"""
        workflow_id = payload.get("workflow_id", "unknown")
        phase = payload.get("current_phase", "?")
        revision = payload.get("revision", "0")
        return f"{workflow_id}:{event_type}:{phase}:{revision}"

    def _get_workflows_client(self) -> WorkflowsDBClient:
        if self._workflows_db is None:
            self._workflows_db = WorkflowsDBClient(
                api=self._get_api(),
                database_id=os.environ[self._config.workflows_db_id_env],
            )
        return self._workflows_db

    def _get_pull_requests_client(self, database_id: str) -> PullRequestsDBClient:
        if self._pull_requests_db is None:
            self._pull_requests_db = PullRequestsDBClient(
                api=self._get_api(),
                database_id=database_id,
            )
        return self._pull_requests_db

    def _lookup_workflow_page_id(self, workflow_id: str) -> str | None:
        """workflow_id → page_id 解決。positive 結果のみキャッシュする
        （PR #37 Copilot 7 回目指摘で per-issue 重複 query を抑止）。

        - 既に positive キャッシュにあれば即返す（API call 無し）。
        - キャッシュに無い／negative の場合は Workflows DB に lookup し、
          positive 結果のみキャッシュに保存。
        - workflow_started イベントを `_send_to_notion` が扱う際にエントリを
          invalidate するため、新規 page 作成後の stale キャッシュは発生しない。
        - lookup API エラーは raise してそのまま `dispatch()` まで伝播
          （outbox 経由でリトライ可能、Copilot 3 回目指摘の挙動を維持）。
        """
        cached = self._workflow_page_id_cache.get(workflow_id)
        if cached is not None:
            return cached
        page_id = self._get_workflows_client()._find_page_id(workflow_id)
        if page_id is not None:
            self._workflow_page_id_cache[workflow_id] = page_id
        return page_id

    def _get_review_issues_client(self, database_id: str) -> ReviewIssuesDBClient:
        if self._review_issues_db is None:
            self._review_issues_db = ReviewIssuesDBClient(
                api=self._get_api(),
                database_id=database_id,
            )
        return self._review_issues_db

    def _get_work_items_client(self, database_id: str) -> WorkItemsDBClient:
        if self._work_items_db is None:
            self._work_items_db = WorkItemsDBClient(
                api=self._get_api(),
                database_id=database_id,
            )
        return self._work_items_db

    def _get_workflow_gates_client(
        self, database_id: str
    ) -> WorkflowGatesDBClient:
        if self._workflow_gates_db is None:
            self._workflow_gates_db = WorkflowGatesDBClient(
                api=self._get_api(),
                database_id=database_id,
            )
        return self._workflow_gates_db

    def _get_project_memory_client(
        self, database_id: str
    ) -> ProjectMemoryDBClient:
        if self._project_memory_db is None:
            self._project_memory_db = ProjectMemoryDBClient(
                api=self._get_api(),
                database_id=database_id,
            )
        return self._project_memory_db

    def _get_api(self) -> NotionAPIClient:
        if self._api is None:
            self._api = NotionAPIClient(
                api_token=os.environ[self._config.api_token_env],
                max_attempts=self._config.retry.max_attempts,
                backoff_seconds=self._config.retry.backoff_seconds,
                requests_per_second=self._config.rate_limit.requests_per_second,
            )
        return self._api
