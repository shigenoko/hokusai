"""CLI profile 統合のテスト（Phase B）

対象:
- create_config_from_env_and_file() に profile_name を渡す経路
- `hokusai profile list / show / doctor`
- `--profile` と `--config` の同時指定エラー
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import yaml

from hokusai.cli_main import (
    _handle_profile_doctor,
    _handle_profile_list,
    _handle_profile_show,
)
from hokusai.config import (
    ConflictingProfileAndConfigError,
    create_config_from_env_and_file,
    load_profile_registry,
)
from hokusai.config.profiles import (
    ProfileNotFoundError,
    ProfileRegistryNotFoundError,
)


def _make_registry(tmp_path: Path, profiles: dict, default: str | None = None) -> Path:
    """テスト用 registry + 紐づく config files を生成"""
    raw_profiles = {}
    for name, opts in profiles.items():
        cfg = tmp_path / f"{name}.yaml"
        cfg.write_text("project_root: /tmp\n")
        entry = {"config": str(cfg)}
        entry.update(opts)
        raw_profiles[name] = entry

    data: dict = {"profiles": raw_profiles}
    if default:
        data["default_profile"] = default

    registry_path = tmp_path / "profiles.yaml"
    registry_path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return registry_path


# ---------------------------------------------------------------------------
# create_config_from_env_and_file との統合
# ---------------------------------------------------------------------------


def test_create_config_with_profile_name_resolves_path(tmp_path, monkeypatch):
    registry_file = _make_registry(tmp_path, {"a-co": {}})
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    config = create_config_from_env_and_file(profile_name="a-co")
    assert config is not None
    assert config.project_root == Path("/tmp")


def test_create_config_profile_and_file_exclusive(tmp_path):
    """--profile と --config の同時指定は排他エラー"""
    with pytest.raises(ConflictingProfileAndConfigError):
        create_config_from_env_and_file(
            config_file=str(tmp_path / "any.yaml"),
            profile_name="a-co",
        )


def test_create_config_profile_not_found(tmp_path, monkeypatch):
    registry_file = _make_registry(tmp_path, {"a-co": {}})
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    with pytest.raises(ProfileNotFoundError):
        create_config_from_env_and_file(profile_name="missing")


def test_create_config_registry_not_found_when_profile_specified(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "HOKUSAI_PROFILES_FILE", str(tmp_path / "nonexistent.yaml")
    )
    with pytest.raises(ProfileRegistryNotFoundError):
        create_config_from_env_and_file(profile_name="a-co")


def test_create_config_without_profile_unchanged(tmp_path, monkeypatch):
    """profile_name 未指定なら既存動作（config_file 直接指定 or デフォルト探索）が変わらない"""
    monkeypatch.delenv("HOKUSAI_PROFILES_FILE", raising=False)
    cfg = tmp_path / "direct.yaml"
    cfg.write_text("project_root: /tmp/direct\n")

    config = create_config_from_env_and_file(str(cfg))
    assert config.project_root == Path("/tmp/direct")


# ---------------------------------------------------------------------------
# _handle_profile_list
# ---------------------------------------------------------------------------


def _capture_stdout(callable_, *args, **kwargs):
    """stdout を捕捉して (return_code, captured_text) を返す"""
    old = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        rc = callable_(*args, **kwargs)
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


def test_profile_list_empty(tmp_path, monkeypatch):
    registry_file = tmp_path / "profiles.yaml"
    registry_file.write_text("profiles: {}\n")
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_list, registry)
    assert rc == 0
    assert "登録されている profile はありません" in output


def test_profile_list_shows_all(tmp_path, monkeypatch):
    registry_file = _make_registry(
        tmp_path,
        {
            "a-co": {"data_dir": str(tmp_path / "a-data")},
            "b-co": {"data_dir": str(tmp_path / "b-data")},
        },
        default="a-co",
    )
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_list, registry)
    assert rc == 0
    assert "a-co" in output
    assert "b-co" in output
    assert "default_profile: a-co" in output


# ---------------------------------------------------------------------------
# _handle_profile_show
# ---------------------------------------------------------------------------


def test_profile_show_displays_metadata(tmp_path, monkeypatch):
    registry_file = _make_registry(
        tmp_path,
        {
            "a-co": {
                "label": "A社 EC",
                "description": "A社案件",
                "data_dir": str(tmp_path / "a-data"),
                "dashboard": {"port": 8765},
            }
        },
    )
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_show, "a-co", registry)
    assert rc == 0
    assert "a-co" in output
    assert "A社 EC" in output
    assert "A社案件" in output
    assert "8765" in output
    # シークレットを表示しないことの注意書き
    assert "シークレット" in output


def test_profile_show_unknown_returns_error(tmp_path, monkeypatch):
    registry_file = _make_registry(tmp_path, {"a-co": {}})
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_show, "missing", registry)
    assert rc == 1
    assert "missing" in output


# ---------------------------------------------------------------------------
# _handle_profile_doctor
# ---------------------------------------------------------------------------


def test_profile_doctor_clean(tmp_path, monkeypatch):
    data_dir = tmp_path / "a-data"
    data_dir.mkdir()
    registry_file = _make_registry(
        tmp_path,
        {"a-co": {"data_dir": str(data_dir), "dashboard": {"port": 8765}}},
    )
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_doctor, "a-co", registry)
    assert rc == 0
    assert "OK" in output
    assert "✓" in output


def test_profile_doctor_missing_config_file(tmp_path, monkeypatch):
    """config file 実体が存在しない場合は doctor が問題を検出"""
    registry_path = tmp_path / "profiles.yaml"
    registry_path.write_text(yaml.safe_dump({
        "profiles": {"a-co": {"config": str(tmp_path / "missing.yaml")}}
    }))
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_path))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_doctor, "a-co", registry)
    assert rc == 1
    assert "config file が見つかりません" in output


def test_profile_doctor_creates_data_dir(tmp_path, monkeypatch):
    """data_dir が存在しなくても作成可能なら OK"""
    data_dir = tmp_path / "new-data"
    assert not data_dir.exists()
    registry_file = _make_registry(
        tmp_path,
        {"a-co": {"data_dir": str(data_dir)}},
    )
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_doctor, "a-co", registry)
    assert rc == 0
    assert data_dir.exists()
    assert "data_dir created" in output


def test_profile_doctor_detects_port_conflict(tmp_path, monkeypatch):
    """2 profile が同じ dashboard port を使うと衝突として検出"""
    registry_file = _make_registry(
        tmp_path,
        {
            "a-co": {"dashboard": {"port": 8765}},
            "b-co": {"dashboard": {"port": 8765}},
        },
    )
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_doctor, "a-co", registry)
    assert rc == 1
    assert "port" in output.lower() and "衝突" in output
    assert "b-co" in output


def test_profile_doctor_detects_data_dir_conflict(tmp_path, monkeypatch):
    """2 profile が同じ data_dir を使うと衝突として検出"""
    shared = tmp_path / "shared-data"
    shared.mkdir()
    registry_file = _make_registry(
        tmp_path,
        {
            "a-co": {"data_dir": str(shared)},
            "b-co": {"data_dir": str(shared)},
        },
    )
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_doctor, "a-co", registry)
    assert rc == 1
    assert "data_dir" in output and "衝突" in output


def test_profile_doctor_deep_flag_acknowledged(tmp_path, monkeypatch):
    """--deep フラグが認識され、Phase E 予定の注意書きが出る"""
    registry_file = _make_registry(tmp_path, {"a-co": {}})
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_doctor, "a-co", registry, deep=True)
    assert rc == 0
    assert "Phase E" in output


def test_profile_doctor_unknown_profile(tmp_path, monkeypatch):
    registry_file = _make_registry(tmp_path, {"a-co": {}})
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry_file))

    registry = load_profile_registry()
    rc, output = _capture_stdout(_handle_profile_doctor, "missing", registry)
    assert rc == 1
    assert "missing" in output
