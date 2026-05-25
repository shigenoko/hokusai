"""M2.6 (#107): cleanup --stale --dry-run / --sync-notion の単体テスト

`docs/dogfooding-findings.md` §4.3 の独立小穴。`hokusai cleanup --stale` に
--dry-run（誤操作防止）と --sync-notion（Notion ゴースト残留防止）を追加。

検証ポイント:
1. --dry-run で実際の rmtree が行われないこと
2. --sync-notion で _sync_workflow_cancel_reason が呼ばれること
3. --dry-run --sync-notion で実 API 呼ばれずプレビュー出力のみ
4. orphan workflow（state=None）は sync-notion 時 skip
5. 両フラグ未指定で既存挙動が壊れていないこと
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai import cli_main  # noqa: E402
from hokusai.persistence.sqlite_store import SQLiteStore  # noqa: E402


def _make_args(
    *,
    workflow_id=None,
    stale=False,
    dry_run=False,
    sync_notion=False,
    cancel_reason=None,
    gc_workflows=False,
    retention_days=90,
) -> argparse.Namespace:
    return argparse.Namespace(
        workflow_id=workflow_id,
        stale=stale,
        dry_run=dry_run,
        sync_notion=sync_notion,
        cancel_reason=cancel_reason,
        gc_workflows=gc_workflows,
        retention_days=retention_days,
    )


def _make_config(tmp_path: Path) -> MagicMock:
    """`_handle_cleanup` が触る config 属性だけを生やした mock を返す。

    worktree_root / database_path / get_all_repositories() のみ使われる。
    notion_dashboard / SKIP_NOTION 関連は別 helper 経由（_sync_workflow_cancel_reason）
    で参照されるため、本テストでは monkeypatch でその helper を差し替えて切り離す。
    """
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    config = MagicMock()
    config.database_path = tmp_path / "workflow.db"
    config.worktree_root = worktree_root
    config.get_all_repositories.return_value = []
    return config


def _seed_active_workflow(store: SQLiteStore, workflow_id: str) -> None:
    """active workflow を 1 件 seed（current_phase < 10 で list_active_workflows に出る）."""
    store.save_workflow(
        workflow_id,
        {
            "workflow_id": workflow_id,
            "task_url": "https://example.com/issues/1",
            "task_title": "active wf",
            "branch_name": "feat/x",
            "current_phase": 4,
            "repositories": [],
            "profile_name": "test",
        },
    )


def _seed_completed_workflow(store: SQLiteStore, workflow_id: str) -> None:
    """完了 workflow を seed（current_phase=10 で list_active_workflows から除外）."""
    store.save_workflow(
        workflow_id,
        {
            "workflow_id": workflow_id,
            "task_url": "https://example.com/issues/2",
            "task_title": "done wf",
            "branch_name": "feat/y",
            "current_phase": 10,
            "repositories": [],
            "profile_name": "test",
        },
    )


def test_stale_normal_deletes_worktree(tmp_path):
    """既存挙動: --stale で stale worktree が実削除される（後方互換）"""
    config = _make_config(tmp_path)
    store = SQLiteStore(config.database_path)
    _seed_completed_workflow(store, "wf-done")
    stale_dir = config.worktree_root / "repo_wf-done"
    stale_dir.mkdir()

    args = _make_args(stale=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main._handle_cleanup(args, config)

    assert not stale_dir.exists(), "stale worktree は実削除されるはず"
    assert "1 件の stale worktree を削除しました" in buf.getvalue()


def test_stale_dry_run_keeps_worktree(tmp_path):
    """--dry-run: stale worktree は削除されず候補のみ表示"""
    config = _make_config(tmp_path)
    store = SQLiteStore(config.database_path)
    _seed_completed_workflow(store, "wf-done")
    stale_dir = config.worktree_root / "repo_wf-done"
    stale_dir.mkdir()

    args = _make_args(stale=True, dry_run=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main._handle_cleanup(args, config)

    assert stale_dir.exists(), "--dry-run 時は worktree が残るはず"
    out = buf.getvalue()
    assert "(dry-run) 削除予定" in out
    assert "(dry-run) 1 件の stale worktree が削除候補" in out


def test_stale_active_workflow_is_preserved(tmp_path):
    """active workflow（current_phase < 10）の worktree は --stale でも削除しない"""
    config = _make_config(tmp_path)
    store = SQLiteStore(config.database_path)
    _seed_active_workflow(store, "wf-live")
    live_dir = config.worktree_root / "repo_wf-live"
    live_dir.mkdir()

    args = _make_args(stale=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main._handle_cleanup(args, config)

    assert live_dir.exists(), "active workflow の worktree は残るはず"
    assert "0 件" in buf.getvalue()


def test_stale_sync_notion_calls_helper(tmp_path, monkeypatch):
    """--sync-notion で削除完了 workflow に _sync_workflow_cancel_reason が呼ばれる"""
    config = _make_config(tmp_path)
    store = SQLiteStore(config.database_path)
    _seed_completed_workflow(store, "wf-done")
    stale_dir = config.worktree_root / "repo_wf-done"
    stale_dir.mkdir()

    called: list[dict] = []

    def fake_sync(*, config, workflow_id, state, cancel_reason):
        called.append(
            {
                "workflow_id": workflow_id,
                "cancel_reason": cancel_reason,
                "state_phase": state.get("current_phase"),
            }
        )

    monkeypatch.setattr(cli_main, "_sync_workflow_cancel_reason", fake_sync)

    args = _make_args(stale=True, sync_notion=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main._handle_cleanup(args, config)

    assert not stale_dir.exists()
    assert len(called) == 1
    assert called[0]["workflow_id"] == "wf-done"
    assert called[0]["cancel_reason"] == "stale cleanup"
    assert called[0]["state_phase"] == 10


def test_stale_sync_notion_dry_run_skips_api(tmp_path, monkeypatch):
    """--dry-run --sync-notion: _sync_workflow_cancel_reason は呼ばれず予定表示のみ"""
    config = _make_config(tmp_path)
    store = SQLiteStore(config.database_path)
    _seed_completed_workflow(store, "wf-done")
    stale_dir = config.worktree_root / "repo_wf-done"
    stale_dir.mkdir()

    called: list[str] = []

    def fake_sync(**kwargs):
        called.append(kwargs["workflow_id"])

    monkeypatch.setattr(cli_main, "_sync_workflow_cancel_reason", fake_sync)

    args = _make_args(stale=True, dry_run=True, sync_notion=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main._handle_cleanup(args, config)

    assert stale_dir.exists(), "--dry-run 時は worktree が残る"
    assert called == [], "--dry-run 時は実 sync 呼ばれない"
    assert "(dry-run) Notion 同期予定: wf-done" in buf.getvalue()


def test_stale_sync_notion_orphan_skipped(tmp_path, monkeypatch):
    """orphan worktree（DB に state 無し）は --sync-notion 時 skip + warning"""
    config = _make_config(tmp_path)
    # DB を作るだけで wf-orphan は seed しない
    SQLiteStore(config.database_path)
    orphan_dir = config.worktree_root / "repo_wf-orphan"
    orphan_dir.mkdir()

    called: list[str] = []

    def fake_sync(**kwargs):
        called.append(kwargs["workflow_id"])

    monkeypatch.setattr(cli_main, "_sync_workflow_cancel_reason", fake_sync)

    args = _make_args(stale=True, sync_notion=True)
    err_buf = io.StringIO()
    out_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        cli_main._handle_cleanup(args, config)

    assert not orphan_dir.exists(), "orphan worktree は実削除される"
    assert called == [], "state=None の orphan には sync 呼ばれない"
    assert "state が無いため Notion 同期 skip" in err_buf.getvalue()


def test_stale_dry_run_no_writeback_cleanup(tmp_path, monkeypatch):
    """--dry-run 時は writeback cleanup（実 DB 書き換え）も skip"""
    config = _make_config(tmp_path)
    SQLiteStore(config.database_path)

    called: list[bool] = []

    def fake_writeback(config):
        called.append(True)

    monkeypatch.setattr(cli_main, "_cleanup_writeback_old_errors", fake_writeback)

    args = _make_args(stale=True, dry_run=True)
    with redirect_stdout(io.StringIO()):
        cli_main._handle_cleanup(args, config)

    assert called == [], "--dry-run 時は writeback cleanup を呼ばない"


def test_stale_normal_runs_writeback_cleanup(tmp_path, monkeypatch):
    """既存挙動: --stale 通常モードでは writeback cleanup が呼ばれる（後方互換）"""
    config = _make_config(tmp_path)
    SQLiteStore(config.database_path)

    called: list[bool] = []

    def fake_writeback(config):
        called.append(True)

    monkeypatch.setattr(cli_main, "_cleanup_writeback_old_errors", fake_writeback)

    args = _make_args(stale=True)
    with redirect_stdout(io.StringIO()):
        cli_main._handle_cleanup(args, config)

    assert called == [True], "通常 --stale では writeback cleanup が呼ばれる"


# --- Parser-level regression tests（Copilot Round 1 #3 指摘） ---
# トップレベル --dry-run とサブパーサ --dry-run が衝突したとき、サブパーサ側で
# `default=argparse.SUPPRESS` を指定しないと暗黙 False で上書きされて
# `hokusai --dry-run cleanup --stale` が dry-run にならないバグが入る。
# parser-level で両ポジションをカバーする回帰テスト。


def test_parser_dry_run_after_subcommand():
    """`hokusai cleanup --stale --dry-run` で args.dry_run=True"""
    parser, _, _ = cli_main._build_parser()
    args = parser.parse_args(["cleanup", "--stale", "--dry-run"])
    assert getattr(args, "dry_run", False) is True


def test_parser_dry_run_before_subcommand():
    """`hokusai --dry-run cleanup --stale` でもサブパーサが上書きせず True を維持"""
    parser, _, _ = cli_main._build_parser()
    args = parser.parse_args(["--dry-run", "cleanup", "--stale"])
    assert getattr(args, "dry_run", False) is True


def test_parser_dry_run_default_false():
    """`hokusai cleanup --stale` でフラグ未指定なら dry_run は False（トップレベル既定）"""
    parser, _, _ = cli_main._build_parser()
    args = parser.parse_args(["cleanup", "--stale"])
    assert getattr(args, "dry_run", False) is False


def test_parser_sync_notion_default_false():
    """--sync-notion 未指定でデフォルト False（トップレベルに同名フラグなしの確認）"""
    parser, _, _ = cli_main._build_parser()
    args = parser.parse_args(["cleanup", "--stale"])
    assert getattr(args, "sync_notion", False) is False


def test_parser_sync_notion_enabled():
    """`hokusai cleanup --stale --sync-notion` で sync_notion=True"""
    parser, _, _ = cli_main._build_parser()
    args = parser.parse_args(["cleanup", "--stale", "--sync-notion"])
    assert getattr(args, "sync_notion", False) is True


# --- Round 2 対応: 組み合わせ validation / dedup / stderr 整合性 ---


def test_dry_run_without_stale_rejected(tmp_path, capsys):
    """--dry-run を --stale なしで指定すると即 exit(1) + stderr 警告"""
    config = _make_config(tmp_path)
    SQLiteStore(config.database_path)
    args = _make_args(workflow_id="wf-x", dry_run=True)
    with pytest.raises(SystemExit) as excinfo:
        cli_main._handle_cleanup(args, config)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "--dry-run" in captured.err
    assert "--stale 専用" in captured.err


def test_sync_notion_without_stale_rejected(tmp_path, capsys):
    """--sync-notion を --stale なしで指定すると即 exit(1)"""
    config = _make_config(tmp_path)
    SQLiteStore(config.database_path)
    args = _make_args(workflow_id="wf-x", sync_notion=True)
    with pytest.raises(SystemExit) as excinfo:
        cli_main._handle_cleanup(args, config)
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "--sync-notion" in captured.err


def test_sync_notion_dedups_workflow_id_across_repos(tmp_path, monkeypatch):
    """同一 workflow が複数 repo の worktree を持つとき、Notion 同期は 1 回だけ呼ぶ"""
    config = _make_config(tmp_path)
    store = SQLiteStore(config.database_path)
    _seed_completed_workflow(store, "wf-done")
    # 同一 workflow_id の worktree を 2 つの repo に対して用意
    (config.worktree_root / "repoA_wf-done").mkdir()
    (config.worktree_root / "repoB_wf-done").mkdir()

    called: list[str] = []

    def fake_sync(*, config, workflow_id, state, cancel_reason):
        called.append(workflow_id)

    monkeypatch.setattr(cli_main, "_sync_workflow_cancel_reason", fake_sync)

    args = _make_args(stale=True, sync_notion=True)
    with redirect_stdout(io.StringIO()):
        cli_main._handle_cleanup(args, config)

    assert called == ["wf-done"], "同一 workflow_id は 1 回だけ同期されるべき"


def test_sync_notion_dry_run_orphan_writes_to_stderr(tmp_path, capsys):
    """--dry-run --sync-notion で orphan の警告は stderr（通常経路と整合）"""
    config = _make_config(tmp_path)
    SQLiteStore(config.database_path)
    (config.worktree_root / "repo_wf-orphan").mkdir()

    args = _make_args(stale=True, dry_run=True, sync_notion=True)
    cli_main._handle_cleanup(args, config)

    captured = capsys.readouterr()
    assert "(dry-run) ⚠ wf-orphan" in captured.err
    assert "(dry-run) ⚠ wf-orphan" not in captured.out
