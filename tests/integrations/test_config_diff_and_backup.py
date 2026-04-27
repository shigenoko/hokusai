"""
Phase B-1 (差分プレビュー) と Phase B-2 (.bak 復元) のテスト

- compute_config_diff: 差分計算ヘルパー
- list_config_backups: バックアップ一覧（多世代対応）
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
    BACKUP_RETAIN_COUNT,
    DashboardHandler,
    _prune_old_backups,
    _safe_backup_path,
    _safe_config_path,
    compute_config_diff,
    list_config_backups,
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
# list_config_backups
# ---------------------------------------------------------------------------


def test_list_backups_empty(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    assert list_config_backups("demo") == []


def test_list_backups_includes_legacy(configs_dir):
    """PR #5 以前の単一世代 .bak も一覧に含まれる"""
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    _write_yaml(configs_dir / "demo.yaml.bak", "x: 0\n")
    backups = list_config_backups("demo")
    assert len(backups) == 1
    assert backups[0]["filename"] == "demo.yaml.bak"
    assert backups[0]["size"] > 0


def test_list_backups_returns_timestamped(configs_dir):
    """新しい順（mtime 降順）で返される。同秒は filename で決定的に並ぶ。"""
    import os as _os
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    a = configs_dir / "demo.yaml.bak.20260427-100000"
    b = configs_dir / "demo.yaml.bak.20260427-110000"
    c = configs_dir / "demo.yaml.bak.20260427-120000"
    _write_yaml(a, "x: a\n")
    _write_yaml(b, "x: b\n")
    _write_yaml(c, "x: c\n")
    # mtime を明示して並びを安定化
    _os.utime(a, (1700000000, 1700000000))
    _os.utime(b, (1800000000, 1800000000))
    _os.utime(c, (1900000000, 1900000000))

    backups = list_config_backups("demo")
    assert [b["filename"] for b in backups] == [
        "demo.yaml.bak.20260427-120000",
        "demo.yaml.bak.20260427-110000",
        "demo.yaml.bak.20260427-100000",
    ]


def test_list_backups_handles_yml_extension(configs_dir):
    _write_yaml(configs_dir / "legacy.yml", "x: 1\n")
    _write_yaml(configs_dir / "legacy.yml.bak", "x: 0\n")
    _write_yaml(configs_dir / "legacy.yml.bak.20260427-100000", "x: a\n")
    backups = list_config_backups("legacy")
    assert len(backups) == 2


def test_list_backups_returns_empty_for_unsafe_name(configs_dir):
    assert list_config_backups("../escape") == []


# ---------------------------------------------------------------------------
# restore_config_backup
# ---------------------------------------------------------------------------


def test_restore_picks_latest_backup_by_default(configs_dir):
    """filename 省略時は最新のバックアップを復元する"""
    config_path = configs_dir / "demo.yaml"
    _write_yaml(config_path, "x: current\n")
    # 古い世代と新しい世代を作る
    older = configs_dir / "demo.yaml.bak.20260101-000000"
    newer = configs_dir / "demo.yaml.bak.20260427-120000"
    _write_yaml(older, "x: older\n")
    _write_yaml(newer, "x: newer\n")
    # mtime を確実に差をつける
    import os as _os
    _os.utime(older, (1700000000, 1700000000))
    _os.utime(newer, (1900000000, 1900000000))

    success, error = restore_config_backup("demo")

    assert success is True
    assert error is None
    assert config_path.read_text(encoding="utf-8") == "x: newer\n"
    # 元のバックアップは残る
    assert older.exists()
    assert newer.exists()


def test_restore_specific_filename(configs_dir):
    """filename 指定時はその世代を復元する"""
    config_path = configs_dir / "demo.yaml"
    _write_yaml(config_path, "x: current\n")
    older = configs_dir / "demo.yaml.bak.20260101-000000"
    newer = configs_dir / "demo.yaml.bak.20260427-120000"
    _write_yaml(older, "x: older\n")
    _write_yaml(newer, "x: newer\n")

    success, error = restore_config_backup("demo", filename="demo.yaml.bak.20260101-000000")

    assert success is True
    assert error is None
    assert config_path.read_text(encoding="utf-8") == "x: older\n"


def test_restore_legacy_bak_still_works(configs_dir):
    """PR #5 以前のレガシー .bak も復元できる"""
    config_path = configs_dir / "demo.yaml"
    backup_path = configs_dir / "demo.yaml.bak"
    _write_yaml(config_path, "x: current\n")
    _write_yaml(backup_path, "x: previous\n")

    success, error = restore_config_backup("demo")

    assert success is True
    assert error is None
    assert config_path.read_text(encoding="utf-8") == "x: previous\n"


def test_restore_returns_error_when_no_bak(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    success, error = restore_config_backup("demo")
    assert success is False
    assert error and "見つかりません" in error


def test_restore_rejects_filename_outside_config_name(configs_dir):
    """指定 filename が他の config に属する場合は拒否"""
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    _write_yaml(configs_dir / "other.yaml.bak", "x: hostile\n")

    success, error = restore_config_backup("demo", filename="other.yaml.bak")

    assert success is False
    assert error and ("対応していません" in error or "不正" in error)


def test_restore_rejects_path_traversal_filename(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    success, error = restore_config_backup("demo", filename="../etc/passwd")
    assert success is False
    assert error and "不正" in error


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


def test_api_backup_info_returns_empty_list_when_missing(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    handler = _make_handler()
    handler._handle_config_backup_info_get = DashboardHandler._handle_config_backup_info_get.__get__(handler)

    handler._handle_config_backup_info_get("demo")

    data = _parse(handler)
    assert data["success"] is True
    assert data["exists"] is False
    assert data["backups"] == []
    assert data["retain"] == BACKUP_RETAIN_COUNT


def test_api_backup_info_returns_list_when_present(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    _write_yaml(configs_dir / "demo.yaml.bak.20260427-100000", "x: a\n")
    _write_yaml(configs_dir / "demo.yaml.bak.20260427-110000", "x: b\n")
    handler = _make_handler()
    handler._handle_config_backup_info_get = DashboardHandler._handle_config_backup_info_get.__get__(handler)

    handler._handle_config_backup_info_get("demo")

    data = _parse(handler)
    assert data["success"] is True
    assert data["exists"] is True
    assert len(data["backups"]) == 2
    for entry in data["backups"]:
        assert "filename" in entry
        assert "mtime" in entry
        assert "size" in entry


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


def test_api_backup_restore_picks_latest_by_default(configs_dir):
    """filename 省略時は最新世代を復元する"""
    import os as _os
    _write_yaml(configs_dir / "demo.yaml", "x: current\n")
    older = configs_dir / "demo.yaml.bak.20260427-100000"
    newer = configs_dir / "demo.yaml.bak.20260427-120000"
    _write_yaml(older, "x: a\n")
    _write_yaml(newer, "x: b\n")
    # CI 環境で連続書き込みの mtime が同秒になるのを避けるため explicit に設定
    _os.utime(older, (1700000000, 1700000000))
    _os.utime(newer, (1900000000, 1900000000))

    handler = _make_handler()
    handler._handle_config_backup_restore_post = DashboardHandler._handle_config_backup_restore_post.__get__(handler)
    handler._read_json_body.return_value = {"config_name": "demo"}

    handler._handle_config_backup_restore_post()

    data = _parse(handler)
    assert data["success"] is True
    assert "復元" in data["message"]
    # 最新の方が復元される
    assert (configs_dir / "demo.yaml").read_text(encoding="utf-8") == "x: b\n"
    assert data["filename"] == "demo.yaml.bak.20260427-120000"


def test_api_backup_restore_with_specific_filename(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: current\n")
    _write_yaml(configs_dir / "demo.yaml.bak.20260101-000000", "x: older\n")
    _write_yaml(configs_dir / "demo.yaml.bak.20260427-120000", "x: newer\n")

    handler = _make_handler()
    handler._handle_config_backup_restore_post = DashboardHandler._handle_config_backup_restore_post.__get__(handler)
    handler._read_json_body.return_value = {
        "config_name": "demo",
        "filename": "demo.yaml.bak.20260101-000000",
    }

    handler._handle_config_backup_restore_post()

    data = _parse(handler)
    assert data["success"] is True
    assert (configs_dir / "demo.yaml").read_text(encoding="utf-8") == "x: older\n"
    assert data["filename"] == "demo.yaml.bak.20260101-000000"


def test_api_backup_restore_rejects_filename_for_other_config(configs_dir):
    """指定 filename が他の config を指す場合 400 を返す"""
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    _write_yaml(configs_dir / "other.yaml.bak", "x: hostile\n")

    handler = _make_handler()
    handler._handle_config_backup_restore_post = DashboardHandler._handle_config_backup_restore_post.__get__(handler)
    handler._read_json_body.return_value = {
        "config_name": "demo",
        "filename": "other.yaml.bak",
    }

    handler._handle_config_backup_restore_post()

    data = _parse(handler)
    assert data["success"] is False
    handler.send_response.assert_called_with(400)


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


def test_list_config_backups_returns_empty_for_unsafe_name(configs_dir):
    """`list_config_backups` は不正な name に対して空リストを返す（呼び出し側で 400 にする）"""
    assert list_config_backups("../escape") == []


# ---------------------------------------------------------------------------
# Multi-generation backup: save_config_yaml + _prune_old_backups
# ---------------------------------------------------------------------------


def test_save_creates_timestamped_backup(configs_dir):
    """save_config_yaml は `.bak.<timestamp>` 形式でバックアップを作成する"""
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    save_config_yaml("demo", {"x": 2})

    bak_files = list(configs_dir.glob("demo.yaml.bak*"))
    assert len(bak_files) == 1
    # timestamp 付きの形式
    assert bak_files[0].name.startswith("demo.yaml.bak.")
    # 元の内容がバックアップされている
    assert "x: 1" in bak_files[0].read_text(encoding="utf-8")


def test_save_creates_multiple_generations(configs_dir):
    """連続保存で複数の世代が蓄積する"""
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    save_config_yaml("demo", {"x": 2})
    # mtime に差を付けるためタイムスタンプ命名を別にする必要があるが、
    # save_config_yaml は秒単位なので、ファイル名衝突を避けるため少し待つ。
    # ここでは前のバックアップを別名にリネームしておく。
    bak1 = list(configs_dir.glob("demo.yaml.bak.*"))[0]
    bak1.rename(configs_dir / "demo.yaml.bak.20260101-000000")
    save_config_yaml("demo", {"x": 3})

    bak_files = list(configs_dir.glob("demo.yaml.bak.*"))
    assert len(bak_files) == 2


def test_prune_keeps_only_retain_count(configs_dir):
    """`_prune_old_backups` は retain を超える古いものを削除する"""
    config_path = configs_dir / "demo.yaml"
    _write_yaml(config_path, "x: 1\n")
    # 5 つのバックアップを mtime 違いで作成
    backup_paths = []
    for i, ts in enumerate(["100", "200", "300", "400", "500"]):
        p = configs_dir / f"demo.yaml.bak.20260427-{ts}"
        _write_yaml(p, f"x: {i}\n")
        # 異なる mtime を設定
        import os as _os
        _os.utime(p, (1700000000 + i * 1000, 1700000000 + i * 1000))
        backup_paths.append(p)

    deleted = _prune_old_backups(config_path, retain=2)

    assert deleted == 3
    remaining = sorted(configs_dir.glob("demo.yaml.bak.*"))
    assert len(remaining) == 2
    # 新しい 2 件（mtime 大）が残る
    remaining_names = {p.name for p in remaining}
    assert "demo.yaml.bak.20260427-500" in remaining_names
    assert "demo.yaml.bak.20260427-400" in remaining_names


def test_prune_no_op_when_within_retain(configs_dir):
    config_path = configs_dir / "demo.yaml"
    _write_yaml(config_path, "x: 1\n")
    _write_yaml(configs_dir / "demo.yaml.bak.20260427-100000", "x: a\n")

    deleted = _prune_old_backups(config_path, retain=10)

    assert deleted == 0
    assert (configs_dir / "demo.yaml.bak.20260427-100000").exists()


# ---------------------------------------------------------------------------
# _safe_backup_path
# ---------------------------------------------------------------------------


def test_safe_backup_path_accepts_legacy(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    _write_yaml(configs_dir / "demo.yaml.bak", "x: 0\n")
    path = _safe_backup_path("demo", "demo.yaml.bak")
    assert path.name == "demo.yaml.bak"


def test_safe_backup_path_accepts_timestamped(configs_dir):
    _write_yaml(configs_dir / "demo.yaml", "x: 1\n")
    _write_yaml(configs_dir / "demo.yaml.bak.20260427-100000", "x: 0\n")
    path = _safe_backup_path("demo", "demo.yaml.bak.20260427-100000")
    assert path.name == "demo.yaml.bak.20260427-100000"


@pytest.mark.parametrize(
    "filename",
    [
        "../etc/passwd",
        "demo.yaml.bak/../escape",
        "/etc/passwd",
        "",
        "..",
    ],
)
def test_safe_backup_path_rejects_traversal(configs_dir, filename):
    with pytest.raises(ValueError):
        _safe_backup_path("demo", filename)


def test_safe_backup_path_rejects_other_config(configs_dir):
    """`other` の .bak を `demo` 用として渡したら拒否する"""
    _write_yaml(configs_dir / "other.yaml.bak", "x: 0\n")
    with pytest.raises(ValueError):
        _safe_backup_path("demo", "other.yaml.bak")


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


def test_api_save_returns_400_on_missing_config_name(configs_dir):
    """`_handle_settings_post` の他のクライアント側エラーも 400 で返す"""
    handler = _make_handler()
    handler._handle_settings_post = DashboardHandler._handle_settings_post.__get__(handler)
    handler._read_json_body.return_value = {"data": {"project_root": str(configs_dir)}}

    handler._handle_settings_post()

    data = _parse(handler)
    assert data["success"] is False
    handler.send_response.assert_called_with(400)


def test_api_save_returns_400_on_invalid_data(configs_dir):
    handler = _make_handler()
    handler._handle_settings_post = DashboardHandler._handle_settings_post.__get__(handler)
    handler._read_json_body.return_value = {"config_name": "demo", "data": "not-a-dict"}

    handler._handle_settings_post()

    data = _parse(handler)
    assert data["success"] is False
    handler.send_response.assert_called_with(400)


def test_api_save_returns_400_on_validate_config_failure(configs_dir):
    """validate_config NG（必須フィールド欠損など）も 400 で返す"""
    handler = _make_handler()
    handler._handle_settings_post = DashboardHandler._handle_settings_post.__get__(handler)
    # base_branch も project_root も指定しない → validate_config が必須欠損で NG
    handler._read_json_body.return_value = {"config_name": "demo", "data": {}}

    handler._handle_settings_post()

    data = _parse(handler)
    assert data["success"] is False
    handler.send_response.assert_called_with(400)


# ---------------------------------------------------------------------------
# load_config_yaml / parse_project_rules: パストラバーサル防御 (defense in depth)
# ---------------------------------------------------------------------------


def test_load_config_yaml_rejects_unsafe_name(configs_dir):
    """load_config_yaml も `_resolve_config_path` 経由で CONFIGS_DIR 外を読まない"""
    from scripts.dashboard import load_config_yaml

    with pytest.raises(FileNotFoundError):
        # `..` 等の不正な name は _resolve_config_path が None を返し、
        # FileNotFoundError として返ってくる
        load_config_yaml("../etc/passwd")


def test_parse_project_rules_returns_empty_for_unsafe_name(configs_dir):
    """parse_project_rules も同様に不正な name で外部ファイルを読まない"""
    from scripts.dashboard import parse_project_rules

    rules = parse_project_rules("../escape")
    assert rules == []


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
