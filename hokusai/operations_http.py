"""read-only HTTP admin（Step 3 第4スライス / roadmap-gbrain-inspirations.md §P1）

Operation Registry を **依存ゼロ（stdlib `http.server`）の read-only HTTP admin**
として外部公開する。CLI `operations run` と同じ実行経路（`resolve_read_only_
operation` → `invoke_operation`）をそのまま呼ぶことで、CLI / Dashboard /
HTTP admin が**同一 handler を単一経路で**叩く構成を完成させる（Step 3 の
締め。MCP は SDK 依存を足すため、まず依存ゼロの HTTP admin で要件を満たす）。

設計方針:
- リクエスト処理は純関数 `handle_operations_request()` に切り出し、決定的に
  テストする（`http.server` 部分は薄いラッパに留める）。
- **read-only のみ**: mutating operation は `ScopeViolationError` → 403。
- 既定 bind は `127.0.0.1`（ローカル admin 想定。外部公開しない）。認証は
  持たず、ネットワーク到達性で保護する前提（read-only ゆえ副作用なし）。

エンドポイント:
- `GET /operations` → 登録 operation 一覧（list と同じ stable schema）
- `GET /operations/<name>?key=value...` → operation を実行し結果を返す
  （query string が入力 params。未知=404 / scope 違反=403 / 入力不正=400）
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .operations import (
    OperationRegistry,
    ScopeViolationError,
    UnknownOperationError,
    invoke_operation,
    resolve_read_only_operation,
)

_OPERATIONS_PREFIX = "/operations"


def _query_to_params(query: str) -> dict[str, Any]:
    """query string を operation params dict に正規化する。

    `parse_qs` は `{key: [v1, v2, ...]}` を返すため、各 key は**最後の値**を採る
    （同名 key の重複は後勝ち。CLI `--param` の重複 reject とは別経路だが、HTTP
     では寛容に最後を採用）。値が空のリストは除外する。
    """
    out: dict[str, Any] = {}
    for key, values in parse_qs(query, keep_blank_values=True).items():
        if values:
            out[key] = values[-1]
    return out


def handle_operations_request(
    registry: OperationRegistry,
    method: str,
    path: str,
    params: dict[str, Any],
    *,
    config: Any,
) -> tuple[int, dict[str, Any]]:
    """read-only HTTP admin の 1 リクエストを処理する純関数。

    Args:
        registry: Operation Registry
        method: HTTP メソッド（"GET" 以外は 405）
        path: query を除いた path（例 "/operations/workflow.status"）
        params: 入力パラメータ（query string 由来の dict）
        config: store 解決用 config（`invoke_operation` に渡す）

    Returns:
        (status_code, body_dict) — body は JSON 直列化可能。エラーは
        `{"error": "..."}` 形。決定的・I/O は store 読取のみ（read-only）。
    """
    if method != "GET":
        return 405, {"error": f"method not allowed: {method}"}

    # 末尾スラッシュを正規化（"/operations/" は一覧扱い）。
    normalized = path.rstrip("/") or "/"

    if normalized == _OPERATIONS_PREFIX:
        ops = [
            {
                "name": op.name,
                "scope": op.scope,
                "summary": op.summary,
                "input_schema": op.input_schema,
            }
            for op in registry.list()
        ]
        return 200, {"operations": ops}

    prefix = _OPERATIONS_PREFIX + "/"
    if normalized.startswith(prefix):
        name = normalized[len(prefix):]
        if not name or "/" in name:
            return 404, {"error": f"not found: {path}"}
        try:
            op = resolve_read_only_operation(registry, name)
        except UnknownOperationError:
            return 404, {
                "error": f"unknown operation: {name}",
                "available": registry.names(),
            }
        except ScopeViolationError as e:
            return 403, {"error": str(e)}
        try:
            result = invoke_operation(op, params, config=config)
        except ValueError as e:
            return 400, {"error": str(e)}
        return 200, {"operation": name, "result": result}

    return 404, {"error": f"not found: {path}"}


def build_operations_http_handler(
    registry: OperationRegistry, config: Any
) -> type[BaseHTTPRequestHandler]:
    """`handle_operations_request` を呼ぶ薄い `BaseHTTPRequestHandler` を返す。

    registry / config を closure で束ね、`do_GET` は path/query の分解と
    JSON レスポンス書き出しのみを担う（ロジックは純関数側）。
    """

    class _Handler(BaseHTTPRequestHandler):
        # access log を stderr に垂れ流さない（CLI 出力を汚さない）。
        def log_message(self, *_args: Any) -> None:
            return

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(
                body, ensure_ascii=False, default=str
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 (http.server 規約)
            parts = urlsplit(self.path)
            params = _query_to_params(parts.query)
            status, body = handle_operations_request(
                registry, "GET", parts.path, params, config=config
            )
            self._respond(status, body)

    return _Handler


def serve_operations_http(
    registry: OperationRegistry,
    config: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> HTTPServer:
    """read-only HTTP admin サーバを構築して返す（呼び出し側で serve_forever）。

    既定 bind は `127.0.0.1`（ローカル admin）。テスト容易性のため起動
    （`serve_forever`）はせず、構築済み `HTTPServer` を返す。CLI 側が
    `serve_forever()` を呼ぶ。
    """
    handler_cls = build_operations_http_handler(registry, config)
    return HTTPServer((host, port), handler_cls)
