"""HOKUSAI Web Dashboard（Operations Console）の BASIC 認証テスト

Phase D-0: アクセス制限の DoD「管理者・開発者だけがアクセスできる」を検証。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.config import set_config
from hokusai.config.loaders import _parse_web_dashboard_config
from hokusai.config.models import (
    WebDashboardAuthConfig,
    WebDashboardConfig,
    WorkflowConfig,
)
from scripts.dashboard import (
    _basic_auth_required,
    _check_basic_auth,
)


# ---------------------------------------------------------------------------
# 設定パース
# ---------------------------------------------------------------------------


def test_parse_web_dashboard_default_when_missing():
    cfg = _parse_web_dashboard_config({})
    assert isinstance(cfg, WebDashboardConfig)
    assert cfg.auth.enabled is False
    assert cfg.auth.username_env == "HOKUSAI_OPS_USERNAME"


def test_parse_web_dashboard_full_config():
    cfg = _parse_web_dashboard_config({
        "web_dashboard": {
            "auth": {
                "enabled": True,
                "username_env": "MY_USER",
                "password_env": "MY_PASS",
                "realm": "Custom Realm",
            }
        }
    })
    assert cfg.auth.enabled is True
    assert cfg.auth.username_env == "MY_USER"
    assert cfg.auth.password_env == "MY_PASS"
    assert cfg.auth.realm == "Custom Realm"


def test_parse_web_dashboard_rejects_non_bool_enabled():
    cfg = _parse_web_dashboard_config({
        "web_dashboard": {"auth": {"enabled": "yes"}}
    })
    assert cfg.auth.enabled is False


def test_parse_web_dashboard_rejects_empty_env_name():
    cfg = _parse_web_dashboard_config({
        "web_dashboard": {"auth": {"username_env": ""}}
    })
    assert cfg.auth.username_env == "HOKUSAI_OPS_USERNAME"


# ---------------------------------------------------------------------------
# _basic_auth_required
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_disabled():
    set_config(WorkflowConfig(
        web_dashboard=WebDashboardConfig(auth=WebDashboardAuthConfig(enabled=False))
    ))


@pytest.fixture
def auth_enabled_with_creds(monkeypatch):
    monkeypatch.setenv("HOKUSAI_OPS_USERNAME", "alice")
    monkeypatch.setenv("HOKUSAI_OPS_PASSWORD", "secret123")
    set_config(WorkflowConfig(
        web_dashboard=WebDashboardConfig(auth=WebDashboardAuthConfig(enabled=True))
    ))


@pytest.fixture
def auth_enabled_no_creds(monkeypatch):
    monkeypatch.delenv("HOKUSAI_OPS_USERNAME", raising=False)
    monkeypatch.delenv("HOKUSAI_OPS_PASSWORD", raising=False)
    set_config(WorkflowConfig(
        web_dashboard=WebDashboardConfig(auth=WebDashboardAuthConfig(enabled=True))
    ))


def test_basic_auth_required_returns_disabled(auth_disabled):
    enabled, user, pw, realm = _basic_auth_required()
    assert enabled is False


def test_basic_auth_required_with_creds(auth_enabled_with_creds):
    enabled, user, pw, realm = _basic_auth_required()
    assert enabled is True
    assert user == "alice"
    assert pw == "secret123"


def test_basic_auth_required_no_creds_locks_down(auth_enabled_no_creds):
    """enabled=True で環境変数未設定 → ロックダウン状態（user / pw が None）"""
    enabled, user, pw, realm = _basic_auth_required()
    assert enabled is True
    assert user is None
    assert pw is None


# ---------------------------------------------------------------------------
# _check_basic_auth
# ---------------------------------------------------------------------------


class _FakeHandler:
    """SimpleHTTPRequestHandler の最小モック"""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}
        self.wfile = MagicMock()
        self.responses: list = []
        self._sent_headers: list = []

    def send_response(self, code):
        self.responses.append(code)

    def send_header(self, key, value):
        self._sent_headers.append((key, value))

    def end_headers(self):
        pass


def _build_auth_header(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def test_check_basic_auth_passes_when_disabled(auth_disabled):
    handler = _FakeHandler()
    assert _check_basic_auth(handler) is True
    assert handler.responses == []


def test_check_basic_auth_rejects_missing_header(auth_enabled_with_creds):
    handler = _FakeHandler()
    assert _check_basic_auth(handler) is False
    assert handler.responses == [401]
    # WWW-Authenticate ヘッダが付与される
    assert any(k == "WWW-Authenticate" for k, v in handler._sent_headers)


def test_check_basic_auth_rejects_wrong_password(auth_enabled_with_creds):
    handler = _FakeHandler({"Authorization": _build_auth_header("alice", "wrong")})
    assert _check_basic_auth(handler) is False
    assert handler.responses == [401]


def test_check_basic_auth_rejects_wrong_username(auth_enabled_with_creds):
    handler = _FakeHandler({"Authorization": _build_auth_header("bob", "secret123")})
    assert _check_basic_auth(handler) is False


def test_check_basic_auth_accepts_valid_credentials(auth_enabled_with_creds):
    handler = _FakeHandler({"Authorization": _build_auth_header("alice", "secret123")})
    assert _check_basic_auth(handler) is True
    assert handler.responses == []


def test_check_basic_auth_rejects_when_creds_unset(auth_enabled_no_creds):
    """enabled=True で環境変数未設定なら、どの認証情報でも拒否（ロックダウン）"""
    handler = _FakeHandler({"Authorization": _build_auth_header("anyone", "anything")})
    assert _check_basic_auth(handler) is False
    assert handler.responses == [401]


def test_check_basic_auth_rejects_malformed_header(auth_enabled_with_creds):
    handler = _FakeHandler({"Authorization": "Bearer some-token"})
    assert _check_basic_auth(handler) is False


def test_check_basic_auth_rejects_garbage_base64(auth_enabled_with_creds):
    handler = _FakeHandler({"Authorization": "Basic !!!!notbase64"})
    assert _check_basic_auth(handler) is False
