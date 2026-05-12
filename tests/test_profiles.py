"""Profile Registry のテスト（Phase A）

対象: hokusai/config/profiles.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hokusai.config.profiles import (
    ConflictingProfileAndConfigError,
    InvalidProfileNameError,
    ProfileConfig,
    ProfileError,
    ProfileNotFoundError,
    ProfileRegistry,
    ProfileRegistryNotFoundError,
    assert_profile_config_exclusive,
    load_profile_registry,
    resolve_profile_to_config_path,
    resolve_registry_path,
    validate_profile_name,
)

# ---------------------------------------------------------------------------
# validate_profile_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a", "a-company", "b_company", "c123", "ab2026"])
def test_validate_profile_name_accepts_valid(name: str):
    validate_profile_name(name)  # raises なし


@pytest.mark.parametrize(
    "name",
    [
        "",
        "1company",      # 先頭が数字
        "-company",      # 先頭がハイフン
        "A-company",     # 大文字
        "a/company",     # スラッシュ
        "a company",     # スペース
        "a.company",     # ドット
    ],
)
def test_validate_profile_name_rejects_invalid(name: str):
    with pytest.raises(InvalidProfileNameError):
        validate_profile_name(name)


# ---------------------------------------------------------------------------
# resolve_registry_path
# ---------------------------------------------------------------------------


def test_resolve_registry_path_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(tmp_path / "env.yaml"))
    explicit = tmp_path / "explicit.yaml"
    assert resolve_registry_path(explicit) == explicit


def test_resolve_registry_path_env_var(tmp_path, monkeypatch):
    target = tmp_path / "env.yaml"
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(target))
    assert resolve_registry_path() == target


def test_resolve_registry_path_default(monkeypatch):
    monkeypatch.delenv("HOKUSAI_PROFILES_FILE", raising=False)
    resolved = resolve_registry_path()
    assert resolved.name == "profiles.yaml"
    assert resolved.parent.name == ".hokusai"


def test_resolve_registry_path_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", "~/custom/profiles.yaml")
    resolved = resolve_registry_path()
    assert "~" not in str(resolved)
    assert resolved.name == "profiles.yaml"


# ---------------------------------------------------------------------------
# load_profile_registry: 正常系
# ---------------------------------------------------------------------------


def _write_registry(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_load_profile_registry_parses_minimal(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry_file = _write_registry(tmp_path, {
        "profiles": {
            "a-company": {"config": str(cfg)},
        }
    })

    registry = load_profile_registry(registry_file)
    assert registry.default_profile is None
    assert "a-company" in registry.profiles
    p = registry.profiles["a-company"]
    assert p.name == "a-company"
    assert p.config_path == cfg
    assert p.data_dir is None
    assert p.dashboard_port is None
    assert p.label is None
    assert p.description is None
    assert registry.source_path == registry_file


def test_load_profile_registry_parses_full(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry_file = _write_registry(tmp_path, {
        "default_profile": "a-company",
        "profiles": {
            "a-company": {
                "label": "A社 EC",
                "config": str(cfg),
                "data_dir": str(tmp_path / "a-data"),
                "dashboard": {"port": 8765},
                "description": "A社向け案件",
            },
        },
    })

    registry = load_profile_registry(registry_file)
    assert registry.default_profile == "a-company"
    p = registry.profiles["a-company"]
    assert p.label == "A社 EC"
    assert p.data_dir == tmp_path / "a-data"
    assert p.dashboard_port == 8765
    assert p.description == "A社向け案件"


def test_load_profile_registry_multiple_profiles(tmp_path):
    cfg_a = tmp_path / "a.yaml"
    cfg_a.write_text("project_root: /tmp/a\n")
    cfg_b = tmp_path / "b.yaml"
    cfg_b.write_text("project_root: /tmp/b\n")
    registry_file = _write_registry(tmp_path, {
        "profiles": {
            "a-company": {"config": str(cfg_a), "dashboard": {"port": 8765}},
            "b-company": {"config": str(cfg_b), "dashboard": {"port": 8766}},
        }
    })

    registry = load_profile_registry(registry_file)
    assert registry.names() == ["a-company", "b-company"]
    assert registry.profiles["a-company"].dashboard_port == 8765
    assert registry.profiles["b-company"].dashboard_port == 8766


def test_registry_get_returns_profile(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry_file = _write_registry(tmp_path, {
        "profiles": {"a-company": {"config": str(cfg)}}
    })
    registry = load_profile_registry(registry_file)
    p = registry.get("a-company")
    assert isinstance(p, ProfileConfig)
    assert p.name == "a-company"


# ---------------------------------------------------------------------------
# load_profile_registry: 異常系
# ---------------------------------------------------------------------------


def test_load_profile_registry_missing_file(tmp_path):
    with pytest.raises(ProfileRegistryNotFoundError):
        load_profile_registry(tmp_path / "nonexistent.yaml")


def test_load_profile_registry_broken_yaml(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("profiles: {invalid: [unclosed\n")
    with pytest.raises(ProfileError, match="YAML"):
        load_profile_registry(path)


def test_load_profile_registry_top_level_not_dict(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- item1\n- item2\n")
    with pytest.raises(ProfileError, match="dict"):
        load_profile_registry(path)


def test_load_profile_registry_profiles_not_dict(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("profiles:\n  - a-company\n")
    with pytest.raises(ProfileError, match="profiles"):
        load_profile_registry(path)


def test_load_profile_registry_missing_config_key(tmp_path):
    registry_file = _write_registry(tmp_path, {
        "profiles": {"a-company": {"label": "no config here"}}
    })
    with pytest.raises(ProfileError, match="config"):
        load_profile_registry(registry_file)


def test_load_profile_registry_invalid_profile_name(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry_file = _write_registry(tmp_path, {
        "profiles": {"BadName": {"config": str(cfg)}}
    })
    with pytest.raises(InvalidProfileNameError):
        load_profile_registry(registry_file)


def test_load_profile_registry_default_not_in_profiles(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry_file = _write_registry(tmp_path, {
        "default_profile": "nonexistent",
        "profiles": {"a-company": {"config": str(cfg)}},
    })
    with pytest.raises(ProfileError, match="default_profile"):
        load_profile_registry(registry_file)


def test_load_profile_registry_dashboard_port_not_int(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry_file = _write_registry(tmp_path, {
        "profiles": {
            "a-company": {"config": str(cfg), "dashboard": {"port": "8765"}}
        }
    })
    with pytest.raises(ProfileError, match="port"):
        load_profile_registry(registry_file)


# ---------------------------------------------------------------------------
# ProfileRegistry.get
# ---------------------------------------------------------------------------


def test_registry_get_unknown_profile_raises_with_candidates(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry_file = _write_registry(tmp_path, {
        "profiles": {
            "a-company": {"config": str(cfg)},
            "b-company": {"config": str(cfg)},
        }
    })
    registry = load_profile_registry(registry_file)
    with pytest.raises(ProfileNotFoundError, match="a-company.*b-company"):
        registry.get("c-company")


def test_registry_get_empty_registry_shows_none(tmp_path):
    registry = ProfileRegistry()
    with pytest.raises(ProfileNotFoundError, match="\\(none\\)"):
        registry.get("any")


# ---------------------------------------------------------------------------
# assert_profile_config_exclusive
# ---------------------------------------------------------------------------


def test_assert_exclusive_both_specified_raises():
    with pytest.raises(ConflictingProfileAndConfigError):
        assert_profile_config_exclusive("a-company", "configs/a.yaml")


def test_assert_exclusive_only_profile_ok():
    assert_profile_config_exclusive("a-company", None)


def test_assert_exclusive_only_config_ok():
    assert_profile_config_exclusive(None, "configs/a.yaml")


def test_assert_exclusive_neither_ok():
    assert_profile_config_exclusive(None, None)


# ---------------------------------------------------------------------------
# resolve_profile_to_config_path
# ---------------------------------------------------------------------------


def test_resolve_profile_to_config_path_success(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry_file = _write_registry(tmp_path, {
        "profiles": {"a-company": {"config": str(cfg)}}
    })

    profile, path = resolve_profile_to_config_path("a-company", registry_file)
    assert profile.name == "a-company"
    assert path == cfg


def test_resolve_profile_to_config_path_missing_config_file(tmp_path):
    """registry には書かれているが config ファイル実体が無い"""
    registry_file = _write_registry(tmp_path, {
        "profiles": {"a-company": {"config": str(tmp_path / "missing.yaml")}}
    })
    with pytest.raises(ProfileError, match="config file"):
        resolve_profile_to_config_path("a-company", registry_file)


def test_resolve_profile_to_config_path_invalid_name(tmp_path):
    registry_file = _write_registry(tmp_path, {"profiles": {}})
    with pytest.raises(InvalidProfileNameError):
        resolve_profile_to_config_path("BadName", registry_file)


def test_resolve_profile_to_config_path_no_registry(tmp_path):
    with pytest.raises(ProfileRegistryNotFoundError):
        resolve_profile_to_config_path(
            "a-company", tmp_path / "no-registry.yaml"
        )


# ---------------------------------------------------------------------------
# Phase C: data_dir 自動補完
# ---------------------------------------------------------------------------


def test_phase_c_autocomplete_paths_from_registry_data_dir(tmp_path, monkeypatch):
    """registry の data_dir が config に補完される（config で未指定の場合）"""
    from hokusai.config import create_config_from_env_and_file

    data_dir = tmp_path / "profile-a-data"

    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp/repo-a\n")
    registry = tmp_path / "profiles.yaml"
    registry.write_text(yaml.safe_dump({
        "profiles": {
            "a-co": {
                "config": str(cfg),
                "data_dir": str(data_dir),
            }
        }
    }))
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry))

    config = create_config_from_env_and_file(profile_name="a-co")
    assert config.data_dir == data_dir
    assert config.database_path == data_dir / "workflow.db"
    assert config.checkpoint_db_path == data_dir / "checkpoint.db"
    assert config.worktree_root == data_dir / "worktrees"


def test_phase_c_config_file_overrides_registry_data_dir(tmp_path, monkeypatch):
    """config file 内で明示された path は registry の data_dir より優先"""
    from hokusai.config import create_config_from_env_and_file

    data_dir_registry = tmp_path / "registry-data"
    db_explicit = tmp_path / "explicit-workflow.db"

    cfg = tmp_path / "a.yaml"
    cfg.write_text(
        f"project_root: /tmp/repo-a\n"
        f"database_path: {db_explicit}\n"
    )
    registry = tmp_path / "profiles.yaml"
    registry.write_text(yaml.safe_dump({
        "profiles": {
            "a-co": {
                "config": str(cfg),
                "data_dir": str(data_dir_registry),
            }
        }
    }))
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry))

    config = create_config_from_env_and_file(profile_name="a-co")
    # config file の database_path が registry の data_dir 補完より優先される
    assert config.database_path == db_explicit
    # 明示されていない他のフィールドは registry data_dir から補完される
    assert config.checkpoint_db_path == data_dir_registry / "checkpoint.db"
    assert config.worktree_root == data_dir_registry / "worktrees"


def test_phase_c_no_registry_data_dir_no_autocomplete(tmp_path, monkeypatch):
    """registry に data_dir が無ければ補完されない（既存 WorkflowConfig デフォルト）"""
    from hokusai.config import create_config_from_env_and_file

    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp/repo-a\n")
    registry = tmp_path / "profiles.yaml"
    registry.write_text(yaml.safe_dump({
        "profiles": {"a-co": {"config": str(cfg)}}
    }))
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry))

    config = create_config_from_env_and_file(profile_name="a-co")
    # WorkflowConfig のデフォルト（~/.hokusai/...）が使われる
    # registry の data_dir 補完は走らない
    assert ".hokusai" in str(config.data_dir)


def test_phase_c_paths_expanduser_in_config_file(tmp_path, monkeypatch):
    """config file の database_path / checkpoint_db_path に ~ が含まれても展開される"""
    from hokusai.config import create_config_from_env_and_file

    cfg = tmp_path / "a.yaml"
    cfg.write_text(
        "project_root: /tmp/repo\n"
        f"database_path: {tmp_path}/expanded.db\n"
        f"checkpoint_db_path: {tmp_path}/expanded-cp.db\n"
    )

    config = create_config_from_env_and_file(str(cfg))
    assert config.database_path == tmp_path / "expanded.db"
    assert config.checkpoint_db_path == tmp_path / "expanded-cp.db"


def test_phase_c_parent_directories_created(tmp_path, monkeypatch):
    """補完された path の親ディレクトリが自動作成される"""
    from hokusai.config import create_config_from_env_and_file

    data_dir = tmp_path / "nested" / "deep" / "profile-data"
    assert not data_dir.exists()

    cfg = tmp_path / "a.yaml"
    cfg.write_text("project_root: /tmp/repo\n")
    registry = tmp_path / "profiles.yaml"
    registry.write_text(yaml.safe_dump({
        "profiles": {
            "a-co": {
                "config": str(cfg),
                "data_dir": str(data_dir),
            }
        }
    }))
    monkeypatch.setenv("HOKUSAI_PROFILES_FILE", str(registry))

    config = create_config_from_env_and_file(profile_name="a-co")
    # data_dir 自体が作成される
    assert data_dir.exists()
    # database_path / checkpoint_db_path の親も作成される（補完先 == data_dir なので同じ）
    assert config.database_path.parent.exists()
