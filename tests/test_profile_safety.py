"""Phase E: 誤操作防止と診断のテスト

対象:
- SQLiteStore.workflow_exists / get_workflow_profile_name
- find_workflow_in_other_profiles（他 profile への横断探索）
- 既存 v0.2.x DB（profile_name カラム無し）の互換性
- workflow state への profile_name 保存
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hokusai.config import (
    ProfileConfig,
    ProfileRegistry,
    find_workflow_in_other_profiles,
)
from hokusai.persistence.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# SQLiteStore: profile_name カラム追加とマイグレーション
# ---------------------------------------------------------------------------


def test_new_db_has_profile_name_column(tmp_path):
    """v0.3.0 で新規作成された DB は最初から profile_name カラムを持つ"""
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(workflows)")
        columns = [row[1] for row in cursor.fetchall()]
    assert "profile_name" in columns


def test_legacy_db_migrates_profile_name_column(tmp_path):
    """v0.2.x 以前の DB（profile_name 無し）も ALTER TABLE で追加される"""
    db_path = tmp_path / "legacy.db"

    # 旧スキーマ（profile_name カラム無し）を手動で作成
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE workflows (
                workflow_id TEXT PRIMARY KEY,
                task_url TEXT NOT NULL,
                task_title TEXT,
                branch_name TEXT,
                current_phase INTEGER DEFAULT 1,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO workflows (workflow_id, task_url, state_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("legacy-wf-1", "https://example.com", "{}", "2026-04-01T00:00:00", "2026-04-01T00:00:00"),
        )
        conn.commit()

    # SQLiteStore で開く → マイグレーション発火
    store = SQLiteStore(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info(workflows)")
        columns = [row[1] for row in cursor.fetchall()]
    assert "profile_name" in columns

    # legacy 行は profile_name=NULL
    assert store.get_workflow_profile_name("legacy-wf-1") is None
    # 行は失われない
    assert store.workflow_exists("legacy-wf-1")


def test_save_workflow_persists_profile_name(tmp_path):
    """state に profile_name があれば DB に書き込まれる"""
    db_path = tmp_path / "wf.db"
    store = SQLiteStore(db_path)
    state = {
        "task_url": "https://example.com/task",
        "task_title": "Test",
        "profile_name": "company-a",
        "current_phase": 1,
    }
    store.save_workflow("wf-001", state)
    assert store.get_workflow_profile_name("wf-001") == "company-a"


def test_save_workflow_without_profile_name(tmp_path):
    """state に profile_name が無くても保存は成功し、profile_name は NULL"""
    db_path = tmp_path / "wf.db"
    store = SQLiteStore(db_path)
    state = {"task_url": "https://example.com/task", "current_phase": 1}
    store.save_workflow("wf-002", state)
    assert store.get_workflow_profile_name("wf-002") is None
    assert store.workflow_exists("wf-002")


def test_update_workflow_preserves_profile_name(tmp_path):
    """既存 row の更新時に profile_name が含まれなくても、既存値を保持する"""
    db_path = tmp_path / "wf.db"
    store = SQLiteStore(db_path)
    # 初回: profile_name 指定
    store.save_workflow("wf-003", {
        "task_url": "https://example.com",
        "profile_name": "company-a",
        "current_phase": 1,
    })
    # 更新: profile_name 無し
    store.save_workflow("wf-003", {
        "task_url": "https://example.com",
        "current_phase": 5,
    })
    # 既存値が保持される
    assert store.get_workflow_profile_name("wf-003") == "company-a"


def test_workflow_exists_returns_true_for_existing(tmp_path):
    db_path = tmp_path / "wf.db"
    store = SQLiteStore(db_path)
    store.save_workflow("wf-here", {"task_url": "x", "current_phase": 1})
    assert store.workflow_exists("wf-here") is True


def test_workflow_exists_returns_false_for_missing(tmp_path):
    db_path = tmp_path / "wf.db"
    store = SQLiteStore(db_path)
    assert store.workflow_exists("nonexistent") is False


# ---------------------------------------------------------------------------
# find_workflow_in_other_profiles
# ---------------------------------------------------------------------------


def _make_registry_with_dbs(tmp_path: Path, profile_workflows: dict[str, list[str]]) -> ProfileRegistry:
    """テスト用に profile data_dir + DB を作成し、ProfileRegistry を返す"""
    profiles: dict[str, ProfileConfig] = {}
    for name, workflow_ids in profile_workflows.items():
        data_dir = tmp_path / name
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "workflow.db"
        store = SQLiteStore(db_path)
        for wf_id in workflow_ids:
            store.save_workflow(wf_id, {
                "task_url": f"https://example.com/{wf_id}",
                "current_phase": 1,
                "profile_name": name,
            })
        # registry には config_path も必要なので dummy で作る
        cfg = data_dir / "config.yaml"
        cfg.write_text(f"project_root: /tmp/{name}\n")
        profiles[name] = ProfileConfig(
            name=name,
            config_path=cfg,
            data_dir=data_dir,
        )
    return ProfileRegistry(profiles=profiles)


def test_find_workflow_in_other_profile(tmp_path):
    """current profile に無い workflow が他 profile に存在することを検出"""
    registry = _make_registry_with_dbs(tmp_path, {
        "company-a": ["wf-aaa"],
        "company-b": ["wf-bbb"],
    })
    found = find_workflow_in_other_profiles(
        "wf-bbb", current_profile="company-a", registry=registry
    )
    assert found == ["company-b"]


def test_find_workflow_not_found_anywhere(tmp_path):
    """どの profile にも存在しない workflow"""
    registry = _make_registry_with_dbs(tmp_path, {
        "company-a": ["wf-aaa"],
        "company-b": ["wf-bbb"],
    })
    found = find_workflow_in_other_profiles(
        "wf-missing", current_profile="company-a", registry=registry
    )
    assert found == []


def test_find_workflow_excludes_current_profile(tmp_path):
    """current_profile にある workflow は含めない"""
    registry = _make_registry_with_dbs(tmp_path, {
        "company-a": ["wf-shared"],
        "company-b": ["wf-shared"],
    })
    found = find_workflow_in_other_profiles(
        "wf-shared", current_profile="company-a", registry=registry
    )
    assert found == ["company-b"]


def test_find_workflow_multiple_profiles_match(tmp_path):
    """複数 profile に同じ workflow_id があれば全て列挙"""
    registry = _make_registry_with_dbs(tmp_path, {
        "company-a": ["wf-everywhere"],
        "company-b": ["wf-everywhere"],
        "company-c": ["wf-everywhere"],
    })
    found = find_workflow_in_other_profiles(
        "wf-everywhere", current_profile="company-a", registry=registry
    )
    assert sorted(found) == ["company-b", "company-c"]


def test_find_workflow_handles_missing_data_dir(tmp_path):
    """data_dir が無い profile はスキップ（エラーにしない）"""
    registry = ProfileRegistry(profiles={
        "no-data-dir": ProfileConfig(
            name="no-data-dir",
            config_path=tmp_path / "any.yaml",
            data_dir=None,
        ),
    })
    found = find_workflow_in_other_profiles(
        "wf-x", current_profile=None, registry=registry
    )
    assert found == []


def test_find_workflow_handles_missing_db_file(tmp_path):
    """data_dir はあるが workflow.db が無い場合もスキップ"""
    data_dir = tmp_path / "no-db"
    data_dir.mkdir()
    cfg = data_dir / "config.yaml"
    cfg.write_text("project_root: /tmp\n")
    registry = ProfileRegistry(profiles={
        "no-db": ProfileConfig(
            name="no-db",
            config_path=cfg,
            data_dir=data_dir,
        ),
    })
    found = find_workflow_in_other_profiles(
        "wf-x", current_profile=None, registry=registry
    )
    assert found == []


def test_find_workflow_handles_corrupt_db(tmp_path, monkeypatch):
    """他 profile の DB が壊れていても current profile の操作は止めない"""
    data_dir = tmp_path / "broken-co"
    data_dir.mkdir()
    cfg = data_dir / "config.yaml"
    cfg.write_text("project_root: /tmp\n")
    # 壊れた DB ファイル
    broken_db = data_dir / "workflow.db"
    broken_db.write_bytes(b"not a sqlite file")

    registry = ProfileRegistry(profiles={
        "broken-co": ProfileConfig(
            name="broken-co",
            config_path=cfg,
            data_dir=data_dir,
        ),
    })
    # 例外を投げずに空 list を返す
    found = find_workflow_in_other_profiles(
        "wf-x", current_profile=None, registry=registry
    )
    assert found == []
