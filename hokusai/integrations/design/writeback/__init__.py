"""Figma / Miro 書き戻し（Phase E, v0.4.0）

Phase 8a 完了時に Figma frame / Miro board に進捗コメント / カードを投稿する。

詳細: docs/hokusai-figma-miro-writeback-implementation-plan.md
"""

from __future__ import annotations

from .figma_writeback import FigmaWritebackArgs, FigmaWritebackDispatcher
from .idempotency import build_idempotency_key
from .integration import (
    WritebackEnabledConfig,
    WritebackResult,
    build_figma_dispatcher,
    build_miro_dispatcher,
    decide_primary_figma,
    decide_primary_miro,
    dispatch_phase8a_writeback,
    load_writeback_config,
    populate_primary_writeback_targets,
)
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
    "WritebackEnabledConfig",
    "WritebackResult",
    "WritebackTarget",
    "build_figma_dispatcher",
    "build_idempotency_key",
    "build_miro_dispatcher",
    "decide_primary_figma",
    "decide_primary_miro",
    "dispatch_phase8a_writeback",
    "load_writeback_config",
    "populate_primary_writeback_targets",
]
