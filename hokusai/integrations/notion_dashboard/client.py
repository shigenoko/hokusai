"""Notion API HTTP クライアント

HOKUSAI 専用 Notion Integration の API token を使った直接 API クライアント。
標準ライブラリ urllib のみで実装し、依存追加なし。

責務:
- HTTPS リクエストの送信
- レートリミット（429）の検知とリトライ
- 一過性エラー（5xx）の指数バックオフ
- 認証エラー・ペイロードエラーの即時失敗
- API token の安全な扱い（ログ・例外メッセージに出さない）
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ...logging_config import get_logger

logger = get_logger("integrations.notion_dashboard.client")


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"


class NotionAPIError(Exception):
    """Notion API エラー（4xx 系のうち再送しても無駄なもの）"""

    def __init__(self, status: int, message: str, code: str = ""):
        self.status = status
        self.message = message
        self.code = code
        super().__init__(f"Notion API error {status}: {message}")


class NotionRateLimitError(Exception):
    """レートリミット超過。retry_after 秒待って再送する。"""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Notion rate limit exceeded; retry after {retry_after:.1f}s")


class NotionAPIClient:
    """Notion API への薄い HTTP クライアント。

    送信時は token をヘッダにのみ載せる。エラーメッセージや例外には token を含めない。
    """

    def __init__(
        self,
        api_token: str,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 5.0,
        requests_per_second: float = 2.0,
        timeout: float = 10.0,
    ):
        if not api_token:
            raise ValueError("Notion API token は空にできません")
        self._api_token = api_token
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = max(0.5, backoff_seconds)
        self._min_interval = 1.0 / max(0.1, requests_per_second)
        self._timeout = timeout
        self._last_request_at: float = 0.0

    def query_database(self, database_id: str, *, filter_: dict | None = None) -> dict:
        return self._request("POST", f"/databases/{database_id}/query", body={
            "filter": filter_,
        } if filter_ else None)

    def create_page(self, payload: dict) -> dict:
        return self._request("POST", "/pages", body=payload)

    def create_database(self, payload: dict) -> dict:
        """新規 Notion Database を作成する。初期セットアップで使用。"""
        return self._request("POST", "/databases", body=payload)

    def update_page(self, page_id: str, payload: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", body=payload)

    def retrieve_database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def retrieve_page(self, page_id: str) -> dict:
        """Notion ページのプロパティ等を取得する（GET /v1/pages/{page_id}）。"""
        return self._request("GET", f"/pages/{page_id}")

    def list_block_children(
        self, block_id: str, *, start_cursor: str | None = None
    ) -> dict:
        """指定ブロック直下の子ブロック一覧を取得する（GET /v1/blocks/{id}/children）。

        ページ ID も block_id として使える（Notion API の慣習）。
        Notion API は 1 レスポンスあたり最大 100 件を返し `has_more` /
        `next_cursor` でページネーションする。呼び出し側で全件走査が必要な場合は
        `start_cursor` を渡して反復取得すること。
        """
        path = f"/blocks/{block_id}/children"
        if start_cursor:
            # cursor は opaque な token のため、`&` `=` `#` 等の予約文字を含む
            # 可能性がある。string 連結で URL に埋め込むと truncation / 不正
            # URL になり pagination が壊れるため、必ず URL encode する。
            encoded_cursor = urllib.parse.quote(start_cursor, safe="")
            path = f"{path}?start_cursor={encoded_cursor}"
        return self._request("GET", path)

    def append_block_children(self, block_id: str, children: list[dict]) -> dict:
        return self._request(
            "PATCH", f"/blocks/{block_id}/children", body={"children": children}
        )

    def get_bot_info(self) -> dict:
        """Notion API GET /users/me を呼んで bot ユーザー情報を取得する。

        Operations Console の接続状態パネルで「どの integration に接続しているか」
        を識別するために使う。Notion API は workspace 名を直接返さないため、
        bot user name と integration の所有者情報（owner.workspace 等）で代用する。

        Returns:
            Notion API のレスポンスをそのまま返す:
            {"id": "...", "name": "...", "type": "bot", "bot": {"owner": {...}}}
        """
        return self._request("GET", "/users/me")

    def _request(self, method: str, path: str, *, body: dict | None = None) -> dict:
        url = f"{NOTION_API_BASE}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last_exception: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            self._enforce_rate_limit()
            try:
                return self._send(method, url, data)
            except NotionRateLimitError as e:
                last_exception = e
                wait = e.retry_after
                logger.warning(
                    f"Notion API rate limit (attempt {attempt}/{self._max_attempts}); "
                    f"sleeping {wait:.1f}s"
                )
                time.sleep(wait)
            except NotionAPIError as e:
                # 4xx は即時失敗（401/403/404 等は再送しても無駄）
                if 500 <= e.status < 600:
                    last_exception = e
                    backoff = self._backoff_seconds * attempt
                    logger.warning(
                        f"Notion API 5xx error (attempt {attempt}/{self._max_attempts}); "
                        f"sleeping {backoff:.1f}s"
                    )
                    time.sleep(backoff)
                else:
                    raise
            except (urllib.error.URLError, TimeoutError) as e:
                last_exception = e
                backoff = self._backoff_seconds * attempt
                logger.warning(
                    f"Notion API network error (attempt {attempt}/{self._max_attempts}): "
                    f"{type(e).__name__}; sleeping {backoff:.1f}s"
                )
                time.sleep(backoff)

        if last_exception is not None:
            raise last_exception
        raise RuntimeError("Notion API request failed without exception")

    def _enforce_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _send(self, method: str, url: str, data: bytes | None) -> dict:
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                err_body = e.read().decode("utf-8")
                err_data = json.loads(err_body) if err_body else {}
                message = err_data.get("message", "")
                code = err_data.get("code", "")
            except Exception:
                message = ""
                code = ""

            if status == 429:
                retry_after = self._parse_retry_after(e.headers)
                raise NotionRateLimitError(retry_after) from None

            # API token を含む可能性のある詳細はログに出さず、status と message のみ
            safe_message = message or code or "(no detail)"
            raise NotionAPIError(status, safe_message, code=str(code)) from None

    @staticmethod
    def _parse_retry_after(headers: Any) -> float:
        try:
            value = headers.get("Retry-After") if headers else None
            if value:
                return max(1.0, float(value))
        except (TypeError, ValueError):
            pass
        return 5.0
