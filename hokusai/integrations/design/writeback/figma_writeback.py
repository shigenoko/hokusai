"""Figma 書き戻し dispatcher。

Phase 8a 完了時に Figma frame コメントを投稿する。
失敗時は figma_sync_outbox に積み、Operations Console から手動再送できる。

計画書 §3 / §6.1 / §9.2 / §11 (Step 3) に対応。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ....logging_config import get_logger
from ..figma import FigmaAPIError, FigmaClient, FigmaRateLimitError
from .idempotency import build_idempotency_key
from .outbox import OutboxStore, WritebackTarget
from .templates import build_figma_payload, render_figma_message

logger = get_logger("integrations.design.writeback.figma")


@dataclass
class FigmaWritebackArgs:
    """Figma 書き戻しに必要な引数の集合"""

    workflow_id: str
    profile_name: str | None
    event_type: str          # "phase8a_completed" 等
    revision: str            # commit sha や MR iid
    file_key: str            # state.primary_figma_file_key
    node_id: str             # state.primary_figma_node_id
    node_offset: dict[str, float] | None  # state.primary_figma_node_offset
    mr_url: str | None
    commit_sha: str | None


class FigmaWritebackDispatcher:
    """Figma frame コメント投稿の dispatcher。

    on_failure: warn を既定とし、失敗時は outbox に積んで継続する。
    """

    def __init__(self, client: FigmaClient, store: OutboxStore):
        if store.target != WritebackTarget.FIGMA:
            raise ValueError(
                f"FigmaWritebackDispatcher requires FIGMA target, got {store.target}"
            )
        self.client = client
        self.store = store

    def dispatch(self, args: FigmaWritebackArgs, *, force: bool = False) -> dict[str, Any]:
        """1 回の投稿試行。

        Args:
            args: 投稿引数
            force: True なら errors にあっても再試行する（Operations Console から強制再送）

        Returns:
            投稿結果のサマリ:
              {"status": "delivered" | "skipped" | "enqueued",
               "idempotency_key": str,
               "response_id": str | None,
               "error": str | None}

        この関数自体は例外を投げない（best effort）。
        例外は outbox 経由で運用者に伝える。
        """
        idempotency_key = build_idempotency_key(
            workflow_id=args.workflow_id,
            event_type=args.event_type,
            resource=f"figma_{args.node_id}",
            revision=args.revision,
        )

        # 3 段階チェック（投稿済み / pending / errors）
        if self.store.should_skip(idempotency_key, force=force):
            logger.info(
                "figma writeback skipped (already delivered or pending): %s",
                idempotency_key,
            )
            return {
                "status": "skipped",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": None,
            }

        message = render_figma_message(mr_url=args.mr_url, commit_sha=args.commit_sha)
        payload_for_outbox = {
            "file_key": args.file_key,
            "node_id": args.node_id,
            "node_offset": args.node_offset or {"x": 0, "y": 0},
            "message": message,
            "mr_url": args.mr_url,
            "commit_sha": args.commit_sha,
        }

        try:
            response = self.client.post_comment(
                args.file_key,
                message=message,
                node_id=args.node_id,
                node_offset=args.node_offset,
            )
        except (FigmaAPIError, FigmaRateLimitError) as e:
            logger.warning(
                "figma post_comment failed (%s): %s",
                type(e).__name__, str(e),
            )
            self.store.enqueue(
                idempotency_key=idempotency_key,
                workflow_id=args.workflow_id,
                profile_name=args.profile_name,
                event_type=args.event_type,
                payload=payload_for_outbox,
                error=str(e),
            )
            return {
                "status": "enqueued",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": str(e),
            }
        except Exception as e:
            # ネットワーク / urllib エラー等。token は ValueError で弾く想定なので
            # ここでは例外メッセージから token を漏らさないため type 名のみ記録。
            logger.warning(
                "figma post_comment unexpected error: %s",
                type(e).__name__,
            )
            self.store.enqueue(
                idempotency_key=idempotency_key,
                workflow_id=args.workflow_id,
                profile_name=args.profile_name,
                event_type=args.event_type,
                payload=payload_for_outbox,
                error=f"{type(e).__name__}: {e}",
            )
            return {
                "status": "enqueued",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": f"{type(e).__name__}: {e}",
            }

        response_id = str(response.get("id") or "") or None
        self.store.mark_succeeded(
            idempotency_key=idempotency_key,
            workflow_id=args.workflow_id,
            profile_name=args.profile_name,
            resource=f"figma_{args.node_id}",
            response_id=response_id,
        )
        logger.info(
            "figma writeback delivered: workflow=%s key=%s response_id=%s",
            args.workflow_id, idempotency_key, response_id,
        )
        return {
            "status": "delivered",
            "idempotency_key": idempotency_key,
            "response_id": response_id,
            "error": None,
        }

    def retry(self, outbox_id: int, *, force: bool = False) -> dict[str, Any]:
        """手動再送（Operations Console から呼ぶ）。

        attempt_count >= MAX_ATTEMPT_COUNT に達したら errors へ移動する。
        """
        entry = self.store.get_outbox(outbox_id)
        if entry is None:
            return {"status": "not_found", "error": f"outbox id {outbox_id} not found"}

        # attempt_count を先に +1（試行を記録）
        new_count = self.store.increment_attempt(outbox_id)
        from .outbox import MAX_ATTEMPT_COUNT
        if new_count >= MAX_ATTEMPT_COUNT:
            self.store.move_to_errors(
                outbox_id,
                error=f"max attempts ({MAX_ATTEMPT_COUNT}) exceeded after retry",
            )
            return {
                "status": "moved_to_errors",
                "idempotency_key": entry.idempotency_key,
                "error": f"max attempts ({MAX_ATTEMPT_COUNT}) exceeded",
            }

        # payload から再構築して dispatch
        p = entry.payload
        args = FigmaWritebackArgs(
            workflow_id=entry.workflow_id,
            profile_name=entry.profile_name,
            event_type=entry.event_type,
            revision=str(p.get("commit_sha") or "(unknown)"),
            file_key=p.get("file_key", ""),
            node_id=p.get("node_id", ""),
            node_offset=p.get("node_offset"),
            mr_url=p.get("mr_url"),
            commit_sha=p.get("commit_sha"),
        )
        return self.dispatch(args, force=force)
