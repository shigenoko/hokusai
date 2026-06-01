"""read-only HTTP admin（Step 3 第4スライス）のテスト。

純関数 `handle_operations_request` のルーティング / ステータス分類を中心に
検証し、実 HTTP サーバ疎通（end-to-end / 404 / 非 GET 405）でも execute 経路が
HTTP 越しに同じく動くことを担保する。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.operations import (
    MUTATING,
    READ_ONLY,
    Operation,
    OperationRegistry,
    build_default_registry,
)
from hokusai.operations_http import (
    _query_to_params,
    handle_operations_request,
    serve_operations_http,
)


class _FakeConfig:
    """llm_gateway.enabled と database_path だけ持つ config ダブル。

    handler が store を使わない（echo / ping）か、使っても空 DB の安全既定値で
    足りるケースを検証する。store 未指定時 invoke_operation が
    ReadOnlyStore(":memory:") を構築する（read-only・存在しない表は安全既定）。
    """

    class _GW:
        enabled = False

    llm_gateway = _GW()
    database_path = ":memory:"


# --- _query_to_params ----------------------------------------------------


def test_query_to_params_basic():
    assert _query_to_params("a=1&b=2") == {"a": "1", "b": "2"}


def test_query_to_params_last_wins():
    # 同名 key は後勝ち（HTTP では寛容）
    assert _query_to_params("k=1&k=2") == {"k": "2"}


def test_query_to_params_empty():
    assert _query_to_params("") == {}


def test_query_to_params_blank_value():
    assert _query_to_params("k=") == {"k": ""}


# --- handle_operations_request: ルーティング / ステータス ----------------


def test_request_list_operations():
    reg = build_default_registry()
    status, body = handle_operations_request(
        reg, "GET", "/operations", {}, config=_FakeConfig()
    )
    assert status == 200
    names = {op["name"] for op in body["operations"]}
    assert "runtime.health" in names
    # stable schema
    for op in body["operations"]:
        assert set(op) == {"name", "scope", "summary", "input_schema"}


def test_request_method_not_allowed():
    reg = build_default_registry()
    status, body = handle_operations_request(
        reg, "POST", "/operations", {}, config=_FakeConfig()
    )
    assert status == 405
    assert "error" in body


def test_request_unknown_operation_404():
    reg = build_default_registry()
    status, body = handle_operations_request(
        reg, "GET", "/operations/no.such.op", {}, config=_FakeConfig()
    )
    assert status == 404
    assert "unknown operation" in body["error"]
    assert "available" in body


def test_request_scope_violation_403():
    reg = OperationRegistry()
    reg.register(Operation(
        name="danger.do", summary="", scope=MUTATING,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda params, *, store, config: {},
    ))
    status, body = handle_operations_request(
        reg, "GET", "/operations/danger.do", {}, config=_FakeConfig()
    )
    assert status == 403
    assert "error" in body


def test_request_handler_value_error_400():
    reg = build_default_registry()
    # workflow.status は workflow_id 必須 → 400
    status, body = handle_operations_request(
        reg, "GET", "/operations/workflow.status", {}, config=_FakeConfig()
    )
    assert status == 400
    assert "workflow_id" in body["error"]


def test_request_success_executes_read_only_op():
    reg = OperationRegistry()
    reg.register(Operation(
        name="echo.params", summary="", scope=READ_ONLY,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda params, *, store, config: {"got": params},
    ))
    status, body = handle_operations_request(
        reg, "GET", "/operations/echo.params", {"x": "1"},
        config=_FakeConfig(),
    )
    assert status == 200
    assert body["operation"] == "echo.params"
    assert body["result"] == {"got": {"x": "1"}}


def test_request_trailing_slash_is_list():
    reg = build_default_registry()
    status, body = handle_operations_request(
        reg, "GET", "/operations/", {}, config=_FakeConfig()
    )
    assert status == 200
    assert "operations" in body


def test_request_unknown_path_404():
    reg = build_default_registry()
    status, _ = handle_operations_request(
        reg, "GET", "/nope", {}, config=_FakeConfig()
    )
    assert status == 404


def test_request_nested_path_404():
    reg = build_default_registry()
    status, _ = handle_operations_request(
        reg, "GET", "/operations/a/b", {}, config=_FakeConfig()
    )
    assert status == 404


def test_request_error_bodies_do_not_reflect_request_data():
    """エラーボディに path / method / 未知 name の生文字列を反映しない
    （reflected data 経路を作らない。SonarCloud S5131 / PR #165）。"""
    reg = build_default_registry()
    # 未知 path: path 文字列を含めない
    _, b1 = handle_operations_request(
        reg, "GET", "/secret-path-xyz", {}, config=_FakeConfig()
    )
    assert "secret-path-xyz" not in json.dumps(b1)
    # 未知 operation: name を含めない（available は registry 由来で安全）
    _, b2 = handle_operations_request(
        reg, "GET", "/operations/evil<script>", {}, config=_FakeConfig()
    )
    assert "evil" not in json.dumps(b2)
    # 405: method を含めない
    _, b3 = handle_operations_request(
        reg, "DELETE", "/operations", {}, config=_FakeConfig()
    )
    assert b3 == {"error": "method not allowed"}


def test_request_400_redacts_request_values_in_message():
    """handler の ValueError が query param の生値を埋め込んでも、HTTP 層が
    params 由来の値を伏字化して reflected data を残さない（PR #165 Round 2）。"""
    reg = OperationRegistry()

    def _raise_with_raw(params, *, store, config):
        # _coerce_limit と同型: 生値をメッセージに埋め込む handler
        raise ValueError(f"limit は整数で指定してください: {params['limit']!r}")

    reg.register(Operation(
        name="bad.limit", summary="", scope=READ_ONLY,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=_raise_with_raw,
    ))
    secret = "evil<script>payload"
    status, body = handle_operations_request(
        reg, "GET", "/operations/bad.limit", {"limit": secret},
        config=_FakeConfig(),
    )
    assert status == 400
    # 生値（および repr 形）は body に残らない
    assert secret not in json.dumps(body)
    # 伏字化されつつ static 文言は保持
    assert "<redacted>" in body["error"]
    assert "limit は整数で指定してください" in body["error"]


def test_request_400_keeps_static_message_intact():
    """params 由来の値を含まない static な検証文言はそのまま返す。"""
    reg = build_default_registry()
    status, body = handle_operations_request(
        reg, "GET", "/operations/workflow.status", {}, config=_FakeConfig()
    )
    assert status == 400
    assert "workflow_id" in body["error"]
    assert "<redacted>" not in body["error"]


# --- 実 HTTP 疎通（execute 経路が HTTP 越しでも動く） --------------------


def test_http_server_end_to_end():
    reg = OperationRegistry()
    reg.register(Operation(
        name="ping", summary="", scope=READ_ONLY,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda params, *, store, config: {"pong": True, "p": params},
    ))
    server = serve_operations_http(
        reg, _FakeConfig(), host="127.0.0.1", port=0  # port=0 → 空きポート
    )
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/operations/ping?a=9", timeout=5
        ) as resp:
            assert resp.status == 200
            data = json.loads(resp.read())
        assert data["operation"] == "ping"
        assert data["result"] == {"pong": True, "p": {"a": "9"}}
        # list も疎通
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/operations", timeout=5
        ) as resp2:
            listing = json.loads(resp2.read())
        assert listing["operations"][0]["name"] == "ping"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_server_unknown_returns_404():
    reg = build_default_registry()
    server = serve_operations_http(
        reg, _FakeConfig(), host="127.0.0.1", port=0
    )
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/operations/no.such", timeout=5
            )
        assert ei.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_server_non_get_returns_405():
    """GET 以外は既定の 501 でなく契約通りの 405 を返す（Copilot Round 1）。"""
    reg = build_default_registry()
    server = serve_operations_http(
        reg, _FakeConfig(), host="127.0.0.1", port=0
    )
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/operations", method="DELETE"
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_server_head_returns_405():
    """HEAD も do_HEAD 未実装の既定 501 でなく契約通り 405（Copilot Round 2）。"""
    reg = build_default_registry()
    server = serve_operations_http(
        reg, _FakeConfig(), host="127.0.0.1", port=0
    )
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/operations", method="HEAD"
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
