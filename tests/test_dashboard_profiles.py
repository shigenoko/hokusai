"""Dashboard CLI / profile 統合のテスト（Phase D）

対象:
- hokusai/dashboard/__init__.py の prepare_dashboard_env / _port_in_use
- scripts/dashboard.py の env var による PORT / DB_PATH / profile name オーバーライド
- `hokusai dashboard --profile <name>` の port 解決ロジック
"""

from __future__ import annotations

import importlib
import os
import socket
from pathlib import Path

import pytest

from hokusai.config.models import WorkflowConfig
from hokusai.dashboard import (
    DashboardPortInUseError,
    prepare_dashboard_env,
)

# ---------------------------------------------------------------------------
# prepare_dashboard_env
# ---------------------------------------------------------------------------


def test_prepare_dashboard_env_includes_paths(tmp_path):
    db = tmp_path / "wf.db"
    cp = tmp_path / "cp.db"
    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=db,
        checkpoint_db_path=cp,
    )
    env = prepare_dashboard_env(cfg, profile_name="a-co", port=8766)
    assert env["HOKUSAI_DASHBOARD_PORT"] == "8766"
    assert env["HOKUSAI_DASHBOARD_DB_PATH"] == str(db)
    assert env["HOKUSAI_DASHBOARD_CHECKPOINT_DB_PATH"] == str(cp)
    assert env["HOKUSAI_DASHBOARD_PROFILE"] == "a-co"


def test_prepare_dashboard_env_omits_when_not_set(tmp_path):
    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
    )
    env = prepare_dashboard_env(cfg)
    assert "HOKUSAI_DASHBOARD_PORT" not in env
    assert "HOKUSAI_DASHBOARD_PROFILE" not in env


def test_prepare_dashboard_env_expands_user(tmp_path):
    """database_path に Path オブジェクトを渡しても文字列化される"""
    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=Path("~/test-wf.db"),
        checkpoint_db_path=Path("~/test-cp.db"),
    )
    env = prepare_dashboard_env(cfg, port=8765)
    assert "~" not in env["HOKUSAI_DASHBOARD_DB_PATH"]
    assert "~" not in env["HOKUSAI_DASHBOARD_CHECKPOINT_DB_PATH"]


# ---------------------------------------------------------------------------
# port 衝突検出
# ---------------------------------------------------------------------------


def test_port_in_use_detects_listening_port():
    """実際に listen している port を検出"""
    from hokusai.dashboard import _port_in_use

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    try:
        assert _port_in_use(port) is True
    finally:
        sock.close()


def test_port_in_use_free_port_returns_false():
    """free な port は使用中ではない"""
    from hokusai.dashboard import _port_in_use

    # OS に未使用 port を選ばせる
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", 0))
    port = sock.getsockname()[1]
    sock.close()
    # close 直後の port は通常 free
    assert _port_in_use(port) is False


def test_port_in_use_reraises_non_eaddrinuse_errors(monkeypatch):
    """EADDRINUSE 以外の OSError（例: EACCES）は「使用中」と誤判定せず再 raise"""
    import errno

    from hokusai.dashboard import _port_in_use

    class _FakeSocket:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, _value):
            pass

        def bind(self, _addr):
            err = OSError("permission denied (simulated EACCES)")
            err.errno = errno.EACCES
            raise err

    monkeypatch.setattr("hokusai.dashboard.socket.socket", _FakeSocket)

    with pytest.raises(OSError) as exc_info:
        _port_in_use(80)  # 特権ポートを bind しようとした場面を再現
    assert exc_info.value.errno == errno.EACCES


def test_start_dashboard_raises_on_port_conflict(tmp_path):
    """start_dashboard は port 衝突時に DashboardPortInUseError"""
    from hokusai.dashboard import start_dashboard

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", 0))
    busy_port = sock.getsockname()[1]
    sock.listen(1)
    try:
        cfg = WorkflowConfig(
            data_dir=tmp_path,
            database_path=tmp_path / "wf.db",
            checkpoint_db_path=tmp_path / "cp.db",
        )
        with pytest.raises(DashboardPortInUseError, match=str(busy_port)):
            start_dashboard(cfg, profile_name="test", port=busy_port)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# scripts/dashboard.py の env override
# ---------------------------------------------------------------------------


def test_dashboard_module_reads_port_from_env(monkeypatch):
    """HOKUSAI_DASHBOARD_PORT が scripts/dashboard.py の PORT に反映される"""
    monkeypatch.setenv("HOKUSAI_DASHBOARD_PORT", "9999")

    # 既に import 済みの場合は reload してモジュールレベル定義を再評価
    import scripts.dashboard as dashboard_module
    importlib.reload(dashboard_module)

    assert dashboard_module.PORT == 9999


def test_dashboard_module_reads_db_path_from_env(monkeypatch, tmp_path):
    custom_db = tmp_path / "custom-wf.db"
    monkeypatch.setenv("HOKUSAI_DASHBOARD_DB_PATH", str(custom_db))

    import scripts.dashboard as dashboard_module
    importlib.reload(dashboard_module)

    assert dashboard_module.DB_PATH == custom_db


def test_dashboard_module_invalid_port_falls_back_to_default(monkeypatch, capsys):
    monkeypatch.setenv("HOKUSAI_DASHBOARD_PORT", "not-a-number")

    import scripts.dashboard as dashboard_module
    importlib.reload(dashboard_module)

    assert dashboard_module.PORT == 8765
    # warning が出ている
    captured = capsys.readouterr()
    assert "8765" in captured.out or "warning" in captured.out.lower()


def test_dashboard_module_default_port_without_env(monkeypatch):
    """env 未設定なら従来通り 8765"""
    monkeypatch.delenv("HOKUSAI_DASHBOARD_PORT", raising=False)

    import scripts.dashboard as dashboard_module
    importlib.reload(dashboard_module)

    assert dashboard_module.PORT == 8765


def test_dashboard_module_profile_badge_with_env(monkeypatch):
    """HOKUSAI_DASHBOARD_PROFILE が設定されていればバッジ HTML を返す"""
    monkeypatch.setenv("HOKUSAI_DASHBOARD_PROFILE", "company-a")

    import scripts.dashboard as dashboard_module
    importlib.reload(dashboard_module)

    badge = dashboard_module._render_profile_badge()
    assert "company-a" in badge
    assert "Profile:" in badge


def test_dashboard_module_profile_badge_without_env(monkeypatch):
    """profile 未設定なら空文字列（既存レンダリングを壊さない）"""
    monkeypatch.delenv("HOKUSAI_DASHBOARD_PROFILE", raising=False)

    import scripts.dashboard as dashboard_module
    importlib.reload(dashboard_module)

    badge = dashboard_module._render_profile_badge()
    assert badge == ""


def test_dashboard_module_profile_badge_escapes_html(monkeypatch):
    """profile 名に特殊文字が含まれてもエスケープされる"""
    monkeypatch.setenv("HOKUSAI_DASHBOARD_PROFILE", "<script>alert(1)</script>")

    import scripts.dashboard as dashboard_module
    importlib.reload(dashboard_module)

    badge = dashboard_module._render_profile_badge()
    assert "<script>" not in badge
    assert "&lt;script&gt;" in badge


# ---------------------------------------------------------------------------
# 後始末: モジュール状態を default に戻す
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dashboard_module():
    """各テストの最後に scripts.dashboard を default env で reload し、
    他テストへの汚染を防ぐ。"""
    yield
    for env_key in (
        "HOKUSAI_DASHBOARD_PORT",
        "HOKUSAI_DASHBOARD_DB_PATH",
        "HOKUSAI_DASHBOARD_CHECKPOINT_DB_PATH",
        "HOKUSAI_DASHBOARD_PROFILE",
    ):
        os.environ.pop(env_key, None)
    import scripts.dashboard as dashboard_module
    importlib.reload(dashboard_module)
