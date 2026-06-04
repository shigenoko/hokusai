"""
State DB backup / restore

HOKUSAI の state（`workflow.db` / `checkpoint.db`）を SQLite の online backup
API で整合スナップショットとして退避・復元する純ロジック。

設計方針:
- **online backup API**（`sqlite3.Connection.backup()`）を使う。単純な
  ファイルコピーは WAL 未チェックポイント状態で壊れた DB を掴むため使わない。
  backup API は使用中の DB からでも単一ファイルの整合スナップショットを生成する。
- **副作用は明示的**。スナップショットは `<out_dir>/<snapshot_id>/` 配下に
  `workflow.db` / `checkpoint.db` / `manifest.json` を置く。
- **restore は安全側**。適用前にスナップショットの `integrity_check` を行い、
  現 DB を `<db>.pre-restore` に退避し、stale な `-wal` / `-shm` を除去してから
  差し替える。
- I/O は実ファイルに対して行うが、`now`（タイムスタンプ）を注入可能にして
  テストの決定性を確保する。

このモジュールは CLI（`hokusai backup` / `hokusai restore`）から呼ばれるが、
HOKUSAI workflow 本体の挙動には一切影響しない（state を読むだけ / 別ファイルに
書くだけ）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"

#: スナップショットに含める論理コンポーネント名 → config 上の属性名。
#: 値は CLI 側で `config.<attr>` を解決して渡す。
COMPONENTS = ("workflow", "checkpoint")


class BackupError(Exception):
    """backup / restore の操作的失敗（typed）。"""


def _snapshot_id(now: datetime) -> str:
    """`YYYYMMDD-HHMMSS` 形式のスナップショット ID を返す。"""
    return now.strftime("%Y%m%d-%H%M%S")


def _snapshot_sort_key(snapshot_id: Any) -> tuple[str, int]:
    """snapshot_id を (timestamp, numeric_suffix) に分解したソートキーを返す。

    snapshot_id はベースが `YYYYMMDD-HHMMSS`（`-` を 1 つ含む）で、同一秒衝突時は
    `-N`（N は整数）が付く。文字列降順でそのまま並べると `...-10` が `...-2` より
    古い扱いになる（辞書順の罠）ため、suffix を整数として比較する。

    manifest は改竄され得る untrusted input なので、`snapshot_id` が str 以外
    （None / 数値 等）の場合は最小キー扱いにして `.split()` 例外を避ける。
    """
    if not isinstance(snapshot_id, str):
        return ("", 0)
    parts = snapshot_id.split("-")
    # ベース ts 部分は最初の 2 要素（YYYYMMDD, HHMMSS）。3 要素目があり数値なら suffix。
    if len(parts) >= 3 and parts[2].isdigit():
        return ("-".join(parts[:2]), int(parts[2]))
    return (snapshot_id, 0)


def _ro_uri(db_path: Path) -> str:
    """read-only 接続用の URI を返す。

    URI 構築は `Path.as_uri()` を使う（スペース / `#` / `?` 等の予約文字を
    percent-encode して silent な接続失敗を防ぐ。`ReadOnlyStore` と同方針＝
    hokusai/operations.py）。
    """
    return f"{db_path.resolve().as_uri()}?mode=ro"


def integrity_check(db_path: str | Path) -> bool:
    """`PRAGMA integrity_check` が ok を返すかを read-only で確認する。

    DB が存在しない / 開けない場合は False。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return False
    conn = None
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    except (sqlite3.Error, ValueError, OSError):
        return False
    finally:
        if conn is not None:
            conn.close()


def _online_backup(src: Path, dst: Path) -> None:
    """`src` の整合スナップショットを `dst` に online backup API で作成する。

    `src` は read-only で開く。`dst` は単一ファイル DB として書かれる
    （WAL は残らない）。
    """
    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(_ro_uri(src), uri=True)
        dst_conn = sqlite3.connect(dst)
        with dst_conn:
            src_conn.backup(dst_conn)
    except (sqlite3.Error, ValueError, OSError) as e:
        raise BackupError(f"backup に失敗しました ({src}): {e}") from e
    finally:
        if src_conn is not None:
            src_conn.close()
        if dst_conn is not None:
            dst_conn.close()


def _component_paths(database_path: str | Path, checkpoint_db_path: str | Path) -> dict[str, Path]:
    """論理コンポーネント名 → 実 DB パスの対応を返す。"""
    return {
        "workflow": Path(database_path),
        "checkpoint": Path(checkpoint_db_path),
    }


def create_backup(
    *,
    database_path: str | Path,
    checkpoint_db_path: str | Path,
    out_dir: str | Path,
    label: str | None = None,
    version: str | None = None,
    profile: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """state DB の整合スナップショットを作成し manifest を書き出す。

    Args:
        database_path: workflow.db のパス。
        checkpoint_db_path: checkpoint.db のパス。
        out_dir: スナップショットを格納する親ディレクトリ。
        label: manifest に残す任意のメモ。
        version: HOKUSAI バージョン（manifest 記録用）。
        profile: profile 名（manifest 記録用）。
        now: タイムスタンプ（テスト注入用）。省略時は datetime.now()。

    Returns:
        manifest dict（snapshot_id / path / components / ... を含む）。

    Raises:
        BackupError: 退避対象 DB が 1 つも存在しない場合、backup に失敗した場合。
    """
    now = now or datetime.now()
    out_dir = Path(out_dir)
    snapshot_id = _snapshot_id(now)

    snapshot_dir = out_dir / snapshot_id
    # 同一秒に複数回呼ばれた場合の衝突回避（決定的に suffix を付ける）。
    if snapshot_dir.exists():
        suffix = 1
        while (out_dir / f"{snapshot_id}-{suffix}").exists():
            suffix += 1
        snapshot_id = f"{snapshot_id}-{suffix}"
        snapshot_dir = out_dir / snapshot_id

    paths = _component_paths(database_path, checkpoint_db_path)
    present = {name: p for name, p in paths.items() if p.exists()}
    if not present:
        raise BackupError(
            "退避対象の DB が見つかりません "
            f"(workflow={paths['workflow']} / checkpoint={paths['checkpoint']})"
        )

    try:
        snapshot_dir.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        raise BackupError(
            f"スナップショットディレクトリを作成できません ({snapshot_dir}): {e}"
        ) from e

    components: dict[str, Any] = {}
    try:
        for name, src in present.items():
            dst = snapshot_dir / f"{name}.db"
            _online_backup(src, dst)
            components[name] = {
                "source": str(src),
                "file": dst.name,
                "size_bytes": dst.stat().st_size,
                "integrity_ok": integrity_check(dst),
            }

        manifest = {
            "snapshot_id": snapshot_id,
            "created_at": now.isoformat(),
            "hokusai_version": version,
            "profile": profile,
            "label": label,
            "components": components,
            "path": str(snapshot_dir),
        }
        # manifest 書き込み失敗も「DB だけ残った中途半端なスナップショット」を
        # 生むため、DB コピーと同じ try で包んで失敗時に snapshot_dir を掃除する。
        (snapshot_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except BaseException as e:
        # 途中失敗時は中途半端なスナップショットを残さない。
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        # OSError（manifest 書き込み等）は操作コマンドとして BackupError 化する
        # （_online_backup の sqlite/OSError は既に BackupError）。
        if isinstance(e, OSError):
            raise BackupError(
                f"スナップショットの書き込みに失敗しました ({snapshot_dir}): {e}"
            ) from e
        raise

    return manifest


def _read_manifest(snapshot_dir: Path) -> dict[str, Any] | None:
    """スナップショットの manifest を読む。無ければ None。"""
    mpath = snapshot_dir / MANIFEST_NAME
    if not mpath.exists():
        return None
    try:
        data = json.loads(mpath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # manifest は改竄・破損し得る untrusted ファイル。JSON 不正・encoding
        # 破損・I/O 失敗のいずれも安全に None へ倒す（一貫した扱い）。
        return None
    # ルートが dict 以外（[] / "text" / 数値 等）の有効 JSON も untrusted として
    # None 扱いにする（呼び出し側の m.get(...) で AttributeError にしない）。
    if not isinstance(data, dict):
        return None
    return data


def list_backups(out_dir: str | Path) -> list[dict[str, Any]]:
    """`out_dir` 配下のスナップショットを新しい順（snapshot_id 降順）で返す。

    manifest を持つディレクトリのみを対象とする。
    """
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return []
    found = []
    for child in out_dir.iterdir():
        if not child.is_dir():
            continue
        manifest = _read_manifest(child)
        if manifest is not None:
            found.append(manifest)
    found.sort(
        key=lambda m: _snapshot_sort_key(m.get("snapshot_id", "")),
        reverse=True,
    )
    return found


def prune_backups(out_dir: str | Path, keep: int) -> list[str]:
    """新しい `keep` 件を残し、古いスナップショットを削除する。

    Args:
        out_dir: スナップショット親ディレクトリ。
        keep: 残す世代数（>= 0）。

    Returns:
        削除した snapshot_id のリスト（新しい順入力の末尾＝古い側）。

    Raises:
        BackupError: keep が負の場合。
    """
    if keep < 0:
        raise BackupError(f"--keep は 0 以上である必要があります: {keep}")
    out_dir = Path(out_dir).resolve()
    backups = list_backups(out_dir)
    to_remove = backups[keep:]
    removed: list[str] = []
    for manifest in to_remove:
        sid = manifest.get("snapshot_id")
        if not isinstance(sid, str) or not sid:
            # snapshot_id が str でない / 空の manifest は触らない（path を
            # 信頼して out_dir 自体や外を誤削除しない。`out_dir / sid` の
            # TypeError も防ぐ）。
            continue
        # manifest の `path` は信頼せず snapshot_id から out_dir 配下の
        # ディレクトリを再構成し、out_dir 直下の正規ディレクトリであることを
        # 検証してから削除する（path traversal / 誤削除の防止）。
        snapshot_dir = (out_dir / sid).resolve()
        if snapshot_dir.parent != out_dir or not snapshot_dir.is_dir():
            continue
        try:
            shutil.rmtree(snapshot_dir)
        except OSError as e:
            raise BackupError(
                f"スナップショットの削除に失敗しました ({snapshot_dir}): {e}"
            ) from e
        removed.append(sid)
    return removed


def resolve_snapshot(out_dir: str | Path, ref: str) -> Path | None:
    """`--from` 引数（"latest" / id / パス）からスナップショットディレクトリを解決する。

    解決順:
    1. 予約語 "latest" → out_dir 内の最新（パス解決より優先）
    2. 実在するパス（manifest を持つディレクトリ）
    3. out_dir 配下の snapshot_id

    見つからなければ None。
    """
    out_dir = Path(out_dir)

    # 1. 予約語 "latest" はパス解決より先に扱う。
    #    （cwd 等に `latest/manifest.json` が存在しても、破壊的な restore で
    #    意図せずそちらを復元元に採らないようにする。）
    if ref == "latest":
        backups = list_backups(out_dir)
        if not backups:
            return None
        # manifest の `path` は信頼せず（改竄され得る）、snapshot_id から
        # out_dir 配下のディレクトリを再構成・検証する（prune_backups と同方針）。
        sid = backups[0].get("snapshot_id")
        if not isinstance(sid, str) or not sid:
            # 非 str / 空（改竄）は `out_dir / sid` の TypeError を避けて None。
            return None
        latest = (out_dir / sid).resolve()
        if latest.parent != out_dir.resolve() or not latest.is_dir():
            return None
        return latest

    # 2. 直接パス指定
    candidate = Path(ref)
    if candidate.is_dir() and _read_manifest(candidate) is not None:
        return candidate

    # 3. id 指定
    by_id = out_dir / ref
    if by_id.is_dir() and _read_manifest(by_id) is not None:
        return by_id

    return None


def _sidecar_paths(db_path: Path) -> list[Path]:
    """WAL モード DB の sidecar（-wal / -shm）パスを返す。"""
    return [
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]


def restore_backup(
    *,
    snapshot_dir: str | Path,
    database_path: str | Path,
    checkpoint_db_path: str | Path,
    verify: bool = True,
) -> dict[str, Any]:
    """スナップショットを現 state へ復元する（安全側）。

    手順（コンポーネントごと）:
      1. スナップショット DB の integrity_check（verify=True のとき）
      2. 全コンポーネントを一時ファイルへコピー（target は未変更）
      3. 現 DB と その `-wal` / `-shm` を `<db>.pre-restore[-wal/-shm]` に退避
         （削除でなく退避。失敗時に元 WAL も含め完全復元できるよう保持する）
      4. 一時ファイルを `os.replace` で原子的に配置
      失敗時は処理中分・配置済み分・退避した sidecar をまとめてロールバックする。

    Args:
        snapshot_dir: スナップショットディレクトリ。
        database_path: 復元先の workflow.db パス。
        checkpoint_db_path: 復元先の checkpoint.db パス。
        verify: 適用前に integrity_check を行うか。

    Returns:
        復元結果サマリ（restored コンポーネント / pre-restore 退避先 等）。

    Raises:
        BackupError: manifest 不在 / integrity NG / コピー失敗。
    """
    snapshot_dir = Path(snapshot_dir)
    manifest = _read_manifest(snapshot_dir)
    if manifest is None:
        # 「不在」と「存在するが壊れて読めない（JSON / encoding 破損）」を
        # 区別して原因切り分けを助ける。
        if (snapshot_dir / MANIFEST_NAME).exists():
            raise BackupError(
                f"manifest を読めません（破損 / 不正な JSON / encoding）: "
                f"{snapshot_dir / MANIFEST_NAME}"
            )
        raise BackupError(
            f"スナップショットが見つかりません（manifest 不在）: {snapshot_dir}"
        )

    targets = _component_paths(database_path, checkpoint_db_path)
    components = manifest.get("components", {})
    # manifest は手動編集 / 破損し得る untrusted input。components が dict で
    # ない、各 entry の構造が想定外、file 名が出力ディレクトリ外を指す等は
    # KeyError/TypeError を出さず BackupError として扱う。
    if not isinstance(components, dict):
        raise BackupError("manifest の components が不正です（dict ではない）")

    # 先に全コンポーネントの integrity を検証してから差し替える
    # （途中まで適用して片肺になるのを避ける）。
    planned: list[tuple[str, Path, Path]] = []
    for name, info in components.items():
        if name not in targets:
            continue
        if not isinstance(info, dict) or not isinstance(info.get("file"), str):
            raise BackupError(
                f"manifest のコンポーネント定義が不正です: {name}"
            )
        # file はファイル名のみを想定。パス区切りを含む値は snapshot_dir 外を
        # 指し得るため拒否する（path traversal の防止）。
        file_name = info["file"]
        if "/" in file_name or "\\" in file_name or file_name in ("", ".", ".."):
            raise BackupError(
                f"manifest のコンポーネント file 名が不正です: {name}={file_name!r}"
            )
        src = snapshot_dir / file_name
        if not src.exists():
            raise BackupError(f"スナップショット DB が欠落しています: {src}")
        if verify and not integrity_check(src):
            raise BackupError(
                f"スナップショットの integrity_check に失敗しました: {src}"
            )
        planned.append((name, src, targets[name]))

    if not planned:
        raise BackupError("復元対象のコンポーネントがありません")

    # 2 フェーズコミットで「途中失敗による片肺」を避ける。
    #   Phase A: 全コンポーネントを一時ファイルへコピー（target は一切触らない）
    #   Phase B: 全 target を pre-restore 退避 → os.replace で配置
    #   いずれの失敗でも、それまでの変更をまとめてロールバックする。
    tmps: list[tuple[str, Path, Path]] = []  # (name, tmp, target)

    def _cleanup_tmps() -> None:
        for _n, _tmp, _t in tmps:
            if _tmp.exists():
                try:
                    _tmp.unlink()
                except OSError:
                    pass

    # --- Phase A: コピー（target 未変更）---
    try:
        for name, src, target in planned:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".restore-tmp")
            shutil.copyfile(src, tmp)
            tmps.append((name, tmp, target))
    except OSError as e:
        _cleanup_tmps()
        raise BackupError(f"復元の準備（コピー）に失敗しました: {e}") from e

    # --- Phase B: 退避 + 配置（失敗したら committed + 処理中をまとめてロールバック）---
    committed: list[tuple[str, Path, Path | None]] = []  # (name, target, pre_restore)
    # 「target を pre-restore へ退避済みだが os.replace 前 / 失敗」の処理中分。
    inflight_target: Path | None = None
    inflight_pre: Path | None = None

    def _suffix_of(target: Path, sidecar: Path) -> str:
        # "workflow.db" と "workflow.db-wal" から "-wal" を取り出す。
        return sidecar.name[len(target.name):]

    def _rollback_one(target: Path, pre_restore: Path | None) -> None:
        # 置換済み / 退避済みの target を pre-restore から元に戻す。
        # WAL/SHM sidecar も退避先（pre_restore + suffix）から復元する。
        try:
            if target.exists():
                target.unlink()
            for sidecar in _sidecar_paths(target):
                if sidecar.exists():
                    sidecar.unlink()
            if pre_restore is not None and pre_restore.exists():
                shutil.move(str(pre_restore), str(target))
            if pre_restore is not None:
                for sidecar in _sidecar_paths(target):
                    suffix = _suffix_of(target, sidecar)
                    saved = pre_restore.with_name(pre_restore.name + suffix)
                    if saved.exists():
                        shutil.move(str(saved), str(sidecar))
        except OSError:
            pass

    try:
        for name, tmp, target in tmps:
            pre_restore: Path | None = None
            if target.exists():
                backup_side = target.with_name(target.name + ".pre-restore")
                if backup_side.exists():
                    backup_side.unlink()
                shutil.move(str(target), str(backup_side))
                pre_restore = backup_side
                # 古い WAL/SHM は「削除」ではなく pre-restore 側へ退避する。
                # 削除してしまうと、後段失敗→ロールバックで元 DB は戻っても
                # WAL に残っていた未反映コミットが失われ得るため（SQLiteStore は
                # WAL 前提。失敗時も元 state を完全復元できるよう保持する）。
                for sidecar in _sidecar_paths(target):
                    if sidecar.exists():
                        suffix = _suffix_of(target, sidecar)
                        dest = backup_side.with_name(backup_side.name + suffix)
                        if dest.exists():
                            dest.unlink()
                        shutil.move(str(sidecar), str(dest))
            # ここで os.replace 前に失敗しても巻き戻せるよう処理中分を記録。
            inflight_target, inflight_pre = target, pre_restore
            os.replace(tmp, target)  # 同一ディレクトリなので原子的
            committed.append((name, target, pre_restore))
            inflight_target, inflight_pre = None, None
    except OSError as e:
        # 処理中（os.replace 前後で失敗）分を先に巻き戻す。
        if inflight_target is not None:
            _rollback_one(inflight_target, inflight_pre)
        # 既に置換済みの target を新しい順に巻き戻す。
        for _n, _t, _pre in reversed(committed):
            _rollback_one(_t, _pre)
        _cleanup_tmps()
        raise BackupError(f"復元に失敗しました: {e}") from e

    restored = [
        {
            "component": name,
            "target": str(target),
            "pre_restore": str(pre_restore) if pre_restore else None,
        }
        for name, target, pre_restore in committed
    ]

    return {
        "snapshot_id": manifest.get("snapshot_id"),
        "snapshot_dir": str(snapshot_dir),
        "restored": restored,
    }
