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

import os
import re
from typing import Any

from ..integrations.design import DesignContextResolver, DesignResolution
from ..integrations.design.url_parser import parse_figma_url, parse_miro_url
from ..logging_config import get_logger

logger = get_logger("utils.design_helpers")

# Notion ページ URL から末尾の 32 桁 hex（ハイフンなし）を取り出す。
# 例: https://www.notion.so/Title-3599a8b82c7181d29a2ee1bbd99ae7bc → 3599a8...
_NOTION_PAGE_ID_RE = re.compile(r"([0-9a-fA-F]{32})(?:[?#]|$)")


def ensure_design_context(
    state: dict,
    *,
    text_sources: list[str | None] | None = None,
    resolver: DesignContextResolver | None = None,
    auto_fetch_task_body: bool = True,
) -> dict:
    """state に miro_context / figma_context が無ければ resolve して埋める。

    既に design_integration_status が set されていれば再 resolve しない。
    text_sources は呼び出し側が任意のテキストを渡せる（例: タスク本文）。

    auto_fetch_task_body=True（既定）の場合、Notion Dashboard の API トークン
    が設定されていれば task_url から本文を取得して text_sources に追加する。
    Notion 連携が未設定な環境では何もせず、URL は state に既にセットされた
    `figma_url` / `miro_url` か、`text_sources` 引数経由でのみ抽出される。

    Returns:
        更新された state（in-place 変更も行うが、呼び出し側の安全のため返す）。
    """
    if state.get("design_integration_status"):
        return state

    sources: list[str | None] = []
    if text_sources:
        sources.extend(text_sources)

    # Notion API 経由で task 本文を取得して URL 抽出のソースに追加。
    # Notion Dashboard 連携が無効な場合は何もしない。
    if auto_fetch_task_body:
        body = _fetch_notion_task_body(state.get("task_url"))
        if body:
            sources.append(body)

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


def _fetch_notion_task_body(task_url: str | None) -> str | None:
    """Notion API で task ページの本文ブロックと DB プロパティを取得して連結する。

    Notion Dashboard の API トークンが設定されている場合のみ動作する。失敗時は
    None を返し、呼び出し側の挙動を変えない（フォールバック責任は呼び出し側）。

    取得対象:
    - ページ properties（url / rich_text / title 等のテキスト系プロパティ）
      → DB のカスタムプロパティに Figma URL / Miro URL が入っているケースに対応
    - 子ブロック（paragraph / bulleted_list_item 等）
      → ページ本文に貼られた URL に対応
    """
    if not task_url or "notion.so" not in task_url:
        return None

    try:
        from ..config import get_config

        cfg = get_config()
        nd = cfg.notion_dashboard
        if not nd.enabled:
            return None
        api_token = os.environ.get(nd.api_token_env or "")
        if not api_token:
            return None

        page_id = _extract_notion_page_id(task_url)
        if not page_id:
            return None

        from ..integrations.notion_dashboard.client import NotionAPIClient

        api = NotionAPIClient(api_token)

        parts: list[str] = []

        # 1. ページ properties（DB カスタムプロパティに URL が入っているケース）
        try:
            page = api._request("GET", f"/pages/{page_id}")
            properties = page.get("properties") if isinstance(page, dict) else None
            if isinstance(properties, dict):
                props_text = _properties_to_text(properties)
                if props_text:
                    parts.append(props_text)
        except Exception as exc:
            logger.debug("notion page properties fetch skipped: %s", exc)

        # 2. 子ブロック（本文に URL が貼られているケース）
        # 1 階層のみ（必要に応じて再帰取得は将来拡張）
        try:
            result = api._request("GET", f"/blocks/{page_id}/children")
            blocks = result.get("results") if isinstance(result, dict) else None
            if isinstance(blocks, list):
                blocks_text = _blocks_to_text(blocks)
                if blocks_text:
                    parts.append(blocks_text)
        except Exception as exc:
            logger.debug("notion blocks fetch skipped: %s", exc)

        return "\n".join(parts) if parts else None
    except Exception as exc:
        logger.debug("notion task body fetch skipped: %s", exc)
        return None


def _extract_notion_page_id(url: str) -> str | None:
    """Notion URL の末尾 32 hex を UUID 形式に整形して返す。"""
    m = _NOTION_PAGE_ID_RE.search(url)
    if not m:
        return None
    raw = m.group(1).lower()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    """Notion ブロック配列から rich_text の plain_text を連結する（簡易）。"""
    lines: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        body = block.get(block_type) if isinstance(block_type, str) else None
        if not isinstance(body, dict):
            continue
        rich_text = body.get("rich_text")
        if not isinstance(rich_text, list):
            continue
        text = "".join(
            (r.get("plain_text") or "")
            for r in rich_text
            if isinstance(r, dict)
        )
        if text:
            lines.append(text)
    return "\n".join(lines)


def _properties_to_text(properties: dict[str, Any]) -> str:
    """Notion ページの properties 辞書から URL / text 系の値をテキスト化する。

    URL カラムや、rich_text に貼られた Figma / Miro リンクを抽出するため、
    extract_*_urls() が見つけられる形で `name: value` 形式で連結する。
    """
    lines: list[str] = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        value: str | None = None
        if ptype == "url":
            value = prop.get("url") if isinstance(prop.get("url"), str) else None
        elif ptype in ("rich_text", "title"):
            arr = prop.get(ptype)
            if isinstance(arr, list):
                value = "".join(
                    (r.get("plain_text") or "")
                    for r in arr
                    if isinstance(r, dict)
                )
        if value:
            lines.append(f"{name}: {value}")
    return "\n".join(lines)


def get_design_resolution(state: dict) -> DesignResolution | None:
    """state に保存済みの design context を DesignResolution 形式で再構成する。

    プロンプト埋め込み用。state を直接いじらないため再 resolve はしない。

    per-source の status は state["design_per_source_status"] に保存されていれば
    それを使う。古い state（フィールドが無い）の場合は、context の有無 +
    sync_errors を参照して fallback で復元する。
    """
    from ..integrations.design.context import ResolutionStatus

    integration = state.get("design_integration_status")
    if not integration:
        return None

    figma_ctx = state.get("figma_context")
    miro_ctx = state.get("miro_context")
    sync_errors = state.get("design_sync_errors") or []
    per_source = state.get("design_per_source_status") or {}

    figma_info = _restore_source_status(
        "figma", figma_ctx, sync_errors, per_source.get("figma"),
    )
    miro_info = _restore_source_status(
        "miro", miro_ctx, sync_errors, per_source.get("miro"),
    )

    return DesignResolution(
        figma=ResolutionStatus(
            source="figma",
            status=figma_info["status"],
            url=state.get("figma_url"),
            context=figma_ctx,
            error=figma_info["error"],
        ),
        miro=ResolutionStatus(
            source="miro",
            status=miro_info["status"],
            url=state.get("miro_url"),
            context=miro_ctx,
            error=miro_info["error"],
        ),
        integration_status=integration,
        sync_errors=sync_errors,
        block=False,
    )


def _restore_source_status(
    source: str,
    ctx: dict | None,
    sync_errors: list,
    saved: dict | None,
) -> dict:
    """1 つの source（figma/miro）の status と error を復元する。

    優先順:
    1. state["design_per_source_status"][source] に保存済みなら使う
    2. fallback: context があれば ok / partial、sync_errors にあれば failed、
       それ以外は no_url
    """
    if isinstance(saved, dict) and saved.get("status"):
        return {
            "status": str(saved["status"]),
            "error": saved.get("error"),
        }

    if ctx is not None:
        status = "partial" if ctx.get("warnings") else "ok"
        return {"status": status, "error": None}

    matched = [e for e in sync_errors if isinstance(e, dict) and e.get("source") == source]
    if matched:
        last = matched[-1]
        on_failure = last.get("on_failure", "warn")
        status = "skipped" if on_failure == "skip" else "failed"
        return {"status": status, "error": last.get("error")}

    return {"status": "no_url", "error": None}


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
    # per-source の status / error を保存（後で get_design_resolution が参照）
    state["design_per_source_status"] = {
        "figma": {"status": resolution.figma.status, "error": resolution.figma.error},
        "miro": {"status": resolution.miro.status, "error": resolution.miro.error},
    }
    if resolution.sync_errors:
        existing = state.get("design_sync_errors") or []
        existing.extend(resolution.sync_errors)
        state["design_sync_errors"] = existing
    # Figma コメントは「未解決」のもののみカウント。解決済みは無視する。
    figma_ctx = resolution.figma.context or {}
    unresolved_comments = [
        c for c in (figma_ctx.get("comments") or [])
        if isinstance(c, dict) and not c.get("resolved")
    ]
    state["design_review_required"] = bool(
        unresolved_comments or resolution.figma.status == "partial"
    )

    # on_failure: block 設定の場合、ワークフローを Waiting for Human に遷移させる
    if resolution.block:
        state["waiting_for_human"] = True
        existing_request = state.get("human_input_request") or ""
        block_msg = _format_block_message(resolution)
        if existing_request:
            state["human_input_request"] = f"{existing_request}\n\n{block_msg}"
        else:
            state["human_input_request"] = block_msg


def _format_block_message(resolution: DesignResolution) -> str:
    """on_failure: block 時に human_input_request に詰めるメッセージを組み立てる。"""
    parts = ["デザイン情報の取得に失敗したため、確認待ちで停止しました。"]
    for status in (resolution.figma, resolution.miro):
        if status.status == "failed":
            url = status.url or "(URL なし)"
            err = status.error or "(詳細なし)"
            parts.append(f"- {status.source}: {url} → {err}")
    parts.append("URL や API トークン、ファイル共有設定を確認してください。")
    return "\n".join(parts)
