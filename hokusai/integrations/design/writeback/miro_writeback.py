"""Miro 書き戻し dispatcher。

Phase 8a 完了時に Miro board に進捗 card を作成する。
失敗時は miro_sync_outbox に積み、Operations Console から手動再送できる。

計画書 §3 / §6.2 / §9.2 / §11 (Step 4) に対応。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ....logging_config import get_logger
from ..miro import MiroAPIError, MiroClient, MiroRateLimitError
from .idempotency import build_idempotency_key
from .outbox import MAX_ATTEMPT_COUNT, OutboxStore, WritebackTarget
from .templates import build_miro_card_payload

logger = get_logger("integrations.design.writeback.miro")


@dataclass
class MiroWritebackArgs:
    """Miro 書き戻しに必要な引数の集合"""

    workflow_id: str
    profile_name: str | None
    event_type: str
    revision: str
    board_id: str
    frame_id: str
    frame_meta: dict[str, Any] = field(default_factory=dict)  # x / y / width
    mr_url: str | None = None
    commit_sha: str | None = None


class MiroWritebackDispatcher:
    """Miro card 投稿の dispatcher。"""

    def __init__(self, client: MiroClient, store: OutboxStore):
        if store.target != WritebackTarget.MIRO:
            raise ValueError(
                f"MiroWritebackDispatcher requires MIRO target, got {store.target}"
            )
        self.client = client
        self.store = store

    def dispatch(self, args: MiroWritebackArgs, *, force: bool = False) -> dict[str, Any]:
        """1 回の投稿試行。"""
        idempotency_key = build_idempotency_key(
            workflow_id=args.workflow_id,
            event_type=args.event_type,
            resource=f"miro_{args.frame_id}",
            revision=args.revision,
        )

        if self.store.should_skip(idempotency_key, force=force):
            logger.info(
                "miro writeback skipped (already delivered or pending): %s",
                idempotency_key,
            )
            return {
                "status": "skipped",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": None,
            }

        card_payload = build_miro_card_payload(
            frame_meta=args.frame_meta,
            mr_url=args.mr_url,
            commit_sha=args.commit_sha,
        )

        payload_for_outbox = {
            "board_id": args.board_id,
            "frame_id": args.frame_id,
            "frame_meta": args.frame_meta,
            "mr_url": args.mr_url,
            "commit_sha": args.commit_sha,
            "card_payload": card_payload,
        }

        try:
            response = self.client.create_card(
                args.board_id,
                title=card_payload["data"]["title"],
                description=card_payload["data"].get("description"),
                position=card_payload.get("position"),
                style=card_payload.get("style"),
            )
        except (MiroAPIError, MiroRateLimitError) as e:
            logger.warning(
                "miro create_card failed (%s): %s",
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
            logger.warning(
                "miro create_card unexpected error: %s",
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
            resource=f"miro_{args.frame_id}",
            response_id=response_id,
        )
        logger.info(
            "miro writeback delivered: workflow=%s key=%s response_id=%s",
            args.workflow_id, idempotency_key, response_id,
        )
        return {
            "status": "delivered",
            "idempotency_key": idempotency_key,
            "response_id": response_id,
            "error": None,
        }

    def retry(self, outbox_id: int, *, force: bool = False) -> dict[str, Any]:
        """手動再送（Operations Console から呼ぶ）"""
        entry = self.store.get_outbox(outbox_id)
        if entry is None:
            return {"status": "not_found", "error": f"outbox id {outbox_id} not found"}

        new_count = self.store.increment_attempt(outbox_id)
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

        p = entry.payload
        args = MiroWritebackArgs(
            workflow_id=entry.workflow_id,
            profile_name=entry.profile_name,
            event_type=entry.event_type,
            revision=str(p.get("commit_sha") or "(unknown)"),
            board_id=p.get("board_id", ""),
            frame_id=p.get("frame_id", ""),
            frame_meta=p.get("frame_meta", {}),
            mr_url=p.get("mr_url"),
            commit_sha=p.get("commit_sha"),
        )
        return self.dispatch(args, force=force)
