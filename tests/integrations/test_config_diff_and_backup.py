"""
Phase B-1 (差分プレビュー) と Phase B-2 (.bak 復元) のテスト

- compute_config_diff: 差分計算ヘルパー
- get_config_backup_info: .bak メタ情報
- restore_config_backup: .bak から本体への復元
- API ハンドラ: /api/config/diff (POST), /api/config/backup (GET),
  /api/config/backup/restore (POST)

実ファイル I/O が絡むため `tmp_path` と `monkeypatch` で `CONFIGS_DIR` を
差し替えて検証する。
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts import dashboard as dash  # noqa: E402
from scripts.dashboard import (  # noqa: E402
    DashboardHandler,
    _safe_config_path,
    compute_config_diff,
    get_config_backup_info,
    restore_config_backup,
    save_config_yaml,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def configs_dir(tmp_path: Path, monkeypatch) -> Path:
    """`CONFIGS_DIR` を一時ディレクトリに差し替える"""
    monkeypatch.setattr(dash, "CONFIGS_DIR", tmp_path)
    return tmp_path


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _make_handler() -> MagicMock:
    handler = MagicMock(spec=DashboardHandler)
    handler._send_json_response = DashboardHandler._send_json_response.__get__(handler)
    handler._read_json_body = MagicMock()
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def _parse(handler: MagicMock) -> dict:
    handler.wfile.seek(0)
    return json.loads(handler.wfile.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# compute_config_diff
# ---------------------------------------------------------------------------


def test_diff_returns_no_changes_when_data_matches_file(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "project_root: /tmp\nbase_branch: main\n")
    result = compute_config_diff("demo", {"project_root": "/tmp", "base_branch": "main"})
    assert result["has_changes"] is False
    assert result["is_new_file"] is False
    assert result["lines_added"] == 0
    assert result["lines_removed"] == 0


def test_diff_detects_modified_value(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "project_root: /tmp\nbase_branch: main\n")
    result = compute_config_diff(
        "demo", {"project_root": "/tmp", "base_branch": "develop"}
    )
    assert result["has_changes"] is True
    assert result["is_new_file"] is False
    assert result["lines_added"] >= 1
    assert result["lines_removed"] >= 1
    assert "develop" in result["diff"]


def test_diff_detects_added_field(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "project_root: /tmp\n")
    result = compute_config_diff(
        "demo", {"project_root": "/tmp", "base_branch": "main"}
    )
    assert result["has_changes"] is True
    assert result["lines_added"] >= 1
    assert "base_branch" in result["diff"]


def test_diff_for_new_file(configs_dir):
    """ファイルが存在しない場合は全行が追加扱い"""
    result = compute_config_diff("brand-new", {"project_root": "/tmp"})
    assert result["is_new_file"] is True
    assert result["has_changes"] is True
    assert result["lines_removed"] == 0
    assert result["lines_added"] >= 1
    # `+ ` プレフィクス付きで全行が表示される
    for line in result["diff"].splitlines():
        assert line.startswith("+ ")


def test_diff_handles_yml_extension(configs_dir):
    """.yml 拡張子の既存ファイルも検出する"""
    _write_yaml(configs_dir / "legacy.yml", "project_root: /tmp\n")
    result = compute_config_diff("legacy", {"project_root": "/var"})
    assert result["is_new_file"] is False
    assert result["has_changes"] is True


# ---------------------------------------------------------------------------
# get_config_backup_info
# ---------------------------------------------------------------------------


def test_backup_info_none_when_no_bak(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    assert get_config_backup_info("demo") is None


def test_backup_info_when_bak_exists(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    _write_yaml(configs_dir / "demo.yaml.bak", "x: 0\n")
    info = get_config_backup_info("demo")
    assert info is not None
    assert info["exists"] is True
    assert info["filename"] == "demo.yaml.bak"
    assert info["size"] > 0
    assert "T" in info["mtime"]  # ISO 8601 形式


def test_backup_info_for_yml_extension(configs_dir):
    _write_yaml(configs_dir / "legacy.yml", "x: 1\n")
    _write_yaml(configs_dir / "legacy.yml.bak", "x: 0\n")
    info = get_config_backup_info("legacy")
    assert info is not None
    assert info["filename"] == "legacy.yml.bak"


# ---------------------------------------------------------------------------
# restore_config_backup
# ---------------------------------------------------------------------------


def test_restore_replaces_current_with_bak(configs_dir):
    config_path = configs_dir / "demo.yaml"
    backup_path = configs_dir / "demo.yaml.bak"
    _write_yaml(config_path, "x: current\n")
    _write_yaml(backup_path, "x: previous\n")

    success, error = restore_config_backup("demo")

    assert success is True
    assert error is None
    assert config_path.read_text(encoding="utf-8") == "x: previous\n"
    # .bak ファイルは残る
    assert backup_path.exists()


def test_restore_returns_error_when_no_bak(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    success, error = restore_config_backup("demo")
    assert success is False
    assert error and "見つかりません" in error


def test_restore_handles_yml_extension(configs_dir):
    config_path = configs_dir / "legacy.yml"
    backup_path = configs_dir / "legacy.yml.bak"
    _write_yaml(config_path, "x: current\n")
    _write_yaml(backup_path, "x: previous\n")

    success, _ = restore_config_backup("legacy")

    assert success is True
    assert config_path.read_text(encoding="utf-8") == "x: previous\n"


# ---------------------------------------------------------------------------
# API: /api/config/diff
# ---------------------------------------------------------------------------


def test_api_diff_returns_diff_payload(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "project_root: /tmp\n")
    handler = _make_handler()
    handler._handle_config_diff_post = DashboardHandler._handle_config_diff_post.__get__(handler)
    handler._read_json_body.return_value = {
        "config_name": "demo",
        "data": {"project_root": "/var"},
    }

    handler._handle_config_diff_post()

    data = _parse(handler)
    assert data["success"] is True
    assert data["has_changes"] is True
    assert data["is_new_file"] is False
    assert "diff" in data


def test_api_diff_rejects_missing_config_name(configs_dir):
    handler = _make_handler()
    handler._handle_config_diff_post = DashboardHandler._handle_config_diff_post.__get__(handler)
    handler._read_json_body.return_value = {"data": {"x": 1}}

    handler._handle_config_diff_post()

    data = _parse(handler)
    assert data["success"] is False
    assert any("config_name" in e for e in data["errors"])
    handler.send_response.assert_called_with(400)


def test_api_diff_rejects_invalid_data(configs_dir):
    handler = _make_handler()
    handler._handle_config_diff_post = DashboardHandler._handle_config_diff_post.__get__(handler)
    handler._read_json_body.return_value = {"config_name": "demo", "data": "not-a-dict"}

    handler._handle_config_diff_post()

    data = _parse(handler)
    assert data["success"] is False
    assert any("data" in e for e in data["errors"])
    handler.send_response.assert_called_with(400)


def test_api_diff_rejects_path_traversal(configs_dir):
    handler = _make_handler()
    handler._handle_config_diff_post = DashboardHandler._handle_config_diff_post.__get__(handler)
    handler._read_json_body.return_value = {
        "config_name": "../../etc/passwd",
        "data": {"x": 1},
    }

    handler._handle_config_diff_post()

    data = _parse(handler)
    assert data["success"] is False
    assert any("不正" in e for e in data["errors"])
    handler.send_response.assert_called_with(400)


# ---------------------------------------------------------------------------
# API: /api/config/backup (GET)
# ---------------------------------------------------------------------------


def test_api_backup_info_returns_exists_false_when_missing(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    handler = _make_handler()
    handler._handle_config_backup_info_get = DashboardHandler._handle_config_backup_info_get.__get__(handler)

    handler._handle_config_backup_info_get("demo")

    data = _parse(handler)
    assert data["success"] is True
    assert data["exists"] is False


def test_api_backup_info_returns_metadata_when_present(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    _write_yaml(configs_dir / "demo.yaml.bak", "x: 0\n")
    handler = _make_handler()
    handler._handle_config_backup_info_get = DashboardHandler._handle_config_backup_info_get.__get__(handler)

    handler._handle_config_backup_info_get("demo")

    data = _parse(handler)
    assert data["success"] is True
    assert data["exists"] is True
    assert data["filename"] == "demo.yaml.bak"
    assert "mtime" in data


def test_api_backup_info_rejects_missing_name(configs_dir):
    handler = _make_handler()
    handler._handle_config_backup_info_get = DashboardHandler._handle_config_backup_info_get.__get__(handler)

    handler._handle_config_backup_info_get(None)

    data = _parse(handler)
    assert data["success"] is False
    handler.send_response.assert_called_with(400)


# ---------------------------------------------------------------------------
# API: /api/config/backup/restore (POST)
# ---------------------------------------------------------------------------


def test_api_backup_restore_success(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: current\n")
    _write_yaml(configs_dir / "demo.yaml.bak", "x: previous\n")

    handler = _make_handler()
    handler._handle_config_backup_restore_post = DashboardHandler._handle_config_backup_restore_post.__get__(handler)
    handler._read_json_body.return_value = {"config_name": "demo"}

    handler._handle_config_backup_restore_post()

    data = _parse(handler)
    assert data["success"] is True
    assert "復元" in data["message"]
    assert (configs_dir / "demo.yaml").read_text(encoding="utf-8") == "x: previous\n"


def test_api_backup_restore_returns_404_when_no_bak(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")

    handler = _make_handler()
    handler._handle_config_backup_restore_post = DashboardHandler._handle_config_backup_restore_post.__get__(handler)
    handler._read_json_body.return_value = {"config_name": "demo"}

    handler._handle_config_backup_restore_post()

    data = _parse(handler)
    assert data["success"] is False
    handler.send_response.assert_called_with(404)


def test_api_backup_restore_rejects_missing_config_name(configs_dir):
    handler = _make_handler()
    handler._handle_config_backup_restore_post = DashboardHandler._handle_config_backup_restore_post.__get__(handler)
    handler._read_json_body.return_value = {}

    handler._handle_config_backup_restore_post()

    data = _parse(handler)
    assert data["success"] is False
    handler.send_response.assert_called_with(400)


def test_api_backup_restore_rejects_path_traversal(configs_dir):
    handler = _make_handler()
    handler._handle_config_backup_restore_post = DashboardHandler._handle_config_backup_restore_post.__get__(handler)
    handler._read_json_body.return_value = {"config_name": "../../etc/passwd"}

    handler._handle_config_backup_restore_post()

    data = _parse(handler)
    assert data["success"] is False
    assert any("不正" in e for e in data["errors"])
    handler.send_response.assert_called_with(400)


def test_api_backup_info_rejects_path_traversal(configs_dir):
    handler = _make_handler()
    handler._handle_config_backup_info_get = DashboardHandler._handle_config_backup_info_get.__get__(handler)

    handler._handle_config_backup_info_get("../../etc/passwd")

    data = _parse(handler)
    assert data["success"] is False
    assert any("不正" in e for e in data["errors"])
    handler.send_response.assert_called_with(400)


# ---------------------------------------------------------------------------
# Path traversal: helper level (defense in depth)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../etc/passwd", "..", ".", "foo/bar", "foo\\bar", "", None],
)
def test_safe_config_path_rejects_unsafe_names(configs_dir, name):
    with pytest.raises(ValueError):
        _safe_config_path(name, ".yaml")


def test_safe_config_path_accepts_normal_name(configs_dir):
    path = _safe_config_path("demo", ".yaml")
    assert path.parent == configs_dir.resolve()
    assert path.name == "demo.yaml"


def test_compute_config_diff_rejects_unsafe_name(configs_dir):
    """compute_config_diff も内部で _safe_config_path を呼び ValueError を返す"""
    with pytest.raises(ValueError):
        compute_config_diff("../escape", {"x": 1})


def test_restore_config_backup_rejects_unsafe_name(configs_dir):
    success, error = restore_config_backup("../escape")
    assert success is False
    assert error and "不正" in error


def test_get_config_backup_info_returns_none_for_unsafe_name(configs_dir):
    """get_config_backup_info は ValueError を None として返す（呼び出し側で 400 にする）"""
    assert get_config_backup_info("../escape") is None


# ---------------------------------------------------------------------------
# Path traversal: save_config_yaml (defense in depth on write path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../escape", "..", ".", "foo/bar", "foo\\bar", ""],
)
def test_save_config_yaml_rejects_unsafe_name(configs_dir, name):
    """書き込み経路でも `_safe_config_path` 経由で CONFIGS_DIR 配下に限定する"""
    with pytest.raises(ValueError):
        save_config_yaml(name, {"x": 1})


def test_save_config_yaml_writes_inside_configs_dir(configs_dir):
    """正常 name は CONFIGS_DIR 配下に保存される"""
    success = save_config_yaml("demo", {"project_root": "/tmp"})
    assert success is True
    assert (configs_dir / "demo.yaml").exists()


def test_api_save_returns_400_on_unsafe_config_name(configs_dir):
    """`_handle_settings_post` で ValueError を 400 にマップする"""
    handler = _make_handler()
    handler._handle_settings_post = DashboardHandler._handle_settings_post.__get__(handler)
    handler._read_json_body.return_value = {
        "config_name": "../escape",
        "data": {"project_root": str(configs_dir), "base_branch": "main"},
    }

    handler._handle_settings_post()

    data = _parse(handler)
    assert data["success"] is False
    assert any("不正" in e for e in data["errors"])
    handler.send_response.assert_called_with(400)


# ---------------------------------------------------------------------------
# Routing (do_POST / do_GET dispatch)
# ---------------------------------------------------------------------------


def test_do_post_routes_diff_endpoint():
    handler = _make_handler()
    handler.path = "/api/config/diff"
    handler._handle_config_diff_post = MagicMock()
    handler._handle_config_backup_restore_post = MagicMock()
    handler.do_POST = DashboardHandler.do_POST.__get__(handler)

    handler.do_POST()

    handler._handle_config_diff_post.assert_called_once()
    handler._handle_config_backup_restore_post.assert_not_called()


def test_do_post_routes_backup_restore_endpoint():
    handler = _make_handler()
    handler.path = "/api/config/backup/restore"
    handler._handle_config_diff_post = MagicMock()
    handler._handle_config_backup_restore_post = MagicMock()
    handler.do_POST = DashboardHandler.do_POST.__get__(handler)

    handler.do_POST()

    handler._handle_config_backup_restore_post.assert_called_once()
    handler._handle_config_diff_post.assert_not_called()


def test_do_get_routes_backup_info_endpoint():
    handler = _make_handler()
    handler.path = "/api/config/backup?name=demo"
    handler._handle_config_backup_info_get = MagicMock()
    handler.do_GET = DashboardHandler.do_GET.__get__(handler)

    handler.do_GET()

    handler._handle_config_backup_info_get.assert_called_once_with("demo")
