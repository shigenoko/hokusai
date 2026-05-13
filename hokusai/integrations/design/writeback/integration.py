"""WorkflowRunner ⇄ writeback dispatcher の統合層。

責務:
- WorkflowConfig から writeback 用 dispatcher を構築（client + outbox store）
- Phase 3 で design_context から primary_* を決定する関数
- Phase 8a 完了時に Figma / Miro の両方に dispatch する高レベル関数

計画書 §4.3 / §7.2 / §11 (Step 5) に対応。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ....logging_config import get_logger
from ..figma import FigmaClient
from ..miro import MiroClient
from .figma_writeback import FigmaWritebackArgs, FigmaWritebackDispatcher
from .miro_writeback import MiroWritebackArgs, MiroWritebackDispatcher
from .outbox import OutboxStore, WritebackTarget

logger = get_logger("integrations.design.writeback.integration")


@dataclass
class WritebackEnabledConfig:
    """書き戻し機能の有効/無効と on_failure を保持する config 抽象。

    config YAML の以下を反映:
      figma.writeback.enabled / figma.writeback.on_failure
      miro.writeback.enabled  / miro.writeback.on_failure
    """

    figma_enabled: bool = False
    figma_on_failure: str = "warn"   # warn | block | skip
    figma_token_env: str | None = None
    miro_enabled: bool = False
    miro_on_failure: str = "warn"
    miro_token_env: str | None = None


_VALID_ON_FAILURE = ("warn", "block", "skip")


def _normalize_on_failure(value: Any, default: str = "warn") -> str:
    """on_failure の値を warn/block/skip に正規化する。

    None / 不正値はすべて default にフォールバック。これがないと dispatcher
    側 (§8.1) で ValueError が出て dispatcher 構築 / retry API が 500 になる。
    """
    if value is None:
        return default
    s = str(value)
    return s if s in _VALID_ON_FAILURE else default


def load_writeback_config(workflow_config: Any) -> WritebackEnabledConfig:
    """既存 WorkflowConfig（dataclass）から writeback 設定を抽出。

    既存 config 構造を壊さないため、属性が無い場合は既定値（disabled）を返す。
    on_failure に不正値が来た場合は既定 warn にフォールバック（dispatcher が
    ValueError で落ちて 500 になるのを防ぐ）。
    """
    figma_cfg = getattr(workflow_config, "figma", None) or {}
    miro_cfg = getattr(workflow_config, "miro", None) or {}

    # 既存 config が dict / dataclass どちらでも動くよう .get / getattr を試す
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    figma_writeback = _get(figma_cfg, "writeback") or {}
    miro_writeback = _get(miro_cfg, "writeback") or {}

    return WritebackEnabledConfig(
        figma_enabled=bool(_get(figma_writeback, "enabled", False)),
        figma_on_failure=_normalize_on_failure(_get(figma_writeback, "on_failure")),
        figma_token_env=_get(figma_cfg, "api_token_env"),
        miro_enabled=bool(_get(miro_writeback, "enabled", False)),
        miro_on_failure=_normalize_on_failure(_get(miro_writeback, "on_failure")),
        miro_token_env=_get(miro_cfg, "api_token_env"),
    )


def build_figma_dispatcher(
    db_path: Path,
    writeback_cfg: WritebackEnabledConfig,
) -> FigmaWritebackDispatcher | None:
    """Figma writeback が有効かつ token が設定されている時のみ dispatcher を返す"""
    if not writeback_cfg.figma_enabled:
        return None
    env_name = writeback_cfg.figma_token_env
    token = os.environ.get(env_name) if env_name else None
    if not token:
        logger.info(
            "figma writeback disabled (token env %r is not set)", env_name,
        )
        return None
    client = FigmaClient(api_token=token)
    store = OutboxStore(db_path, target=WritebackTarget.FIGMA)
    return FigmaWritebackDispatcher(
        client, store, on_failure=writeback_cfg.figma_on_failure,
    )


def build_miro_dispatcher(
    db_path: Path,
    writeback_cfg: WritebackEnabledConfig,
) -> MiroWritebackDispatcher | None:
    """Miro writeback が有効かつ token が設定されている時のみ dispatcher を返す"""
    if not writeback_cfg.miro_enabled:
        return None
    env_name = writeback_cfg.miro_token_env
    token = os.environ.get(env_name) if env_name else None
    if not token:
        logger.info(
            "miro writeback disabled (token env %r is not set)", env_name,
        )
        return None
    client = MiroClient(api_token=token)
    store = OutboxStore(db_path, target=WritebackTarget.MIRO)
    return MiroWritebackDispatcher(
        client, store, on_failure=writeback_cfg.miro_on_failure,
    )


# ---------------------------------------------------------------------------
# Phase 3: primary_* の決定
# ---------------------------------------------------------------------------


def decide_primary_figma(state: dict[str, Any]) -> dict[str, Any]:
    """state.figma_context（design_context）から primary Figma frame を決める。

    Returns:
        state に書き戻す primary_* キーの dict（空の場合は書き戻し不要）。

    決定ルール（計画書 §7.2 Figma）:
      1. figma_context が空 → 何も返さない
      2. state.figma_target_node_id があればそれを node_id に
      3. それ以外は figma_context の screens / nodes 先頭を採用
    """
    file_key = state.get("figma_file_key")
    if not file_key:
        return {}

    figma_ctx = state.get("figma_context") or {}
    target_node_id = state.get("figma_target_node_id")
    node_id = target_node_id

    if not node_id:
        screens = figma_ctx.get("screens") or []
        if screens:
            # screens[0]["id"] が node_id を保持する想定（既存形式）
            node_id = screens[0].get("id") or screens[0].get("node_id")

    if not node_id:
        return {}

    return {
        "primary_figma_file_key": file_key,
        "primary_figma_frame_id": node_id,
        "primary_figma_node_id": node_id,
        "primary_figma_node_offset": {"x": 0, "y": 0},
    }


def decide_primary_miro(state: dict[str, Any]) -> dict[str, Any]:
    """state.miro_context から primary Miro frame + board を決める。

    決定ルール（計画書 §7.2 Miro）:
      1. miro_context が空 → 何も返さない
      2. miro_board_id を採用、frames 先頭を frame_id に
    """
    board_id = state.get("miro_board_id")
    if not board_id:
        return {}

    miro_ctx = state.get("miro_context") or {}
    screens = miro_ctx.get("screens") or []
    if not screens:
        return {}

    frame = screens[0]
    # MiroClient.to_common_context() の screens は node_id キーを使う。
    # id / frame_id は旧 schema 互換のために fallback として受け付ける。
    frame_id = (
        frame.get("node_id")
        or frame.get("id")
        or frame.get("frame_id")
    )
    if not frame_id:
        return {}

    return {
        "primary_miro_frame_id": frame_id,
        "primary_miro_board_id": board_id,
    }


def populate_primary_writeback_targets(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 3 で state に primary_* を書き込む（既存値は上書きしない）"""
    for k, v in decide_primary_figma(state).items():
        if not state.get(k):
            state[k] = v
    for k, v in decide_primary_miro(state).items():
        if not state.get(k):
            state[k] = v
    return state


# ---------------------------------------------------------------------------
# Phase 8a: dispatch
# ---------------------------------------------------------------------------


@dataclass
class WritebackResult:
    """Phase 8a 完了時の writeback 試行サマリ"""

    figma: dict[str, Any] | None = None
    miro: dict[str, Any] | None = None


def dispatch_phase8a_writeback(
    state: dict[str, Any],
    *,
    mr_url: str | None,
    commit_sha: str | None,
    revision: str | None = None,
    figma_dispatcher: FigmaWritebackDispatcher | None = None,
    miro_dispatcher: MiroWritebackDispatcher | None = None,
    profile_name: str | None = None,
    workflow_id: str | None = None,
) -> WritebackResult:
    """Phase 8a 完了時に Figma / Miro 両方に dispatch する。

    primary_* が未設定なら該当 target は skip。
    例外は伝播せず、各 dispatcher 内で outbox に記録される（best effort）。
    """
    result = WritebackResult()
    wf_id = workflow_id or state.get("workflow_id") or ""
    rev = revision or commit_sha or "(unknown)"

    if figma_dispatcher is not None:
        file_key = state.get("primary_figma_file_key")
        node_id = state.get("primary_figma_node_id")
        if file_key and node_id and wf_id:
            args = FigmaWritebackArgs(
                workflow_id=wf_id,
                profile_name=profile_name,
                event_type="phase8a_completed",
                revision=rev,
                file_key=file_key,
                node_id=node_id,
                node_offset=state.get("primary_figma_node_offset"),
                mr_url=mr_url,
                commit_sha=commit_sha,
            )
            result.figma = figma_dispatcher.dispatch(args)
        else:
            logger.info(
                "figma writeback skipped: primary_figma_* not set (workflow=%s)",
                wf_id,
            )
            result.figma = {"status": "skipped", "error": "primary_figma_* not set"}

    if miro_dispatcher is not None:
        board_id = state.get("primary_miro_board_id")
        frame_id = state.get("primary_miro_frame_id")
        if board_id and frame_id and wf_id:
            # frame_meta は miro_context から復元（無ければ 0 で構築）
            # MiroClient.to_common_context() の screens は node_id キーを使う。
            # 座標情報（x/y/width）は _build_miro_screens が geometry から保存する。
            miro_ctx = state.get("miro_context") or {}
            screens = miro_ctx.get("screens") or []
            frame_meta: dict[str, Any] = {}
            for s in screens:
                screen_id = (
                    s.get("node_id")
                    or s.get("id")
                    or s.get("frame_id")
                )
                if screen_id == frame_id:
                    frame_meta = {
                        "x": s.get("x", 0),
                        "y": s.get("y", 0),
                        "width": s.get("width", 0),
                    }
                    break
            args = MiroWritebackArgs(
                workflow_id=wf_id,
                profile_name=profile_name,
                event_type="phase8a_completed",
                revision=rev,
                board_id=board_id,
                frame_id=frame_id,
                frame_meta=frame_meta,
                mr_url=mr_url,
                commit_sha=commit_sha,
            )
            result.miro = miro_dispatcher.dispatch(args)
        else:
            logger.info(
                "miro writeback skipped: primary_miro_* not set (workflow=%s)",
                wf_id,
            )
            result.miro = {"status": "skipped", "error": "primary_miro_* not set"}

    return result
