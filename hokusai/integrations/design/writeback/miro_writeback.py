"""Miro 書き戻し dispatcher。

Phase 8a 完了時に Miro board に進捗 card を作成する。
失敗時は miro_sync_outbox に積み、Operations Console から手動再送できる。

計画書 §3 / §6.2 / §9.2 / §11 (Step 4) に対応。
"""

from __future__ import annotations

import urllib.error
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
    """Miro card 投稿の dispatcher。

    on_failure ポリシー（計画書 §8.2）:
      - warn（既定）: outbox に積んで継続
      - block: outbox に積み、result.status を "blocked" にする（呼び出し側が
               workflow を waiting_for_human に遷移）
      - skip: outbox にも積まずに warning のみ
    """

    def __init__(
        self,
        client: MiroClient,
        store: OutboxStore,
        *,
        on_failure: str = "warn",
    ):
        if store.target != WritebackTarget.MIRO:
            raise ValueError(
                f"MiroWritebackDispatcher requires MIRO target, got {store.target}"
            )
        if on_failure not in ("warn", "block", "skip"):
            raise ValueError(f"on_failure must be warn|block|skip, got {on_failure!r}")
        self.client = client
        self.store = store
        self.on_failure = on_failure

    def dispatch(
        self,
        args: MiroWritebackArgs,
        *,
        force: bool = False,
        from_retry: bool = False,
    ) -> dict[str, Any]:
        """1 回の投稿試行。

        Args:
            force: True なら errors にあっても再試行
            from_retry: True なら is_pending チェックを skip（retry 経路用）
        """
        idempotency_key = build_idempotency_key(
            workflow_id=args.workflow_id,
            event_type=args.event_type,
            resource=f"miro_{args.frame_id}",
            revision=args.revision,
        )

        if self.store.is_already_delivered(idempotency_key):
            logger.info("miro writeback skipped (already delivered): %s", idempotency_key)
            return {
                "status": "skipped",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": None,
            }
        if not from_retry and self.store.is_pending(idempotency_key):
            logger.info("miro writeback skipped (pending): %s", idempotency_key)
            return {
                "status": "skipped",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": None,
            }
        if not force and self.store.is_in_errors(idempotency_key):
            logger.info("miro writeback skipped (in errors): %s", idempotency_key)
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
            "event_type": args.event_type,
            # retry 時に同じ idempotency_key を再構築するため revision を明示保存
            "revision": args.revision,
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
            return self._handle_failure(
                idempotency_key=idempotency_key,
                args=args,
                payload_for_outbox=payload_for_outbox,
                error=str(e),
            )
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as e:
            # ネットワーク / OS エラー / _request 内最終 raise の RuntimeError
            logger.warning(
                "miro create_card network/runtime error: %s",
                type(e).__name__,
            )
            return self._handle_failure(
                idempotency_key=idempotency_key,
                args=args,
                payload_for_outbox=payload_for_outbox,
                error=f"{type(e).__name__}: {e}",
            )

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

    def _handle_failure(
        self,
        *,
        idempotency_key: str,
        args: MiroWritebackArgs,
        payload_for_outbox: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        """on_failure に応じて失敗を処理する。"""
        if self.on_failure == "skip":
            return {
                "status": "skipped",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": error,
                "on_failure": "skip",
            }
        self.store.enqueue(
            idempotency_key=idempotency_key,
            workflow_id=args.workflow_id,
            profile_name=args.profile_name,
            event_type=args.event_type,
            payload=payload_for_outbox,
            error=error,
        )
        if self.on_failure == "block":
            return {
                "status": "blocked",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": error,
                "on_failure": "block",
            }
        return {
            "status": "enqueued",
            "idempotency_key": idempotency_key,
            "response_id": None,
            "error": error,
        }

    def retry(self, outbox_id: int, *, force: bool = False) -> dict[str, Any]:
        """手動再送（Operations Console から呼ぶ）。

        attempt_count を +1 して **再試行を実行**し、その結果が「失敗で outbox が
        残るケース」（enqueued / blocked）で MAX 到達していたら errors へ移動する。
        on_failure="block" の dispatcher でも MAX 到達時に確実に errors 移動される
        よう、blocked も対象に含める。
        5 回目の再送でも実際に投稿を試みるよう、判定を dispatch 後に行う。
        """
        entry = self.store.get_outbox(outbox_id)
        if entry is None:
            return {"status": "not_found", "error": f"outbox id {outbox_id} not found"}

        # attempt_count を +1（dispatch 前に記録）
        new_count = self.store.increment_attempt(outbox_id)

        # 5 回目でも実際に試行する
        # revision は payload に明示保存している（dispatch 時と同じキーを再生成）
        p = entry.payload
        args = MiroWritebackArgs(
            workflow_id=entry.workflow_id,
            profile_name=entry.profile_name,
            event_type=p.get("event_type", entry.event_type),
            revision=str(p.get("revision") or p.get("commit_sha") or "(unknown)"),
            board_id=p.get("board_id", ""),
            frame_id=p.get("frame_id", ""),
            frame_meta=p.get("frame_meta", {}),
            mr_url=p.get("mr_url"),
            commit_sha=p.get("commit_sha"),
        )
        result = self.dispatch(args, force=force, from_retry=True)

        # 再失敗かつ MAX 到達なら errors 移動。
        # enqueued（on_failure=warn）と blocked（on_failure=block）どちらも
        # 「outbox に残っている失敗」なので、5 回目で errors に動かす。
        if (
            result.get("status") in ("enqueued", "blocked")
            and new_count >= MAX_ATTEMPT_COUNT
        ):
            self.store.move_to_errors(
                outbox_id,
                error=(
                    f"max attempts ({MAX_ATTEMPT_COUNT}) exceeded "
                    f"(last error: {result.get('error')})"
                ),
            )
            result["status"] = "moved_to_errors"

        return result
