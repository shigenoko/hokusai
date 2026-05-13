"""
Figma REST API クライアント（v0.4.0 から書き戻し対応）。

提供 API:
- 読み取り: get_file / get_file_nodes / get_comments / get_image_urls / to_common_context
- 書き戻し: post_comment（v0.4.0, Phase E）

設計方針:
- 標準ライブラリ urllib のみで実装し、依存追加なし。
- token はヘッダにのみ載せる。例外メッセージやログには出さない。
- レスポンスは要約済み共通形式に正規化してから返す。
- レート制限と 5xx は指数バックオフで再送、4xx は即時失敗。
- 画像 export 失敗は致命でない（partial として扱う）。
- 書き戻し（post_comment）は MVP 範囲外で、Phase E (v0.4.0) から有効。
  詳細: docs/hokusai-figma-miro-writeback-implementation-plan.md
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ...logging_config import get_logger

logger = get_logger("integrations.design.figma")


FIGMA_API_BASE = "https://api.figma.com/v1"


class FigmaAPIError(Exception):
    """Figma API エラー（再送しても無駄な 4xx 系）"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Figma API error {status}: {message}")


class FigmaRateLimitError(Exception):
    """Figma API のレートリミット超過。"""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Figma rate limit; retry after {retry_after:.1f}s")


class FigmaClient:
    """Figma REST API への薄いクライアント。"""

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
            raise ValueError("Figma API token は空にできません")
        self._api_token = api_token
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = max(0.5, backoff_seconds)
        self._min_interval = 1.0 / max(0.1, requests_per_second)
        self._timeout = timeout
        self._last_request_at: float = 0.0

    # ---------- public API ----------

    def get_file(self, file_key: str, *, depth: int | None = 2) -> dict[str, Any]:
        """ファイル全体（または指定深さ）の構造を取得。"""
        path = f"/files/{file_key}"
        if depth is not None:
            path = f"{path}?depth={depth}"
        return self._request("GET", path)

    def get_file_nodes(self, file_key: str, node_ids: list[str]) -> dict[str, Any]:
        """指定 node のサブツリーのみ取得。"""
        if not node_ids:
            return {"nodes": {}}
        ids_param = ",".join(urllib.parse.quote(nid, safe="") for nid in node_ids)
        return self._request("GET", f"/files/{file_key}/nodes?ids={ids_param}")

    def get_comments(self, file_key: str) -> list[dict[str, Any]]:
        try:
            data = self._request("GET", f"/files/{file_key}/comments")
        except FigmaAPIError as exc:
            # コメントは画面操作ベースで権限が分かれることがあるため、
            # 取れなかった場合は空配列で返して partial 扱いを上位に任せる。
            logger.warning("figma comments fetch failed: status=%s", exc.status)
            return []
        return data.get("comments", []) if isinstance(data, dict) else []

    def post_comment(
        self,
        file_key: str,
        *,
        message: str,
        node_id: str,
        node_offset: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Figma frame に pin コメントを投稿する（Phase E, v0.4.0）。

        Figma REST API:
            POST /v1/files/{file_key}/comments
            body: {"message": "...", "client_meta": {"node_id": "...", "node_offset": {...}}}

        詳細: https://developers.figma.com/docs/rest-api/comments-endpoints/

        Args:
            file_key: Figma file key
            message: コメント本文
            node_id: pin する frame / node の ID
            node_offset: pin 位置（既定 {"x": 0, "y": 0}、frame 左上）

        Returns:
            API レスポンス（"id" にコメント ID が入る）

        Raises:
            FigmaAPIError: 4xx エラー（権限不足、frame 不在等）
            FigmaRateLimitError: 429
        """
        if not file_key or not node_id or not message:
            raise ValueError("file_key / node_id / message は必須")
        offset = node_offset or {"x": 0, "y": 0}
        body = {
            "message": message,
            "client_meta": {
                "node_id": node_id,
                "node_offset": offset,
            },
        }
        return self._request("POST", f"/files/{file_key}/comments", body=body)

    def get_image_urls(
        self, file_key: str, node_ids: list[str], *, fmt: str = "png", scale: float = 1.0
    ) -> dict[str, str]:
        """指定 node の画像 export URL を取得。失敗時は空辞書。"""
        if not node_ids:
            return {}
        try:
            ids_param = ",".join(urllib.parse.quote(nid, safe="") for nid in node_ids)
            data = self._request(
                "GET",
                f"/images/{file_key}?ids={ids_param}&format={fmt}&scale={scale}",
            )
        except FigmaAPIError as exc:
            logger.warning("figma image export failed: status=%s", exc.status)
            return {}
        images = data.get("images") if isinstance(data, dict) else None
        return images or {}

    # ---------- 共通コンテキスト変換 ----------

    @staticmethod
    def to_common_context(
        *,
        url: str,
        file_key: str,
        node_id: str | None,
        file_payload: dict[str, Any],
        comments: list[dict[str, Any]],
        image_urls: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Figma の生レスポンスを HOKUSAI 共通コンテキスト形式に変換する。

        - screens は file_payload.document を BFS で辿り、最大 12 件まで抽出
        - texts/components/notes はそれぞれ最大 20 / 10 / 10 件で打ち切り
        - comments は author/body/resolved のみ抽出
        """
        title = file_payload.get("name") if isinstance(file_payload, dict) else None
        updated_at = (
            file_payload.get("lastModified") if isinstance(file_payload, dict) else None
        )

        screens = _extract_figma_screens(file_payload, root_node_id=node_id, limit=12)
        normalized_comments = []
        for c in comments[:50]:
            if not isinstance(c, dict):
                continue
            user = c.get("user") if isinstance(c.get("user"), dict) else {}
            normalized_comments.append({
                "author": user.get("handle") or user.get("name") or "",
                "body": (c.get("message") or "")[:500],
                "resolved": bool(c.get("resolved_at")),
            })

        warnings: list[str] = []
        if not screens:
            warnings.append("Figma のスクリーン情報を抽出できませんでした")
        if image_urls is not None and node_id and node_id not in image_urls:
            warnings.append("指定 node の画像 export に失敗しました")

        summary = _summarize_figma(title, screens, normalized_comments)

        return {
            "source": "figma",
            "url": url,
            "title": title,
            "updated_at": updated_at,
            "summary": summary,
            "screens": screens,
            "comments": normalized_comments,
            "warnings": warnings,
        }

    # ---------- 内部 ----------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{FIGMA_API_BASE}{path}"
        last_exception: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._enforce_rate_limit()
            try:
                return self._send(method, url, body=body)
            except FigmaRateLimitError as e:
                last_exception = e
                logger.warning(
                    "figma rate limit (attempt %d/%d); sleeping %.1fs",
                    attempt, self._max_attempts, e.retry_after,
                )
                time.sleep(e.retry_after)
            except FigmaAPIError as e:
                if 500 <= e.status < 600:
                    last_exception = e
                    backoff = self._backoff_seconds * attempt
                    logger.warning(
                        "figma 5xx (attempt %d/%d); sleeping %.1fs",
                        attempt, self._max_attempts, backoff,
                    )
                    time.sleep(backoff)
                else:
                    raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_exception = e
                backoff = self._backoff_seconds * attempt
                logger.warning(
                    "figma network error (attempt %d/%d): %s; sleeping %.1fs",
                    attempt, self._max_attempts, type(e).__name__, backoff,
                )
                time.sleep(backoff)
        if last_exception:
            raise last_exception
        raise RuntimeError("Figma API request failed without exception")

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
        headers = {"X-Figma-Token": self._api_token}
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
                message = err_data.get("err") or err_data.get("message") or ""
            except Exception:
                message = ""
            if status == 429:
                retry_after = _parse_retry_after(e.headers)
                raise FigmaRateLimitError(retry_after) from None
            raise FigmaAPIError(status, message or "(no detail)") from None


def _parse_retry_after(headers: Any) -> float:
    try:
        value = headers.get("Retry-After") if headers else None
        if value:
            return max(1.0, float(value))
    except (TypeError, ValueError):
        pass
    return 5.0


def _extract_figma_screens(
    file_payload: dict[str, Any],
    *,
    root_node_id: str | None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """document を BFS で辿り frame ノードを抽出。

    root_node_id 指定時は document 内でその node を見つけて起点にする。
    """
    if not isinstance(file_payload, dict):
        return []

    document = file_payload.get("document")
    if not isinstance(document, dict):
        return []

    start = document
    if root_node_id:
        found = _find_node_by_id(document, root_node_id)
        if found:
            start = found

    # BFS は deque + popleft で O(n)。list.pop(0) だと各取り出しが O(n) になり、
    # ノード数が増えると visited 上限以内でも体感遅延が出る。
    from collections import deque

    screens: list[dict[str, Any]] = []
    queue: deque[dict[str, Any]] = deque([start])
    visited = 0
    while queue and len(screens) < limit:
        node = queue.popleft()
        visited += 1
        if visited > 2000:
            break
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type in ("FRAME", "COMPONENT", "COMPONENT_SET", "INSTANCE", "SECTION"):
            screens.append(_screen_from_node(node))
        children = node.get("children")
        if isinstance(children, list):
            queue.extend(children)
    return screens


def _find_node_by_id(node: dict[str, Any], target: str) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    if node.get("id") == target:
        return node
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            found = _find_node_by_id(child, target)
            if found is not None:
                return found
    return None


def _screen_from_node(node: dict[str, Any]) -> dict[str, Any]:
    name = node.get("name", "")
    node_id = node.get("id", "")
    texts: list[str] = []
    components: list[str] = []
    notes: list[str] = []

    def _walk(n: Any) -> None:
        if not isinstance(n, dict):
            return
        if len(texts) >= 20 and len(components) >= 10:
            return
        ntype = n.get("type")
        if ntype == "TEXT":
            chars = n.get("characters")
            if isinstance(chars, str) and chars.strip() and len(texts) < 20:
                texts.append(chars.strip()[:200])
        elif ntype in ("INSTANCE", "COMPONENT") and len(components) < 10:
            cname = n.get("name")
            if isinstance(cname, str) and cname:
                components.append(cname)
        children = n.get("children")
        if isinstance(children, list):
            for c in children:
                _walk(c)

    _walk(node)

    return {
        "name": name,
        "node_id": node_id,
        "description": "",
        "texts": texts,
        "components": components,
        "notes": notes,
    }


def _summarize_figma(
    title: str | None,
    screens: list[dict[str, Any]],
    comments: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if title:
        parts.append(f"file: {title}")
    parts.append(f"screens: {len(screens)}")
    unresolved = sum(1 for c in comments if not c.get("resolved"))
    if comments:
        parts.append(f"comments: {len(comments)} (unresolved: {unresolved})")
    return ", ".join(parts)
