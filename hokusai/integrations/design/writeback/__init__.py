"""Figma / Miro 書き戻し（Phase E, v0.4.0）

Phase 8a 完了時に Figma frame / Miro board に進捗コメント / カードを投稿する。

詳細: docs/hokusai-figma-miro-writeback-implementation-plan.md
"""

from __future__ import annotations

from .idempotency import build_idempotency_key
from .outbox import OutboxEntry, OutboxStore, WritebackTarget

__all__ = [
    "OutboxEntry",
    "OutboxStore",
    "WritebackTarget",
    "build_idempotency_key",
]
