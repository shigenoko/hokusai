"""Figma 書き戻し dispatcher。

Phase 8a 完了時に Figma frame コメントを投稿する。
失敗時は figma_sync_outbox に積み、Operations Console から手動再送できる。

計画書 §3 / §6.1 / §9.2 / §11 (Step 3) に対応。
"""

from __future__ import annotations

import urllib.error
from dataclasses import dataclass
from typing import Any

from ....logging_config import get_logger
from ..figma import FigmaAPIError, FigmaClient, FigmaRateLimitError
from .idempotency import build_idempotency_key
from .outbox import OutboxStore, WritebackTarget
from .templates import render_figma_message

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

    on_failure ポリシー（計画書 §8.2）:
      - warn（既定）: outbox に積んで継続
      - block: outbox に積み、result.status を "blocked" にする（呼び出し側が
               workflow を waiting_for_human に遷移）
      - skip: outbox にも積まずに warning のみ
    """

    def __init__(
        self,
        client: FigmaClient,
        store: OutboxStore,
        *,
        on_failure: str = "warn",
    ):
        if store.target != WritebackTarget.FIGMA:
            raise ValueError(
                f"FigmaWritebackDispatcher requires FIGMA target, got {store.target}"
            )
        if on_failure not in ("warn", "block", "skip"):
            raise ValueError(f"on_failure must be warn|block|skip, got {on_failure!r}")
        self.client = client
        self.store = store
        self.on_failure = on_failure

    def dispatch(
        self,
        args: FigmaWritebackArgs,
        *,
        force: bool = False,
        from_retry: bool = False,
    ) -> dict[str, Any]:
        """1 回の投稿試行。

        Args:
            args: 投稿引数
            force: True なら errors にあっても再試行する（Operations Console から強制再送）
            from_retry: True なら is_pending チェックを skip する（retry 経路で
                       同じ outbox 行を再試行するため）

        Returns:
            投稿結果のサマリ:
              {"status": "delivered" | "skipped" | "enqueued" | "blocked",
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

        # 投稿済みは常に skip
        if self.store.is_already_delivered(idempotency_key):
            logger.info("figma writeback skipped (already delivered): %s", idempotency_key)
            return {
                "status": "skipped",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": None,
            }
        # pending スキップ（retry 経路では bypass）
        if not from_retry and self.store.is_pending(idempotency_key):
            logger.info("figma writeback skipped (pending): %s", idempotency_key)
            return {
                "status": "skipped",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": None,
            }
        # errors（諦め済）は force=True でない限り skip
        if not force and self.store.is_in_errors(idempotency_key):
            logger.info("figma writeback skipped (in errors): %s", idempotency_key)
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
            return self._handle_failure(
                idempotency_key=idempotency_key,
                args=args,
                payload_for_outbox=payload_for_outbox,
                error=str(e),
            )
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as e:
            # ネットワーク / OS エラー / _request 内最終 raise の RuntimeError。
            # token は ValueError で post_comment 入口で弾く想定なので、ここでは
            # 例外メッセージから token を漏らさないため type 名のみ記録。
            logger.warning(
                "figma post_comment network/runtime error: %s",
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

    def _handle_failure(
        self,
        *,
        idempotency_key: str,
        args: FigmaWritebackArgs,
        payload_for_outbox: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        """on_failure に応じて失敗を処理する。"""
        if self.on_failure == "skip":
            # outbox にも積まず、warning だけ返す
            return {
                "status": "skipped",
                "idempotency_key": idempotency_key,
                "response_id": None,
                "error": error,
                "on_failure": "skip",
            }

        # warn / block どちらも outbox に積む
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

        attempt_count を +1 して **再試行を実行**し、その結果が enqueued（再失敗）で
        かつ attempt_count が MAX に到達していたら errors へ移動する。
        5 回目の再送リクエストでも実際に投稿を試みる動作にするため、
        判定タイミングを dispatch 後にする。
        """
        from .outbox import MAX_ATTEMPT_COUNT

        entry = self.store.get_outbox(outbox_id)
        if entry is None:
            return {"status": "not_found", "error": f"outbox id {outbox_id} not found"}

        # attempt_count を +1 して試行を記録（dispatch 前）
        new_count = self.store.increment_attempt(outbox_id)

        # payload から再構築して dispatch（5 回目でも実際に試行する）
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
        result = self.dispatch(args, force=force, from_retry=True)

        # 再失敗かつ MAX 到達なら errors 移動
        if result.get("status") == "enqueued" and new_count >= MAX_ATTEMPT_COUNT:
            self.store.move_to_errors(
                outbox_id,
                error=(
                    f"max attempts ({MAX_ATTEMPT_COUNT}) exceeded "
                    f"(last error: {result.get('error')})"
                ),
            )
            result["status"] = "moved_to_errors"

        return result
