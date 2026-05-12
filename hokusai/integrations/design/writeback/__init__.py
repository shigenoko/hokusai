"""Figma / Miro 書き戻し（Phase E, v0.4.0）

Phase 8a 完了時に Figma frame / Miro board に進捗コメント / カードを投稿する。

詳細: docs/hokusai-figma-miro-writeback-implementation-plan.md
"""

from __future__ import annotations

from .figma_writeback import FigmaWritebackArgs, FigmaWritebackDispatcher
from .idempotency import build_idempotency_key
from .miro_writeback import MiroWritebackArgs, MiroWritebackDispatcher
from .outbox import MAX_ATTEMPT_COUNT, OutboxEntry, OutboxStore, WritebackTarget

__all__ = [
    "MAX_ATTEMPT_COUNT",
    "FigmaWritebackArgs",
    "FigmaWritebackDispatcher",
    "MiroWritebackArgs",
    "MiroWritebackDispatcher",
    "OutboxEntry",
    "OutboxStore",
    "WritebackTarget",
    "build_idempotency_key",
]
