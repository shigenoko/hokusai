"""
Miro REST API クライアント（read-only）。

責務:
- board 情報の取得
- item 一覧の取得（frame / sticky_note / text / shape / connector）
- 共通コンテキスト形式への正規化

設計方針:
- 標準ライブラリ urllib のみで実装し、依存追加なし。
- token は Authorization: Bearer ヘッダにのみ載せる。
- レート制限と 5xx は指数バックオフで再送、4xx は即時失敗。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ...logging_config import get_logger

logger = get_logger("integrations.design.miro")


MIRO_API_BASE = "https://api.miro.com/v2"


class MiroAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Miro API error {status}: {message}")


class MiroRateLimitError(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Miro rate limit; retry after {retry_after:.1f}s")


_ITEM_TYPES = ("frame", "sticky_note", "text", "shape", "connector", "card")


class MiroClient:
    """Miro REST API への薄いクライアント。"""

    def __init__(
        self,
        api_token: str,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 5.0,
        requests_per_second: float = 1.5,
        timeout: float = 10.0,
    ):
        if not api_token:
            raise ValueError("Miro API token は空にできません")
        self._api_token = api_token
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = max(0.5, backoff_seconds)
        self._min_interval = 1.0 / max(0.1, requests_per_second)
        self._timeout = timeout
        self._last_request_at: float = 0.0

    # ---------- public API ----------

    def get_board(self, board_id: str) -> dict[str, Any]:
        return self._request("GET", f"/boards/{urllib.parse.quote(board_id, safe='')}")

    def list_items(
        self,
        board_id: str,
        *,
        max_pages: int = 5,
        limit_per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """全 item を最大 max_pages * limit_per_page まで取得。"""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            qs = f"limit={limit_per_page}"
            if cursor:
                qs = f"{qs}&cursor={urllib.parse.quote(cursor, safe='')}"
            data = self._request(
                "GET",
                f"/boards/{urllib.parse.quote(board_id, safe='')}/items?{qs}",
            )
            page = data.get("data") if isinstance(data, dict) else None
            if isinstance(page, list):
                items.extend(page)
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not cursor:
                break
        return items

    # ---------- 共通コンテキスト変換 ----------

    @staticmethod
    def to_common_context(
        *,
        url: str,
        board_id: str,
        board_payload: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        title = (
            board_payload.get("name") if isinstance(board_payload, dict) else None
        )
        updated_at = (
            board_payload.get("modifiedAt") if isinstance(board_payload, dict) else None
        )

        screens = _build_miro_screens(items, limit=12)

        warnings: list[str] = []
        if not items:
            warnings.append("Miro item を取得できませんでした")

        summary = _summarize_miro(title, items, screens)

        return {
            "source": "miro",
            "url": url,
            "title": title,
            "updated_at": updated_at,
            "summary": summary,
            "screens": screens,
            "comments": [],
            "warnings": warnings,
        }

    # ---------- 内部 ----------

    def create_card(
        self,
        board_id: str,
        *,
        title: str,
        description: str | None = None,
        position: dict[str, float] | None = None,
        style: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Miro board に card を作成する（Phase E, v0.4.0）。

        Miro REST API:
            POST /v2/boards/{board_id}/cards
            body: {"data": {"title": "...", "description": "..."},
                   "position": {"x": ..., "y": ...},
                   "style": {"fillColor": "..."}}

        Args:
            board_id: Miro board ID
            title: card タイトル
            description: card 本文（HTML 可）
            position: 配置位置（x, y）
            style: スタイル（fillColor 等）

        Returns:
            API レスポンス（"id" に card ID が入る）
        """
        if not board_id or not title:
            raise ValueError("board_id / title は必須")
        body: dict[str, Any] = {"data": {"title": title}}
        if description is not None:
            body["data"]["description"] = description
        if position is not None:
            body["position"] = position
        if style is not None:
            body["style"] = style
        path = f"/boards/{urllib.parse.quote(board_id, safe='')}/cards"
        return self._request("POST", path, body=body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{MIRO_API_BASE}{path}"
        last_exception: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._enforce_rate_limit()
            try:
                return self._send(method, url, body=body)
            except MiroRateLimitError as e:
                last_exception = e
                logger.warning(
                    "miro rate limit (attempt %d/%d); sleeping %.1fs",
                    attempt, self._max_attempts, e.retry_after,
                )
                time.sleep(e.retry_after)
            except MiroAPIError as e:
                if 500 <= e.status < 600:
                    last_exception = e
                    backoff = self._backoff_seconds * attempt
                    logger.warning(
                        "miro 5xx (attempt %d/%d); sleeping %.1fs",
                        attempt, self._max_attempts, backoff,
                    )
                    time.sleep(backoff)
                else:
                    raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_exception = e
                backoff = self._backoff_seconds * attempt
                logger.warning(
                    "miro network error (attempt %d/%d): %s; sleeping %.1fs",
                    attempt, self._max_attempts, type(e).__name__, backoff,
                )
                time.sleep(backoff)
        if last_exception:
            raise last_exception
        raise RuntimeError("Miro API request failed without exception")

    def _enforce_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _send(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Accept": "application/json",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url,
            method=method,
            headers=headers,
            data=data,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                body_text = response.read().decode("utf-8")
                return json.loads(body_text) if body_text else {}
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                err_body = e.read().decode("utf-8")
                err_data = json.loads(err_body) if err_body else {}
                message = err_data.get("message") or err_data.get("type") or ""
            except Exception:
                message = ""
            if status == 429:
                retry_after = _parse_retry_after(e.headers)
                raise MiroRateLimitError(retry_after) from None
            raise MiroAPIError(status, message or "(no detail)") from None


def _parse_retry_after(headers: Any) -> float:
    try:
        value = headers.get("Retry-After") if headers else None
        if value:
            return max(1.0, float(value))
    except (TypeError, ValueError):
        pass
    return 5.0


def _build_miro_screens(
    items: list[dict[str, Any]], *, limit: int = 12
) -> list[dict[str, Any]]:
    """frame を起点にスクリーンをまとめる。frame が無い場合は flat に並べる。"""
    frames = [it for it in items if isinstance(it, dict) and it.get("type") == "frame"]
    others = [it for it in items if isinstance(it, dict) and it.get("type") != "frame"]

    screens: list[dict[str, Any]] = []

    def _text_of(item: dict[str, Any]) -> str:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        for key in ("title", "content", "text"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return _strip_html(v)[:200]
        return ""

    if frames:
        # parent.id ベースで、各フレームに属するアイテムを振り分ける。
        # parent が無いアイテムは「フレーム外」として別 screen にまとめる。
        frame_ids = {f.get("id") for f in frames if isinstance(f, dict) and f.get("id")}
        by_frame: dict[str, list[dict[str, Any]]] = {fid: [] for fid in frame_ids if fid}
        unparented: list[dict[str, Any]] = []
        for it in others:
            parent = it.get("parent") if isinstance(it.get("parent"), dict) else {}
            parent_id = parent.get("id")
            if parent_id and parent_id in frame_ids:
                by_frame[parent_id].append(it)
            else:
                unparented.append(it)

        def _collect(items_for_screen: list[dict[str, Any]]) -> dict[str, list[str]]:
            texts: list[str] = []
            notes: list[str] = []
            components: list[str] = []
            for it in items_for_screen:
                if len(texts) >= 20 and len(notes) >= 10 and len(components) >= 10:
                    break
                t = it.get("type")
                txt = _text_of(it)
                if not txt:
                    continue
                if t == "sticky_note" and len(notes) < 10:
                    notes.append(txt)
                elif t == "text" and len(texts) < 20:
                    texts.append(txt)
                elif t in ("shape", "card") and len(components) < 10:
                    components.append(txt)
            return {"texts": texts, "notes": notes, "components": components}

        for frame in frames[:limit]:
            data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
            name = data.get("title") or frame.get("id") or ""
            buckets = _collect(by_frame.get(frame.get("id"), []))
            # Phase E (v0.4.0): writeback の card 配置位置計算のため geometry を保持
            geometry = frame.get("geometry") if isinstance(frame.get("geometry"), dict) else {}
            screens.append({
                "name": name,
                "node_id": frame.get("id", ""),
                "description": "",
                "x": geometry.get("x", 0),
                "y": geometry.get("y", 0),
                "width": geometry.get("width", 0),
                "height": geometry.get("height", 0),
                "texts": buckets["texts"],
                "components": buckets["components"],
                "notes": buckets["notes"],
            })

        # フレーム外アイテムが残っていれば、別 screen として最後に追加（容量に余裕がある場合）
        if unparented and len(screens) < limit:
            buckets = _collect(unparented)
            if buckets["texts"] or buckets["notes"] or buckets["components"]:
                screens.append({
                    "name": "(unparented items)",
                    "node_id": "",
                    "description": "",
                    "texts": buckets["texts"],
                    "components": buckets["components"],
                    "notes": buckets["notes"],
                })
        return screens

    # frame が無い board は items を 1 つの screen に圧縮
    flat_texts = []
    flat_notes = []
    flat_components = []
    for it in items[:60]:
        t = it.get("type") if isinstance(it, dict) else None
        txt = _text_of(it) if isinstance(it, dict) else ""
        if not txt:
            continue
        if t == "sticky_note" and len(flat_notes) < 20:
            flat_notes.append(txt)
        elif t == "text" and len(flat_texts) < 30:
            flat_texts.append(txt)
        elif t in ("shape", "card") and len(flat_components) < 20:
            flat_components.append(txt)
    if flat_texts or flat_notes or flat_components:
        screens.append({
            "name": "(no frames)",
            "node_id": "",
            "description": "",
            "texts": flat_texts,
            "components": flat_components,
            "notes": flat_notes,
        })
    return screens


def _strip_html(value: str) -> str:
    """Miro の text/sticky は HTML 断片で来ることがあるので簡易にタグ除去。"""
    import re

    return re.sub(r"<[^>]+>", "", value).strip()


def _summarize_miro(
    title: str | None,
    items: list[dict[str, Any]],
    screens: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if title:
        parts.append(f"board: {title}")
    parts.append(f"items: {len(items)}")
    parts.append(f"screens: {len(screens)}")
    return ", ".join(parts)
