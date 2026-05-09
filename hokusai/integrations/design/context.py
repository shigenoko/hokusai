"""
DesignContextResolver

Phase ノードから直接 Figma / Miro API を呼ばず、本クラス経由で取得する。
- URL 抽出
- 設定検証 / token 解決
- キャッシュ参照 → API 呼び出し
- 共通コンテキスト形式へ正規化
- on_failure ポリシーによる失敗時挙動の決定
- プロンプト差し込み用 Markdown の生成

このクラスは LangGraph state を直接いじらない（呼び出し側が結果を state に詰める）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from ...config.models import (
    FigmaIntegrationConfig,
    MiroIntegrationConfig,
    WorkflowConfig,
)
from ...logging_config import get_logger
from .cache import DesignCache
from .figma import FigmaAPIError, FigmaClient, FigmaRateLimitError
from .miro import MiroAPIError, MiroClient, MiroRateLimitError
from .url_parser import (
    ParsedFigmaUrl,
    ParsedMiroUrl,
    parse_figma_url,
    parse_miro_url,
)

logger = get_logger("integrations.design.context")


_FIGMA_URL_RE = re.compile(
    r"https?://(?:www\.)?figma\.com/(?:file|design|proto|board)/[A-Za-z0-9]+(?:/[^\s)\"']*)?",
    re.IGNORECASE,
)
_MIRO_URL_RE = re.compile(
    r"https?://(?:www\.)?miro\.com/(?:app/board|board)/[A-Za-z0-9_=\-]+(?:/[^\s)\"']*)?",
    re.IGNORECASE,
)


# ---------- 結果型 ----------


@dataclass
class ResolutionStatus:
    """1 つの URL に対する解決結果。"""

    source: str  # "figma" | "miro"
    status: str  # "ok" | "partial" | "skipped" | "failed" | "not_configured" | "no_url"
    url: str | None
    context: dict[str, Any] | None
    error: str | None = None


@dataclass
class DesignResolution:
    """Figma + Miro まとめての解決結果。"""

    figma: ResolutionStatus
    miro: ResolutionStatus
    integration_status: str  # "ok" | "partial" | "skipped" | "failed" | "not_configured" | "no_url"
    sync_errors: list[dict[str, Any]]
    block: bool  # on_failure=block で停止すべきか


# ---------- 抽出ヘルパ ----------


def extract_figma_urls(*texts: str | None) -> list[str]:
    found: list[str] = []
    for t in texts:
        if not isinstance(t, str):
            continue
        for m in _FIGMA_URL_RE.findall(t):
            if m not in found:
                found.append(m)
    return found


def extract_miro_urls(*texts: str | None) -> list[str]:
    found: list[str] = []
    for t in texts:
        if not isinstance(t, str):
            continue
        for m in _MIRO_URL_RE.findall(t):
            if m not in found:
                found.append(m)
    return found


# ---------- Resolver ----------


class DesignContextResolver:
    """state / task content から URL を取り、design context を返す。"""

    def __init__(
        self,
        config: WorkflowConfig | None = None,
        *,
        cache: DesignCache | None = None,
        figma_client: FigmaClient | None = None,
        miro_client: MiroClient | None = None,
    ):
        self._config = config
        self._cache = cache
        self._figma_client_override = figma_client
        self._miro_client_override = miro_client

    # ---- public ----

    def resolve(
        self,
        *,
        figma_url: str | None = None,
        miro_url: str | None = None,
        text_sources: list[str | None] | None = None,
    ) -> DesignResolution:
        """指定された URL（または text 内の URL）を解決する。

        text_sources は Notion タスク本文や state.task_description などを
        列挙する。explicit URL が無い場合のみ抽出を試みる。
        """
        cfg = self._config or self._load_config()

        explicit_figma = [figma_url] if figma_url else []
        explicit_miro = [miro_url] if miro_url else []
        if text_sources:
            if not explicit_figma:
                explicit_figma = extract_figma_urls(*text_sources)
            if not explicit_miro:
                explicit_miro = extract_miro_urls(*text_sources)

        sync_errors: list[dict[str, Any]] = []

        figma_status = self._resolve_figma(
            urls=explicit_figma, cfg=cfg.figma if cfg else FigmaIntegrationConfig(),
            sync_errors=sync_errors,
        )
        miro_status = self._resolve_miro(
            urls=explicit_miro, cfg=cfg.miro if cfg else MiroIntegrationConfig(),
            sync_errors=sync_errors,
        )

        integration_status = _aggregate_status(figma_status.status, miro_status.status)

        block = (
            (cfg.figma.on_failure == "block" and figma_status.status == "failed")
            or (cfg.miro.on_failure == "block" and miro_status.status == "failed")
        ) if cfg else False

        return DesignResolution(
            figma=figma_status,
            miro=miro_status,
            integration_status=integration_status,
            sync_errors=sync_errors,
            block=block,
        )

    @staticmethod
    def render_markdown(resolution: DesignResolution) -> str:
        """プロンプトに差し込む Markdown を生成。"""
        sections: list[str] = []

        for status in (resolution.miro, resolution.figma):
            if status.status in ("not_configured", "no_url", "skipped"):
                continue
            ctx = status.context or {}
            label = "Miro 業務フロー" if status.source == "miro" else "Figma UI 仕様"
            lines: list[str] = [f"### {label}"]
            if status.url:
                lines.append(f"- URL: {status.url}")
            if ctx.get("title"):
                lines.append(f"- タイトル: {ctx.get('title')}")
            if ctx.get("updated_at"):
                lines.append(f"- 更新: {ctx.get('updated_at')}")
            if ctx.get("summary"):
                lines.append(f"- 概要: {ctx.get('summary')}")
            screens = ctx.get("screens") or []
            if screens:
                lines.append("- 画面 / フレーム:")
                for s in screens[:8]:
                    name = s.get("name") or s.get("node_id") or "(unnamed)"
                    lines.append(f"  - {name}")
                    texts = s.get("texts") or []
                    if texts:
                        joined = " / ".join(texts[:5])
                        lines.append(f"    - text: {joined}")
                    components = s.get("components") or []
                    if components:
                        lines.append(f"    - components: {', '.join(components[:5])}")
                    notes = s.get("notes") or []
                    if notes:
                        joined = " / ".join(notes[:5])
                        lines.append(f"    - notes: {joined}")
            comments = ctx.get("comments") or []
            unresolved = [c for c in comments if not c.get("resolved")]
            if unresolved:
                lines.append(f"- 未解決コメント: {len(unresolved)} 件")
                for c in unresolved[:3]:
                    body = (c.get("body") or "").splitlines()[0][:120]
                    lines.append(f"  - {c.get('author', '')}: {body}")
            warnings = ctx.get("warnings") or []
            if warnings:
                lines.append("- 警告:")
                for w in warnings[:3]:
                    lines.append(f"  - {w}")
            if status.error:
                lines.append(f"- 取得エラー: {status.error}")
            sections.append("\n".join(lines))

        if not sections:
            return ""
        return "## 外部デザイン・業務フロー情報\n\n" + "\n\n".join(sections)

    # ---- 内部 ----

    def _load_config(self) -> WorkflowConfig:
        from ...config import get_config
        return get_config()

    def _get_cache(self) -> DesignCache:
        if self._cache is None:
            self._cache = DesignCache()
        return self._cache

    def _resolve_figma(
        self,
        *,
        urls: list[str],
        cfg: FigmaIntegrationConfig,
        sync_errors: list[dict[str, Any]],
    ) -> ResolutionStatus:
        if not cfg.enabled:
            return ResolutionStatus(source="figma", status="not_configured", url=None, context=None)
        if not urls:
            return ResolutionStatus(source="figma", status="no_url", url=None, context=None)

        url = urls[0]
        try:
            parsed = parse_figma_url(url)
        except ValueError as exc:
            logger.warning("figma url parse failed: %s", exc)
            return self._failed("figma", url, str(exc), cfg.on_failure, sync_errors)

        token = os.environ.get(cfg.api_token_env or "")
        if not token:
            err = f"環境変数 {cfg.api_token_env} が未設定です"
            return self._failed("figma", url, err, cfg.on_failure, sync_errors)

        cache = self._get_cache()
        cached = cache.get_figma(parsed.file_key, parsed.node_id)
        if cached is not None:
            return ResolutionStatus(
                source="figma", status="ok", url=url, context=cached
            )

        client = self._figma_client_override or FigmaClient(
            token,
            max_attempts=cfg.retry.max_attempts,
            backoff_seconds=cfg.retry.backoff_seconds,
            requests_per_second=cfg.rate_limit.requests_per_second,
            timeout=cfg.timeout,
        )

        try:
            if parsed.node_id:
                payload = client.get_file_nodes(parsed.file_key, [parsed.node_id])
                # `get_file_nodes` は `nodes[node_id].document` を含む。
                # 共通変換で扱いやすいよう、wrap して document として整える。
                nodes = payload.get("nodes") if isinstance(payload, dict) else {}
                node_entry = nodes.get(parsed.node_id) if isinstance(nodes, dict) else None
                document = (
                    node_entry.get("document") if isinstance(node_entry, dict) else None
                )
                file_payload = {
                    "name": payload.get("name"),
                    "lastModified": payload.get("lastModified"),
                    "document": document or {"id": parsed.node_id, "children": []},
                }
            else:
                file_payload = client.get_file(parsed.file_key, depth=2)
            comments = client.get_comments(parsed.file_key) if cfg.fetch_comments else []
            image_urls: dict[str, str] | None = None
            if cfg.export_images and parsed.node_id:
                image_urls = client.get_image_urls(parsed.file_key, [parsed.node_id])
        except (FigmaAPIError, FigmaRateLimitError) as exc:
            return self._failed("figma", url, str(exc), cfg.on_failure, sync_errors)
        except Exception as exc:
            logger.exception("figma fetch unexpected error")
            return self._failed("figma", url, f"{type(exc).__name__}: {exc}", cfg.on_failure, sync_errors)

        ctx = FigmaClient.to_common_context(
            url=url,
            file_key=parsed.file_key,
            node_id=parsed.node_id,
            file_payload=file_payload,
            comments=comments,
            image_urls=image_urls,
        )
        cache.put_figma(parsed.file_key, parsed.node_id, ctx, cfg.cache_ttl_seconds)

        status = "partial" if ctx.get("warnings") else "ok"
        return ResolutionStatus(source="figma", status=status, url=url, context=ctx)

    def _resolve_miro(
        self,
        *,
        urls: list[str],
        cfg: MiroIntegrationConfig,
        sync_errors: list[dict[str, Any]],
    ) -> ResolutionStatus:
        if not cfg.enabled:
            return ResolutionStatus(source="miro", status="not_configured", url=None, context=None)
        if not urls:
            return ResolutionStatus(source="miro", status="no_url", url=None, context=None)

        url = urls[0]
        try:
            parsed = parse_miro_url(url)
        except ValueError as exc:
            logger.warning("miro url parse failed: %s", exc)
            return self._failed("miro", url, str(exc), cfg.on_failure, sync_errors)

        token = os.environ.get(cfg.api_token_env or "")
        if not token:
            err = f"環境変数 {cfg.api_token_env} が未設定です"
            return self._failed("miro", url, err, cfg.on_failure, sync_errors)

        cache = self._get_cache()
        cached = cache.get_miro(parsed.board_id)
        if cached is not None:
            return ResolutionStatus(
                source="miro", status="ok", url=url, context=cached
            )

        client = self._miro_client_override or MiroClient(
            token,
            max_attempts=cfg.retry.max_attempts,
            backoff_seconds=cfg.retry.backoff_seconds,
            requests_per_second=cfg.rate_limit.requests_per_second,
            timeout=cfg.timeout,
        )

        try:
            board_payload = client.get_board(parsed.board_id)
            items = client.list_items(parsed.board_id)
        except (MiroAPIError, MiroRateLimitError) as exc:
            return self._failed("miro", url, str(exc), cfg.on_failure, sync_errors)
        except Exception as exc:
            logger.exception("miro fetch unexpected error")
            return self._failed("miro", url, f"{type(exc).__name__}: {exc}", cfg.on_failure, sync_errors)

        ctx = MiroClient.to_common_context(
            url=url,
            board_id=parsed.board_id,
            board_payload=board_payload,
            items=items,
        )
        cache.put_miro(parsed.board_id, ctx, cfg.cache_ttl_seconds)

        status = "partial" if ctx.get("warnings") else "ok"
        return ResolutionStatus(source="miro", status=status, url=url, context=ctx)

    def _failed(
        self,
        source: str,
        url: str,
        error: str,
        on_failure: str,
        sync_errors: list[dict[str, Any]],
    ) -> ResolutionStatus:
        sync_errors.append({
            "source": source,
            "url": url,
            "error": error,
            "on_failure": on_failure,
        })
        if on_failure == "skip":
            status = "skipped"
        else:
            status = "failed"
        return ResolutionStatus(
            source=source, status=status, url=url, context=None, error=error
        )


def _aggregate_status(figma: str, miro: str) -> str:
    """全体ステータスを 1 つに集約。"""
    if figma == "failed" or miro == "failed":
        return "failed"
    if "partial" in (figma, miro):
        return "partial"
    if figma == "ok" or miro == "ok":
        return "ok"
    if figma == "skipped" or miro == "skipped":
        return "skipped"
    if figma == "no_url" and miro == "no_url":
        return "no_url"
    return "not_configured"
