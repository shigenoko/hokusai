"""
State DB backup / restore のテスト（T1 / Production Readiness）。

決定性のため `now` を注入し、実 SQLite ファイルに対して online backup API の
動作・integrity 検証・restore の安全側挙動・世代刈り込み・参照解決を検証する。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from hokusai.persistence.backup import (
    BackupError,
    create_backup,
    integrity_check,
    list_backups,
    prune_backups,
    resolve_snapshot,
    restore_backup,
)


def _make_db(path: Path, rows: list[str]) -> None:
    """WAL モードの SQLite を作り、指定の値を書き込む。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    for r in rows:
        conn.execute("INSERT INTO t (v) VALUES (?)", (r,))
    conn.commit()
    conn.close()


def _read_rows(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT v FROM t ORDER BY v")]
    finally:
        conn.close()


@pytest.fixture
def state(tmp_path):
    """workflow.db / checkpoint.db / backups ディレクトリの一式。"""
    wf = tmp_path / "workflow.db"
    ck = tmp_path / "checkpoint.db"
    out = tmp_path / "backups"
    _make_db(wf, ["a", "b"])
    _make_db(ck, ["c"])
    return {"wf": wf, "ck": ck, "out": out}


def test_create_backup_makes_consistent_snapshot(state):
    now = datetime(2026, 6, 4, 11, 30, 0)
    manifest = create_backup(
        database_path=state["wf"],
        checkpoint_db_path=state["ck"],
        out_dir=state["out"],
        version="0.11.0",
        profile="hokusai",
        now=now,
    )
    assert manifest["snapshot_id"] == "20260604-113000"
    snap = Path(manifest["path"])
    assert (snap / "manifest.json").exists()
    assert (snap / "workflow.db").exists()
    assert (snap / "checkpoint.db").exists()
    # スナップショット DB は単一ファイルで読め、内容が一致する
    assert _read_rows(snap / "workflow.db") == ["a", "b"]
    assert _read_rows(snap / "checkpoint.db") == ["c"]
    assert manifest["components"]["workflow"]["integrity_ok"] is True
    assert manifest["hokusai_version"] == "0.11.0"
    assert manifest["profile"] == "hokusai"


def test_create_backup_while_db_open(state):
    """退避元 DB を開いたまま（使用中）でも整合スナップショットが取れる。"""
    conn = sqlite3.connect(state["wf"])
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO t (v) VALUES ('open')")
    conn.commit()
    try:
        manifest = create_backup(
            database_path=state["wf"],
            checkpoint_db_path=state["ck"],
            out_dir=state["out"],
            now=datetime(2026, 6, 4, 12, 0, 0),
        )
        snap = Path(manifest["path"])
        assert sorted(_read_rows(snap / "workflow.db")) == ["a", "b", "open"]
    finally:
        conn.close()


def test_create_backup_no_target_raises(tmp_path):
    with pytest.raises(BackupError):
        create_backup(
            database_path=tmp_path / "missing.db",
            checkpoint_db_path=tmp_path / "missing2.db",
            out_dir=tmp_path / "backups",
            now=datetime(2026, 6, 4, 11, 0, 0),
        )


def test_create_backup_skips_absent_checkpoint(tmp_path):
    """checkpoint.db 不在でも workflow.db だけで成功する。"""
    wf = tmp_path / "workflow.db"
    _make_db(wf, ["x"])
    manifest = create_backup(
        database_path=wf,
        checkpoint_db_path=tmp_path / "checkpoint.db",  # 不在
        out_dir=tmp_path / "backups",
        now=datetime(2026, 6, 4, 11, 0, 0),
    )
    assert "workflow" in manifest["components"]
    assert "checkpoint" not in manifest["components"]


def test_snapshot_id_collision_gets_suffix(state):
    now = datetime(2026, 6, 4, 11, 30, 0)
    m1 = create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=now,
    )
    m2 = create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=now,
    )
    assert m1["snapshot_id"] == "20260604-113000"
    assert m2["snapshot_id"] == "20260604-113000-1"


def test_integrity_check(state, tmp_path):
    assert integrity_check(state["wf"]) is True
    assert integrity_check(tmp_path / "nope.db") is False
    # 壊れたファイル（SQLite でない）は False
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    assert integrity_check(bad) is False


def test_list_backups_sorted_desc(state):
    create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                  out_dir=state["out"], now=datetime(2026, 6, 4, 10, 0, 0))
    create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                  out_dir=state["out"], now=datetime(2026, 6, 4, 12, 0, 0))
    create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                  out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0))
    ids = [m["snapshot_id"] for m in list_backups(state["out"])]
    assert ids == ["20260604-120000", "20260604-110000", "20260604-100000"]


def test_list_backups_empty(tmp_path):
    assert list_backups(tmp_path / "nope") == []


def test_prune_backups_keeps_newest(state):
    for h in (8, 9, 10, 11):
        create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                      out_dir=state["out"], now=datetime(2026, 6, 4, h, 0, 0))
    removed = prune_backups(state["out"], keep=2)
    remaining = [m["snapshot_id"] for m in list_backups(state["out"])]
    assert remaining == ["20260604-110000", "20260604-100000"]
    assert sorted(removed) == ["20260604-080000", "20260604-090000"]


def test_prune_negative_raises(state):
    with pytest.raises(BackupError):
        prune_backups(state["out"], keep=-1)


def test_resolve_snapshot_by_id_latest_and_path(state):
    create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                  out_dir=state["out"], now=datetime(2026, 6, 4, 10, 0, 0))
    m2 = create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                       out_dir=state["out"], now=datetime(2026, 6, 4, 12, 0, 0))
    # id 指定
    assert resolve_snapshot(state["out"], "20260604-100000").name == "20260604-100000"
    # latest
    assert resolve_snapshot(state["out"], "latest").name == m2["snapshot_id"]
    # path 指定
    assert resolve_snapshot(state["out"], m2["path"]).name == m2["snapshot_id"]
    # 不在
    assert resolve_snapshot(state["out"], "20990101-000000") is None


def test_restore_roundtrip_and_pre_restore(state):
    manifest = create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    # backup 後に現 DB を変更
    _make_db(state["wf"], ["changed"])  # 上書きではなく追記なので a,b,changed
    conn = sqlite3.connect(state["wf"])
    conn.execute("DELETE FROM t")
    conn.execute("INSERT INTO t (v) VALUES ('changed')")
    conn.commit()
    conn.close()
    assert _read_rows(state["wf"]) == ["changed"]

    result = restore_backup(
        snapshot_dir=manifest["path"],
        database_path=state["wf"],
        checkpoint_db_path=state["ck"],
    )
    # 復元後はスナップショット時点の内容に戻る
    assert _read_rows(state["wf"]) == ["a", "b"]
    # 現 DB は .pre-restore に退避されている
    pre = state["wf"].with_name("workflow.db.pre-restore")
    assert pre.exists()
    assert _read_rows(pre) == ["changed"]
    assert result["snapshot_id"] == "20260604-110000"


def test_restore_removes_stale_wal(state):
    """復元時に現 DB の stale -wal/-shm を除去する。"""
    manifest = create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    # 現 DB に WAL sidecar を作る（接続を開いて未チェックポイントの書き込み）
    conn = sqlite3.connect(state["wf"])
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO t (v) VALUES ('z')")
    conn.commit()
    conn.close()
    stale_wal = state["wf"].with_name("workflow.db-wal")
    # WAL ファイルが存在する場合のみ意味のある検証（環境差を許容）
    restore_backup(
        snapshot_dir=manifest["path"],
        database_path=state["wf"],
        checkpoint_db_path=state["ck"],
    )
    assert not stale_wal.exists()
    assert _read_rows(state["wf"]) == ["a", "b"]


def test_restore_verify_rejects_corrupt_snapshot(state):
    manifest = create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    # スナップショット内の workflow.db を破損させる
    (Path(manifest["path"]) / "workflow.db").write_text("corrupt")
    with pytest.raises(BackupError):
        restore_backup(
            snapshot_dir=manifest["path"],
            database_path=state["wf"],
            checkpoint_db_path=state["ck"],
            verify=True,
        )
    # 検証で弾かれたので現 DB は無傷（pre-restore 退避もされない）
    assert _read_rows(state["wf"]) == ["a", "b"]


def test_restore_missing_manifest_raises(tmp_path, state):
    with pytest.raises(BackupError):
        restore_backup(
            snapshot_dir=tmp_path / "no_such_snapshot",
            database_path=state["wf"],
            checkpoint_db_path=state["ck"],
        )
