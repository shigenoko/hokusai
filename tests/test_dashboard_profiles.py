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


@pytest.mark.parametrize("invalid_port", [0, -1, 65536, 100000])
def test_port_in_use_rejects_out_of_range(invalid_port):
    """port が 1..65535 範囲外なら ValueError（OverflowError / EINVAL を分かりやすく）"""
    from hokusai.dashboard import _port_in_use

    with pytest.raises(ValueError, match="1..65535"):
        _port_in_use(invalid_port)


@pytest.mark.parametrize("invalid_type", [True, False, "8765", 8765.0, None])
def test_port_in_use_rejects_non_int_types(invalid_type):
    """port が int 以外（bool / str / float / None）なら ValueError"""
    from hokusai.dashboard import _port_in_use

    with pytest.raises(ValueError, match="int"):
        _port_in_use(invalid_type)


def test_start_dashboard_resolves_none_port_to_default(tmp_path, monkeypatch):
    """port=None で start_dashboard を呼んだ場合、DEFAULT_DASHBOARD_PORT に解決される"""
    from hokusai.dashboard import DEFAULT_DASHBOARD_PORT, start_dashboard

    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
    )

    # _port_in_use と main をモックして、port が int で渡されることを検証
    captured_port = {}

    def fake_port_in_use(port, host="localhost"):
        captured_port["value"] = port
        return False

    monkeypatch.setattr("hokusai.dashboard._port_in_use", fake_port_in_use)

    import scripts.dashboard
    monkeypatch.setattr(scripts.dashboard, "main", lambda: None)

    start_dashboard(cfg, profile_name="test", port=None)

    # _port_in_use が DEFAULT_DASHBOARD_PORT で呼ばれることを確認
    assert captured_port["value"] == DEFAULT_DASHBOARD_PORT


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


def test_start_dashboard_converts_race_condition_eaddrinuse(tmp_path, monkeypatch):
    """_port_in_use チェック後の race condition で main() が EADDRINUSE を投げた場合、
    DashboardPortInUseError に変換されることを検証する。"""
    import errno as _errno

    from hokusai.dashboard import start_dashboard

    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
    )

    # _port_in_use は False を返す（初回チェック時は空いている状態）
    monkeypatch.setattr("hokusai.dashboard._port_in_use", lambda port, host="localhost": False)

    # scripts.dashboard.main() を呼んだら EADDRINUSE を投げる（race condition 再現）
    def fake_main():
        err = OSError("Address already in use (simulated race)")
        err.errno = _errno.EADDRINUSE
        raise err

    import scripts.dashboard
    monkeypatch.setattr(scripts.dashboard, "main", fake_main)

    with pytest.raises(DashboardPortInUseError, match="他プロセスが取得"):
        start_dashboard(cfg, profile_name="test", port=18888)


def test_cli_handle_dashboard_catches_eacces(tmp_path, monkeypatch, capsys):
    """_handle_dashboard が EACCES（特権ポート権限不足）でスタックトレースせず終了 1 を返す"""
    import errno as _errno

    from hokusai.cli_main import _handle_dashboard

    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
    )

    def fake_start(config, *, profile_name, port):
        err = OSError("permission denied (simulated EACCES)")
        err.errno = _errno.EACCES
        raise err

    monkeypatch.setattr("hokusai.cli_main.start_dashboard", fake_start, raising=False)
    # _handle_dashboard が import するため、hokusai.dashboard 側も差し替える
    import hokusai.dashboard
    monkeypatch.setattr(hokusai.dashboard, "start_dashboard", fake_start)

    class _Args:
        port = 80
        profile = None

    rc = _handle_dashboard(_Args(), cfg)
    captured = capsys.readouterr()
    assert rc == 1
    assert "権限" in captured.out
    assert "80" in captured.out


def test_cli_handle_dashboard_resolves_none_port_in_error_messages(tmp_path, monkeypatch, capsys):
    """args.port が None でも、エラーメッセージに DEFAULT_DASHBOARD_PORT が表示される"""
    import errno as _errno

    from hokusai.cli_main import _handle_dashboard
    from hokusai.dashboard import DEFAULT_DASHBOARD_PORT

    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
    )

    def fake_start(config, *, profile_name, port):
        # port は None ではなく実効値が来ているはず
        assert port == DEFAULT_DASHBOARD_PORT
        err = OSError("EACCES simulated")
        err.errno = _errno.EACCES
        raise err

    import hokusai.dashboard
    monkeypatch.setattr(hokusai.dashboard, "start_dashboard", fake_start)

    class _Args:
        port = None
        profile = None

    rc = _handle_dashboard(_Args(), cfg)
    captured = capsys.readouterr()
    assert rc == 1
    # メッセージに "None" は含まれず、実効 port (8765) が含まれる
    assert "None" not in captured.out
    assert str(DEFAULT_DASHBOARD_PORT) in captured.out


def test_cli_handle_dashboard_catches_value_error(tmp_path, monkeypatch, capsys):
    """_port_in_use の range バリデーション ValueError も親切に終了 1"""
    from hokusai.cli_main import _handle_dashboard

    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
    )

    def fake_start(config, *, profile_name, port):
        raise ValueError(f"port は 1..65535 の範囲である必要があります: got {port}")

    import hokusai.dashboard
    monkeypatch.setattr(hokusai.dashboard, "start_dashboard", fake_start)

    class _Args:
        port = 99999
        profile = None

    rc = _handle_dashboard(_Args(), cfg)
    captured = capsys.readouterr()
    assert rc == 1
    assert "範囲" in captured.out


def test_cli_handle_dashboard_catches_unknown_oserror(tmp_path, monkeypatch, capsys):
    """その他の OSError も親切なメッセージで終了 1 にする"""
    import errno as _errno

    from hokusai.cli_main import _handle_dashboard

    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
    )

    def fake_start(config, *, profile_name, port):
        err = OSError("address not available (simulated)")
        err.errno = _errno.EADDRNOTAVAIL
        raise err

    import hokusai.dashboard
    monkeypatch.setattr(hokusai.dashboard, "start_dashboard", fake_start)

    class _Args:
        port = 8765
        profile = None

    rc = _handle_dashboard(_Args(), cfg)
    captured = capsys.readouterr()
    assert rc == 1
    assert "8765" in captured.out


def test_start_dashboard_propagates_unrelated_oserror(tmp_path, monkeypatch):
    """main() の EADDRINUSE 以外の OSError はそのまま伝搬する"""
    import errno as _errno

    from hokusai.dashboard import start_dashboard

    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
    )

    monkeypatch.setattr("hokusai.dashboard._port_in_use", lambda port, host="localhost": False)

    def fake_main():
        err = OSError("disk full (simulated)")
        err.errno = _errno.ENOSPC
        raise err

    import scripts.dashboard
    monkeypatch.setattr(scripts.dashboard, "main", fake_main)

    with pytest.raises(OSError, match="disk full"):
        start_dashboard(cfg, profile_name="test", port=18888)


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
