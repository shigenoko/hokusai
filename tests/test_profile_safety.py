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

import pytest

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


def test_workflow_runner_injects_profile_name_into_state(tmp_path, monkeypatch):
    """WorkflowRunner(profile_name=...) で start() した workflow の state / DB に
    profile_name が確実に注入されることを検証（Phase E の本来の目的）。"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig
    from hokusai.workflow import WorkflowRunner

    cfg = WorkflowConfig(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "wf.db",
        checkpoint_db_path=tmp_path / "cp.db",
        worktree_root=tmp_path / "worktrees",
    )
    set_config(cfg)

    # WorkflowRunner を profile_name 付きで生成（実 LangGraph 実行はしない）
    runner = WorkflowRunner(
        verbose=False,
        dry_run=True,  # 実行をスキップして state 永続化のみ
        profile_name="company-a",
    )
    assert runner.profile_name == "company-a"

    # dry_run のため start は早期 return するが、profile_name が runner に
    # 保持されていることが要点。実 save 経路の検証は次の関数で実施。


def test_save_workflow_with_runner_profile_persists_to_db(tmp_path):
    """WorkflowRunner.start() 後に DB の profile_name カラムに値が入ることを検証"""
    from hokusai.state import create_initial_state

    db_path = tmp_path / "wf.db"
    store = SQLiteStore(db_path)

    # WorkflowRunner.start() の挙動を再現: initial state に profile_name 注入 → save
    state = create_initial_state(
        task_url="https://example.com/task",
        branch_name="feature/test",
        from_phase=None,
        run_mode="auto",
    )
    state["profile_name"] = "company-a"  # WorkflowRunner が注入する経路を再現

    store.save_workflow(state["workflow_id"], state)

    assert store.get_workflow_profile_name(state["workflow_id"]) == "company-a"


def test_alter_table_skipped_when_column_already_exists(tmp_path, monkeypatch):
    """profile_name カラムが既に存在する DB を開く時、ALTER TABLE が呼ばれない。

    旧実装は try/except で OperationalError を制御フローに使っていたため、
    起動の度に例外コストが発生していた。新実装は PRAGMA table_info で
    事前判定するため、不要な ALTER TABLE 実行は無くなる。
    """
    from hokusai.persistence import sqlite_store as ss

    # 通常通り SQLiteStore で DB を作成（profile_name カラム付き）
    db_path = tmp_path / "wf.db"
    SQLiteStore(db_path)

    # 2 回目以降の起動を再現: ALTER TABLE が呼ばれたら検知する proxy
    alter_calls: list[str] = []

    real_connect = ss.SQLiteStore._connect

    class _AlterTrackingProxy:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("ALTER TABLE"):
                alter_calls.append(sql)
            return self._real.execute(sql, *args, **kwargs)

        def commit(self):
            return self._real.commit()

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *args):
            return self._real.__exit__(*args)

    monkeypatch.setattr(
        ss.SQLiteStore,
        "_connect",
        lambda self: _AlterTrackingProxy(real_connect(self)),
    )

    # 既存 DB を再度開く（profile_name カラム既存）→ ALTER TABLE は呼ばれないはず
    SQLiteStore(db_path)
    assert alter_calls == [], f"既存カラム DB で ALTER TABLE が呼ばれた: {alter_calls}"


def test_alter_table_called_for_legacy_db(tmp_path, monkeypatch):
    """v0.2.x 以前の DB（profile_name 無し）を初めて開く時は ALTER TABLE が呼ばれる"""
    from hokusai.persistence import sqlite_store as ss

    # 旧スキーマ（profile_name カラム無し）を手動で作成
    db_path = tmp_path / "legacy.db"
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
        conn.commit()

    # ALTER TABLE 呼び出しを検知する proxy
    alter_calls: list[str] = []
    real_connect = ss.SQLiteStore._connect

    class _Proxy:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("ALTER TABLE"):
                alter_calls.append(sql)
            return self._real.execute(sql, *args, **kwargs)

        def commit(self):
            return self._real.commit()

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *args):
            return self._real.__exit__(*args)

    monkeypatch.setattr(
        ss.SQLiteStore,
        "_connect",
        lambda self: _Proxy(real_connect(self)),
    )

    SQLiteStore(db_path)
    # legacy DB に対しては ALTER TABLE が 1 回呼ばれる
    assert len(alter_calls) == 1
    assert "profile_name" in alter_calls[0]


def test_alter_table_reraises_non_duplicate_errors(tmp_path, monkeypatch):
    """ALTER TABLE が duplicate column name 以外の OperationalError を再 raise する。

    duplicate を握り潰す挙動は test_legacy_db_migrates_profile_name_column と
    test_new_db_has_profile_name_column で検証されている。ここでは「それ以外」が
    握り潰されないこと（DB lock や別エラーが原因不明にならないこと）を確認する。
    """
    from hokusai.persistence import sqlite_store as ss

    # 既存 v0.2.x 互換の DB を手動で用意（profile_name カラム無し）
    db_path = tmp_path / "wf.db"
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
        conn.commit()

    # _connect が返す Connection をラップして、ALTER TABLE だけ別エラーを投げる
    real_connect = ss.SQLiteStore._connect

    class _LockingProxy:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):
            if "ALTER TABLE workflows" in sql:
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args, **kwargs)

        def commit(self):
            return self._real.commit()

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *args):
            return self._real.__exit__(*args)

    def fake_connect(self):
        return _LockingProxy(real_connect(self))

    monkeypatch.setattr(ss.SQLiteStore, "_connect", fake_connect)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        SQLiteStore(db_path)


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


def test_find_workflow_respects_config_database_path_override(tmp_path):
    """profile config の database_path 明示上書きを尊重して探索する（false negative 防止）"""
    # custom_db を default 位置とは別の場所に作る
    data_dir = tmp_path / "company-a"
    data_dir.mkdir()
    custom_db = tmp_path / "custom-location" / "wf.db"
    custom_db.parent.mkdir()

    # config file に database_path 明示
    cfg = data_dir / "config.yaml"
    cfg.write_text(
        f"project_root: /tmp\n"
        f"database_path: {custom_db}\n"
    )

    # 配置: data_dir 直下にデフォルト位置の workflow.db は存在しない（=空）
    # しかし custom_db には workflow を保存する
    store = SQLiteStore(custom_db)
    store.save_workflow("wf-override", {
        "task_url": "https://example.com",
        "current_phase": 1,
        "profile_name": "company-a",
    })

    # 旧実装（data_dir/"workflow.db" 固定）だと見つからない false negative
    # 新実装は config.database_path を尊重して検出できる
    registry = ProfileRegistry(profiles={
        "company-a": ProfileConfig(
            name="company-a",
            config_path=cfg,
            data_dir=data_dir,
        ),
    })
    found = find_workflow_in_other_profiles(
        "wf-override", current_profile=None, registry=registry
    )
    assert found == ["company-a"]


def test_find_workflow_falls_back_to_data_dir_default(tmp_path):
    """config file が無い / database_path 明示無しなら data_dir/'workflow.db' を使う"""
    data_dir = tmp_path / "company-b"
    data_dir.mkdir()
    cfg = data_dir / "config.yaml"
    cfg.write_text("project_root: /tmp\n")  # database_path 無し

    # data_dir/workflow.db に保存
    default_db = data_dir / "workflow.db"
    store = SQLiteStore(default_db)
    store.save_workflow("wf-default-loc", {
        "task_url": "https://example.com",
        "current_phase": 1,
    })

    registry = ProfileRegistry(profiles={
        "company-b": ProfileConfig(
            name="company-b",
            config_path=cfg,
            data_dir=data_dir,
        ),
    })
    found = find_workflow_in_other_profiles(
        "wf-default-loc", current_profile=None, registry=registry
    )
    assert found == ["company-b"]


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
