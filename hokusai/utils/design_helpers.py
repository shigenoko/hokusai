"""
Design context helpers

Phase ノードから design context（Figma / Miro）を取得・利用するための
薄いユーティリティ。実装方針:

- Phase 2 開始時に `ensure_design_context()` を呼び、state に Miro/Figma の
  正規化済みコンテキストを保存する。
- それ以降の Phase（3/4/5/7/8/10）は state からコンテキストを参照するだけ。
  必要に応じて再取得もできるが、デフォルトではキャッシュ任せ。
- プロンプトに差し込む Markdown は `format_design_context_section()` で取得。

このモジュールは LangGraph の WorkflowState を直接書き換える。
失敗しても workflow を止めないよう、すべての例外を握り潰して警告に丸める。
"""

from __future__ import annotations

from typing import Any

from ..integrations.design import (
    DesignContextResolver,
    DesignResolution,
    extract_figma_urls,
    extract_miro_urls,
)
from ..integrations.design.url_parser import parse_figma_url, parse_miro_url
from ..logging_config import get_logger

logger = get_logger("utils.design_helpers")


def ensure_design_context(
    state: dict,
    *,
    text_sources: list[str | None] | None = None,
    resolver: DesignContextResolver | None = None,
) -> dict:
    """state に miro_context / figma_context が無ければ resolve して埋める。

    既に design_integration_status が set されていれば再 resolve しない。
    text_sources は task_description / research_result 等の任意のテキスト。

    Returns:
        更新された state（in-place 変更も行うが、呼び出し側の安全のため返す）。
    """
    if state.get("design_integration_status"):
        return state

    sources: list[str | None] = []
    if text_sources:
        sources.extend(text_sources)
    # state に既に explicit な miro_url / figma_url が入っているケースを想定
    explicit_figma = state.get("figma_url")
    explicit_miro = state.get("miro_url")
    sources.extend([
        state.get("task_url"),
        state.get("task_title"),
        state.get("research_result"),
    ])

    try:
        resolver = resolver or DesignContextResolver()
        resolution = resolver.resolve(
            figma_url=explicit_figma,
            miro_url=explicit_miro,
            text_sources=sources,
        )
    except Exception as exc:
        logger.warning("design context resolve failed: %s", exc)
        state["design_integration_status"] = "failed"
        state["design_sync_errors"] = state.get("design_sync_errors") or []
        state["design_sync_errors"].append({
            "source": "resolver",
            "error": f"{type(exc).__name__}: {exc}",
        })
        return state

    _apply_resolution(state, resolution)
    return state


def get_design_resolution(state: dict) -> DesignResolution | None:
    """state に保存済みの design context を DesignResolution 形式で再構成する。

    プロンプト埋め込み用。state を直接いじらないため再 resolve はしない。
    """
    from ..integrations.design.context import ResolutionStatus

    integration = state.get("design_integration_status")
    if not integration:
        return None

    figma_ctx = state.get("figma_context")
    miro_ctx = state.get("miro_context")

    figma_status = "ok" if figma_ctx else "no_url"
    miro_status = "ok" if miro_ctx else "no_url"

    return DesignResolution(
        figma=ResolutionStatus(
            source="figma",
            status=figma_status,
            url=state.get("figma_url"),
            context=figma_ctx,
        ),
        miro=ResolutionStatus(
            source="miro",
            status=miro_status,
            url=state.get("miro_url"),
            context=miro_ctx,
        ),
        integration_status=integration,
        sync_errors=state.get("design_sync_errors") or [],
        block=False,
    )


def format_design_context_section(state: dict) -> str:
    """state からプロンプト差し込み用 Markdown を生成。何もなければ空文字列。"""
    res = get_design_resolution(state)
    if res is None:
        return ""
    return DesignContextResolver.render_markdown(res)


def design_links_for_record(state: dict) -> dict[str, Any]:
    """Phase 8 の MR description / Phase 10 の Notion 記録向けのリンク情報。"""
    return {
        "miro_url": state.get("miro_url"),
        "figma_url": state.get("figma_url"),
        "design_integration_status": state.get("design_integration_status"),
        "miro_summary": (state.get("miro_context") or {}).get("summary"),
        "figma_summary": (state.get("figma_context") or {}).get("summary"),
        "design_warnings": [
            *((state.get("miro_context") or {}).get("warnings") or []),
            *((state.get("figma_context") or {}).get("warnings") or []),
        ],
        "design_sync_errors": state.get("design_sync_errors") or [],
    }


# ---------- 内部 ----------


def _apply_resolution(state: dict, resolution: DesignResolution) -> None:
    """resolve 結果を state に書き込む。"""
    figma_url = resolution.figma.url
    miro_url = resolution.miro.url

    if figma_url and not state.get("figma_url"):
        state["figma_url"] = figma_url
        try:
            parsed = parse_figma_url(figma_url)
            state["figma_file_key"] = parsed.file_key
            state["figma_target_node_id"] = parsed.node_id
        except ValueError:
            pass
    if miro_url and not state.get("miro_url"):
        state["miro_url"] = miro_url
        try:
            parsed_m = parse_miro_url(miro_url)
            state["miro_board_id"] = parsed_m.board_id
        except ValueError:
            pass

    state["figma_context"] = resolution.figma.context
    state["miro_context"] = resolution.miro.context
    state["design_integration_status"] = resolution.integration_status
    if resolution.sync_errors:
        existing = state.get("design_sync_errors") or []
        existing.extend(resolution.sync_errors)
        state["design_sync_errors"] = existing
    state["design_review_required"] = bool(
        (resolution.figma.context and resolution.figma.context.get("comments")) or
        resolution.figma.status == "partial"
    )
