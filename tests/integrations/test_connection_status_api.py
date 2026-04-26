"""
ダッシュボード `/api/connections` ルートのテスト

DashboardHandler のハンドラをモック上で直接呼び出し、レスポンスの形と
ステータスコードを検証する。実 HTTP サーバは起動しない。
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.dashboard import DashboardHandler  # noqa: E402

from hokusai.integrations import connection_status as cs  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    cs.clear_cache()
    yield
    cs.clear_cache()


def _make_handler() -> MagicMock:
    """test_dashboard.py の TestPromptAPI と同じパターンで作るハンドラのモック"""
    handler = MagicMock(spec=DashboardHandler)
    handler._send_json_response = DashboardHandler._send_json_response.__get__(handler)
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler._handle_connections_list = DashboardHandler._handle_connections_list.__get__(handler)
    handler._handle_connections_get = DashboardHandler._handle_connections_get.__get__(handler)
    return handler


def _parse(handler: MagicMock) -> dict:
    handler.wfile.seek(0)
    return json.loads(handler.wfile.read().decode("utf-8"))


def _stub_status(service_id: str, status: str = cs.STATUS_CONNECTED) -> dict:
    return cs._build_result(
        service_id=service_id,
        label=service_id,
        category=cs.CategoryLLMAgent,
        status=status,
        summary="stub",
        detail=None,
        required_for=[],
        message_key=f"connection.{service_id}.{status}",
    )


# ---------------------------------------------------------------------------
# /api/connections (一括取得)
# ---------------------------------------------------------------------------


def test_list_returns_full_registry(monkeypatch):
    monkeypatch.setattr(
        cs,
        "get_all_statuses",
        lambda *, refresh, mode: {
            "success": True,
            "checked_at": "2026-04-26T00:00:00+09:00",
            "mode": mode,
            "services": [_stub_status("claude")],
        },
    )

    handler = _make_handler()
    handler._handle_connections_list({})

    data = _parse(handler)
    assert data["success"] is True
    assert data["mode"] == "shallow"
    assert [s["id"] for s in data["services"]] == ["claude"]
    handler.send_response.assert_called_with(200)


def test_list_passes_refresh_and_mode(monkeypatch):
    received: dict = {}

    def _fake_get_all(*, refresh: bool, mode: str) -> dict:
        received["refresh"] = refresh
        received["mode"] = mode
        return {
            "success": True,
            "checked_at": "2026-04-26T00:00:00+09:00",
            "mode": mode,
            "services": [],
        }

    monkeypatch.setattr(cs, "get_all_statuses", _fake_get_all)

    handler = _make_handler()
    handler._handle_connections_list({"refresh": ["1"], "mode": ["deep"]})

    assert received == {"refresh": True, "mode": "deep"}
    data = _parse(handler)
    assert data["mode"] == "deep"


def test_list_rejects_unknown_mode(monkeypatch):
    """未知の mode は shallow にフォールバックする"""
    received: dict = {}

    def _fake_get_all(*, refresh: bool, mode: str) -> dict:
        received["mode"] = mode
        return {"success": True, "checked_at": "x", "mode": mode, "services": []}

    monkeypatch.setattr(cs, "get_all_statuses", _fake_get_all)

    handler = _make_handler()
    handler._handle_connections_list({"mode": ["bogus"]})

    assert received["mode"] == "shallow"


# ---------------------------------------------------------------------------
# /api/connections/{service}
# ---------------------------------------------------------------------------


def test_get_known_service(monkeypatch):
    monkeypatch.setattr(
        cs,
        "get_service_status",
        lambda service_id, *, refresh, mode: _stub_status(service_id),
    )

    handler = _make_handler()
    handler._handle_connections_get("gh", {})

    data = _parse(handler)
    assert data["success"] is True
    assert data["service"]["id"] == "gh"
    handler.send_response.assert_called_with(200)


def test_get_unknown_service_returns_404(monkeypatch):
    monkeypatch.setattr(
        cs, "get_service_status", lambda service_id, *, refresh, mode: None
    )

    handler = _make_handler()
    handler._handle_connections_get("does_not_exist", {})

    data = _parse(handler)
    assert data["success"] is False
    # 他 API（_handle_config_get 等）と揃えて errors: [...] 配列を使う
    assert isinstance(data["errors"], list)
    assert any("does_not_exist" in msg for msg in data["errors"])
    handler.send_response.assert_called_with(404)


def test_get_passes_refresh_and_mode(monkeypatch):
    received: dict = {}

    def _fake_get(service_id: str, *, refresh: bool, mode: str) -> dict:
        received["service_id"] = service_id
        received["refresh"] = refresh
        received["mode"] = mode
        return _stub_status(service_id)

    monkeypatch.setattr(cs, "get_service_status", _fake_get)

    handler = _make_handler()
    handler._handle_connections_get("notion_mcp", {"refresh": ["1"], "mode": ["deep"]})

    assert received == {"service_id": "notion_mcp", "refresh": True, "mode": "deep"}


def test_get_rejects_unknown_mode(monkeypatch):
    received: dict = {}

    def _fake_get(service_id: str, *, refresh: bool, mode: str) -> dict:
        received["mode"] = mode
        return _stub_status(service_id)

    monkeypatch.setattr(cs, "get_service_status", _fake_get)

    handler = _make_handler()
    handler._handle_connections_get("claude", {"mode": ["weird"]})

    assert received["mode"] == "shallow"


# ---------------------------------------------------------------------------
# Routing (do_GET dispatch)
# ---------------------------------------------------------------------------


def test_do_get_routes_collection(monkeypatch):
    """do_GET が /api/connections を _handle_connections_list へ振り分ける"""
    handler = _make_handler()
    handler.path = "/api/connections?refresh=1&mode=deep"
    handler._handle_connections_list = MagicMock()
    handler._handle_connections_get = MagicMock()
    handler.do_GET = DashboardHandler.do_GET.__get__(handler)

    handler.do_GET()

    handler._handle_connections_list.assert_called_once()
    args, _ = handler._handle_connections_list.call_args
    assert args[0]["refresh"] == ["1"]
    assert args[0]["mode"] == ["deep"]
    handler._handle_connections_get.assert_not_called()


def test_do_get_routes_individual_service():
    """do_GET が /api/connections/<service> を _handle_connections_get へ振り分ける"""
    handler = _make_handler()
    handler.path = "/api/connections/gh?refresh=1"
    handler._handle_connections_list = MagicMock()
    handler._handle_connections_get = MagicMock()
    handler.do_GET = DashboardHandler.do_GET.__get__(handler)

    handler.do_GET()

    handler._handle_connections_get.assert_called_once()
    args, _ = handler._handle_connections_get.call_args
    assert args[0] == "gh"
    assert args[1]["refresh"] == ["1"]
    handler._handle_connections_list.assert_not_called()


def test_do_get_url_decodes_service_id():
    """URL エンコードされた service id が unquote される"""
    handler = _make_handler()
    handler.path = "/api/connections/notion%5Fmcp"
    handler._handle_connections_list = MagicMock()
    handler._handle_connections_get = MagicMock()
    handler.do_GET = DashboardHandler.do_GET.__get__(handler)

    handler.do_GET()

    args, _ = handler._handle_connections_get.call_args
    assert args[0] == "notion_mcp"


def test_do_get_trailing_slash_routes_to_list():
    """`/api/connections/`（末尾スラッシュ）は 404 ではなく一覧へ正規化"""
    handler = _make_handler()
    handler.path = "/api/connections/"
    handler._handle_connections_list = MagicMock()
    handler._handle_connections_get = MagicMock()
    handler.do_GET = DashboardHandler.do_GET.__get__(handler)

    handler.do_GET()

    handler._handle_connections_list.assert_called_once()
    handler._handle_connections_get.assert_not_called()
