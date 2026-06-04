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


def test_list_backups_numeric_suffix_order(state):
    """同一秒衝突で suffix が 10 以上になっても numeric 順で正しく並ぶ。

    辞書順ソートだと `...-10` が `...-2` より古い扱いになる罠（Copilot 指摘）の
    回帰テスト。
    """
    now = datetime(2026, 6, 4, 11, 30, 0)
    # 同一秒に 11 個作る → base, base-1, ..., base-10
    for _ in range(11):
        create_backup(
            database_path=state["wf"], checkpoint_db_path=state["ck"],
            out_dir=state["out"], now=now,
        )
    listed = [m["snapshot_id"] for m in list_backups(state["out"])]
    # 新しい順: suffix 10 が先頭、suffix なし（=0）が末尾
    assert listed[0] == "20260604-113000-10"
    assert listed[-1] == "20260604-113000"
    # 辞書順なら "-2" が "-10" より後ろに来るが、numeric key で -10 が先
    assert listed.index("20260604-113000-10") < listed.index("20260604-113000-2")


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


def test_prune_propagates_delete_failure(state, monkeypatch):
    """削除に失敗したら成功扱いにせず BackupError を伝播する（Copilot 指摘）。"""
    for h in (8, 9, 10):
        create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                      out_dir=state["out"], now=datetime(2026, 6, 4, h, 0, 0))

    def _boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr("hokusai.persistence.backup.shutil.rmtree", _boom)
    with pytest.raises(BackupError):
        prune_backups(state["out"], keep=1)


def test_prune_skips_tampered_snapshot_id(state):
    """manifest の snapshot_id が out_dir 直下の正規 dir を指さないなら触らない。"""
    create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                  out_dir=state["out"], now=datetime(2026, 6, 4, 9, 0, 0))
    snap = create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                         out_dir=state["out"], now=datetime(2026, 6, 4, 10, 0, 0))
    # 古い方の manifest の snapshot_id を path traversal 風に改竄
    old_dir = state["out"] / "20260604-090000"
    manifest_path = old_dir / "manifest.json"
    import json as _json
    data = _json.loads(manifest_path.read_text())
    data["snapshot_id"] = "../evil"
    manifest_path.write_text(_json.dumps(data))
    # keep=1 で古い方が刈り込み対象になるが、改竄 id は触らず removed に載らない
    removed = prune_backups(state["out"], keep=1)
    assert removed == []
    # out_dir 自体・新しいスナップショットは無傷
    assert state["out"].is_dir()
    assert (state["out"] / snap["snapshot_id"]).is_dir()


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


def test_backup_handles_path_with_special_chars(tmp_path):
    """スペース等の予約文字を含むパスでも as_uri 経由で接続が壊れない。"""
    base = tmp_path / "with space #and?weird"
    wf = base / "workflow.db"
    ck = base / "checkpoint.db"
    _make_db(wf, ["x"])
    _make_db(ck, ["y"])
    manifest = create_backup(
        database_path=wf, checkpoint_db_path=ck,
        out_dir=base / "back ups",
        now=datetime(2026, 6, 4, 11, 0, 0),
    )
    assert manifest["components"]["workflow"]["integrity_ok"] is True
    assert _read_rows(Path(manifest["path"]) / "workflow.db") == ["x"]


def test_create_backup_mkdir_failure_raises_backup_error(state, monkeypatch):
    """snapshot_dir 作成失敗（OSError）を BackupError 化する。"""
    import pathlib

    orig = pathlib.Path.mkdir

    def boom(self, *a, **k):
        if self.parent == state["out"]:
            raise OSError("permission denied")
        return orig(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "mkdir", boom)
    with pytest.raises(BackupError):
        create_backup(
            database_path=state["wf"], checkpoint_db_path=state["ck"],
            out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
        )


def test_create_backup_manifest_write_failure_cleans_up(state, monkeypatch):
    """manifest 書き込み失敗時は BackupError + 中途半端なスナップショットを残さない。"""
    import pathlib

    orig = pathlib.Path.write_text

    def boom(self, *a, **k):
        if self.name == "manifest.json":
            raise OSError("disk full")
        return orig(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    with pytest.raises(BackupError):
        create_backup(
            database_path=state["wf"], checkpoint_db_path=state["ck"],
            out_dir=state["out"], now=datetime(2026, 6, 4, 11, 30, 0),
        )
    # DB だけ残った中途半端なスナップショットが残っていない
    assert not (state["out"] / "20260604-113000").exists()


def test_resolve_latest_ignores_tampered_manifest_path(state):
    """latest 解決は manifest の path を信頼せず snapshot_id から再構成する。"""
    import json as _json

    create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    # snapshot_id は now から決定的。fixture 由来パス + literal で組み立てて
    # taint 解析の path-injection 誤検知（関数戻り値からのパス構築）を避ける。
    snap_dir = state["out"] / "20260604-110000"
    mpath = snap_dir / "manifest.json"
    data = _json.loads(mpath.read_text())
    data["path"] = "/etc"  # 改竄
    mpath.write_text(_json.dumps(data))
    resolved = resolve_snapshot(state["out"], "latest")
    # /etc ではなく out_dir 直下の実ディレクトリに解決される
    assert resolved == snap_dir.resolve()


def test_restore_rejects_tampered_component_file(state):
    """manifest の component file が traversal 風なら BackupError（KeyError でなく）。"""
    import json as _json

    create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    snap_dir = state["out"] / "20260604-110000"
    mpath = snap_dir / "manifest.json"
    data = _json.loads(mpath.read_text())
    data["components"]["workflow"]["file"] = "../../evil.db"
    mpath.write_text(_json.dumps(data))
    with pytest.raises(BackupError):
        restore_backup(
            snapshot_dir=snap_dir,
            database_path=state["wf"],
            checkpoint_db_path=state["ck"],
        )


def test_read_manifest_handles_invalid_utf8(state):
    """manifest が不正な UTF-8 でも list_backups が落ちずスキップする。"""
    create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    mpath = state["out"] / "20260604-110000" / "manifest.json"
    mpath.write_bytes(b"\xff\xfe invalid utf-8 \x80")
    # 例外を投げず、壊れた manifest はスキップされる
    assert list_backups(state["out"]) == []
    assert resolve_snapshot(state["out"], "latest") is None


def test_read_manifest_rejects_non_dict_json_root(state):
    """manifest が dict 以外の有効 JSON（[] / "text"）でも list が落ちない。"""
    create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    mpath = state["out"] / "20260604-110000" / "manifest.json"
    mpath.write_text("[]")  # 有効 JSON だが dict ではない
    # AttributeError を出さず、dict でない manifest はスキップされる
    assert list_backups(state["out"]) == []
    assert resolve_snapshot(state["out"], "latest") is None


def test_resolve_latest_takes_precedence_over_cwd_path(state, tmp_path, monkeypatch):
    """予約語 latest はパス解決より優先（cwd の latest/ に引っ張られない）。"""
    create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    # cwd に紛らわしい `latest/manifest.json` を置く
    work = tmp_path / "cwd"
    decoy = work / "latest"
    decoy.mkdir(parents=True)
    (decoy / "manifest.json").write_text('{"snapshot_id": "decoy"}')
    monkeypatch.chdir(work)
    resolved = resolve_snapshot(state["out"], "latest")
    # decoy ではなく out_dir 内の最新に解決される
    assert resolved == (state["out"] / "20260604-110000").resolve()


def test_non_str_snapshot_id_does_not_crash_list_prune_resolve(state):
    """snapshot_id が非 str（数値 / null）に改竄されても落ちない。"""
    import json as _json

    # 正常な 1 件 + snapshot_id を改竄した 1 件
    create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                  out_dir=state["out"], now=datetime(2026, 6, 4, 10, 0, 0))
    create_backup(database_path=state["wf"], checkpoint_db_path=state["ck"],
                  out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0))
    bad = state["out"] / "20260604-110000" / "manifest.json"
    data = _json.loads(bad.read_text())
    data["snapshot_id"] = 12345  # 非 str に改竄
    bad.write_text(_json.dumps(data))

    # list / resolve / prune のいずれも例外を投げない
    listed = list_backups(state["out"])  # ソートで .split() 例外にならない
    assert len(listed) == 2
    assert resolve_snapshot(state["out"], "latest") is not None
    # prune も TypeError にならず、非 str の方は触らない
    removed = prune_backups(state["out"], keep=0)
    assert "20260604-100000" in removed


def test_restore_rollback_preserves_original_wal(state, monkeypatch):
    """復元失敗時、元 DB の WAL/SHM sidecar も削除でなく退避され巻き戻る。"""
    snap = create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    # 現 workflow.db の sidecar を sentinel として用意（中身を識別可能に）
    wal = state["wf"].with_name("workflow.db-wal")
    shm = state["wf"].with_name("workflow.db-shm")
    wal.write_bytes(b"ORIGINAL-WAL")
    shm.write_bytes(b"ORIGINAL-SHM")

    import hokusai.persistence.backup as bk
    real_replace = bk.os.replace

    def flaky_replace(src, dst, *a, **k):
        # 2 つ目（checkpoint.db）の配置だけ失敗させる
        if str(dst).endswith("checkpoint.db"):
            raise OSError("simulated failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(bk.os, "replace", flaky_replace)
    with pytest.raises(BackupError):
        restore_backup(
            snapshot_dir=snap["path"],
            database_path=state["wf"], checkpoint_db_path=state["ck"],
        )
    # ロールバックで元の WAL/SHM が削除されず復元されている
    assert wal.exists() and wal.read_bytes() == b"ORIGINAL-WAL"
    assert shm.exists() and shm.read_bytes() == b"ORIGINAL-SHM"
    # 退避用の中間ファイルは残らない
    assert not state["wf"].with_name("workflow.db.pre-restore-wal").exists()


def test_restore_distinguishes_broken_vs_missing_manifest(state, tmp_path):
    """manifest 破損と不在でエラーメッセージを区別する。"""
    create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    snap_dir = state["out"] / "20260604-110000"
    # 破損: manifest は存在するが JSON 不正
    (snap_dir / "manifest.json").write_text("{ broken json")
    with pytest.raises(BackupError, match="読めません"):
        restore_backup(
            snapshot_dir=snap_dir,
            database_path=state["wf"], checkpoint_db_path=state["ck"],
        )
    # 不在
    with pytest.raises(BackupError, match="不在"):
        restore_backup(
            snapshot_dir=tmp_path / "nope",
            database_path=state["wf"], checkpoint_db_path=state["ck"],
        )


def test_restore_two_phase_rollback_on_second_component_failure(state, monkeypatch):
    """2 つ目のコンポーネントで os.replace が失敗しても片肺にならず全て巻き戻る。"""
    snap = create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    # 現 DB を両方とも MUTATED に変える
    for p, v in ((state["wf"], "MUT-wf"), (state["ck"], "MUT-ck")):
        c = sqlite3.connect(p)
        c.execute("DELETE FROM t")
        c.execute("INSERT INTO t (v) VALUES (?)", (v,))
        c.commit()
        c.close()

    import hokusai.persistence.backup as bk
    real_replace = bk.os.replace

    def flaky_replace(src, dst, *a, **k):
        # 2 つ目（checkpoint.db）の配置だけ失敗させる
        if str(dst).endswith("checkpoint.db"):
            raise OSError("simulated failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(bk.os, "replace", flaky_replace)

    with pytest.raises(BackupError):
        restore_backup(
            snapshot_dir=snap["path"],
            database_path=state["wf"], checkpoint_db_path=state["ck"],
        )

    # 片肺にならず、両 DB とも MUTATED のまま（ロールバック済み）
    assert _read_rows(state["wf"]) == ["MUT-wf"]
    assert _read_rows(state["ck"]) == ["MUT-ck"]
    # 一時ファイルが残っていない
    assert not state["wf"].with_name("workflow.db.restore-tmp").exists()
    assert not state["ck"].with_name("checkpoint.db.restore-tmp").exists()


def test_handle_backup_rejects_negative_keep_before_create(state):
    """CLI: --keep 負値は作成前にエラー終了し、snapshot を増やさない（原子性）。"""
    from types import SimpleNamespace

    from hokusai.cli_main import _handle_backup

    cfg = SimpleNamespace(
        data_dir=state["out"].parent,
        database_path=state["wf"],
        checkpoint_db_path=state["ck"],
    )
    args = SimpleNamespace(
        out=str(state["out"]), label=None, keep=-1,
        list=False, output="text", dry_run=False, profile=None,
    )
    rc = _handle_backup(args, cfg)
    assert rc == 1
    # 失敗したのに snapshot が増える、が起きていない
    assert list_backups(state["out"]) == []


def test_restore_rejects_non_dict_components(state):
    """components が dict でない壊れた manifest は BackupError。"""
    import json as _json

    create_backup(
        database_path=state["wf"], checkpoint_db_path=state["ck"],
        out_dir=state["out"], now=datetime(2026, 6, 4, 11, 0, 0),
    )
    snap_dir = state["out"] / "20260604-110000"
    mpath = snap_dir / "manifest.json"
    data = _json.loads(mpath.read_text())
    data["components"] = ["not", "a", "dict"]
    mpath.write_text(_json.dumps(data))
    with pytest.raises(BackupError):
        restore_backup(
            snapshot_dir=snap_dir,
            database_path=state["wf"],
            checkpoint_db_path=state["ck"],
        )
