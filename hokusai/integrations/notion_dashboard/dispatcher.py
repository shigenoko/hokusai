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
from .pull_requests_db import PullRequestsDBClient
from .workflows_db import WorkflowsDBClient

logger = get_logger("integrations.notion_dashboard.dispatcher")


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
        if event_type == "pr_created":
            payload = self._enrich_with_sync_status(
                payload, exclude_idempotency_key=exclude_idempotency_key
            )
            self._handle_pr_created(payload)
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

    def _get_api(self) -> NotionAPIClient:
        if self._api is None:
            self._api = NotionAPIClient(
                api_token=os.environ[self._config.api_token_env],
                max_attempts=self._config.retry.max_attempts,
                backoff_seconds=self._config.retry.backoff_seconds,
                requests_per_second=self._config.rate_limit.requests_per_second,
            )
        return self._api
