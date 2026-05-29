"""Operation Registry (Step 3 第1スライス) のテスト。

registry インフラ (登録・取得・一覧・重複拒否)、seed read-only operation の
handler、CLI `--param KEY=VALUE` パーサを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.cli_main import _parse_operation_params
from hokusai.operations import (
    READ_ONLY,
    Operation,
    OperationRegistry,
    ReadOnlyStore,
    build_default_registry,
    default_registry,
)


class _FakeStore:
    """read-only handler が呼ぶメソッドだけを持つ store ダブル。"""

    def __init__(self, *, pending=0, errors=0, workflows=None):
        self._pending = pending
        self._errors = errors
        self._workflows = workflows or []

    def count_notion_sync_pending(self):
        return self._pending

    def count_notion_sync_errors(self):
        return self._errors

    def list_active_workflows(self):
        return self._workflows


class _FakeConfig:
    """llm_gateway.enabled だけ持つ config ダブル。"""

    class _GW:
        enabled = False

    llm_gateway = _GW()


# --- registry インフラ ---------------------------------------------------


def test_registry_register_and_get():
    reg = OperationRegistry()
    op = Operation(
        name="x.y",
        summary="s",
        scope=READ_ONLY,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda params, *, store, config: {},
    )
    reg.register(op)
    assert reg.get("x.y") is op
    assert reg.get("missing") is None


def test_operation_is_unhashable():
    # frozen でも input_schema(dict) を持つので明示的に unhashable
    # (PR #143 Copilot Round 4: set/dict key 投入時の latent TypeError 防止)
    op = Operation(
        name="x.y",
        summary="s",
        scope=READ_ONLY,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda params, *, store, config: {},
    )
    with pytest.raises(TypeError):
        hash(op)


def test_registry_rejects_duplicate():
    reg = OperationRegistry()
    op = Operation(
        name="dup",
        summary="s",
        scope=READ_ONLY,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda params, *, store, config: {},
    )
    reg.register(op)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(op)


def test_registry_names_sorted():
    reg = OperationRegistry()
    for n in ("c.op", "a.op", "b.op"):
        reg.register(
            Operation(
                name=n,
                summary="",
                scope=READ_ONLY,
                input_schema={"type": "object", "properties": {}, "required": []},
                handler=lambda params, *, store, config: {},
            )
        )
    assert reg.names() == ["a.op", "b.op", "c.op"]
    assert [op.name for op in reg.list()] == ["a.op", "b.op", "c.op"]


def test_default_registry_is_singleton():
    assert default_registry() is default_registry()


def test_build_default_registry_seeds_read_only_ops():
    reg = build_default_registry()
    names = reg.names()
    assert "notion.outbox_status" in names
    assert "runtime.health" in names
    assert "workflow.list" in names
    # 第1スライスは全て read-only
    assert all(op.is_read_only for op in reg.list())


# --- seed handler --------------------------------------------------------


def test_op_notion_outbox_status():
    reg = build_default_registry()
    op = reg.get("notion.outbox_status")
    result = op.handler(
        {}, store=_FakeStore(pending=2, errors=1), config=_FakeConfig()
    )
    # キー名は runtime_health と統一 (PR #143 Copilot Round 3)
    assert result == {"outbox_pending": 2, "outbox_errors": 1}


def test_op_workflow_list():
    reg = build_default_registry()
    op = reg.get("workflow.list")
    wfs = [{"workflow_id": "wf-1"}, {"workflow_id": "wf-2"}]
    result = op.handler({}, store=_FakeStore(workflows=wfs), config=_FakeConfig())
    assert result == {"workflows": wfs}


def test_op_runtime_health_delegates_to_compute(monkeypatch):
    import hokusai.health as health_mod

    captured = {}

    def _fake(store, *, llm_gateway_enabled, workflow_id=None, state=None):
        captured["llm"] = llm_gateway_enabled
        captured["wf"] = workflow_id
        return {"ran": True, "gaps": []}

    monkeypatch.setattr(health_mod, "compute_runtime_health", _fake)
    reg = build_default_registry()
    op = reg.get("runtime.health")
    result = op.handler(
        {"workflow_id": "wf-9"}, store=_FakeStore(), config=_FakeConfig()
    )
    assert result == {"ran": True, "gaps": []}
    assert captured["wf"] == "wf-9"
    assert captured["llm"] is False


# --- --param パーサ ------------------------------------------------------


def test_parse_operation_params_basic():
    assert _parse_operation_params(["a=1", "b=2"]) == {"a": "1", "b": "2"}


def test_parse_operation_params_none():
    assert _parse_operation_params(None) == {}


def test_parse_operation_params_value_with_equals():
    assert _parse_operation_params(["expr=a=b"]) == {"expr": "a=b"}


def test_parse_operation_params_rejects_missing_equals():
    with pytest.raises(ValueError, match="KEY=VALUE"):
        _parse_operation_params(["noequals"])


def test_parse_operation_params_rejects_empty_key():
    with pytest.raises(ValueError, match="KEY が空"):
        _parse_operation_params(["=value"])


def test_parse_operation_params_rejects_duplicate_key():
    # 後勝ちで silent 上書きせず reject (PR #143 Copilot Round 2)
    with pytest.raises(ValueError, match="重複"):
        _parse_operation_params(["a=1", "a=2"])


# --- CLI: stdout/stderr 分離 (PR #143 Copilot Round 1) -------------------
# `operations run ... | jq` で stdout を pipe する利用者が、エラー文と JSON の
# 混在出力を掴まないよう、エラー / usage は stderr・結果 (JSON) のみ stdout。


def _ns(**kw):
    import argparse

    return argparse.Namespace(**kw)


def test_handle_operations_unknown_op_errors_to_stderr(capsys):
    from hokusai.cli_main import _handle_operations

    rc = _handle_operations(
        _ns(operations_subcommand="run", name="no.such.op", params=None),
        _FakeConfig(),
    )
    captured = capsys.readouterr()
    assert rc == 1
    # stdout は JSON 専用なので空
    assert captured.out == ""
    assert "未知の operation" in captured.err


def test_handle_operations_no_subcommand_usage_to_stderr(capsys):
    from hokusai.cli_main import _handle_operations

    rc = _handle_operations(_ns(operations_subcommand=None), _FakeConfig())
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "使い方" in captured.err


# --- CLI: list / run の成功パス契約 (PR #143 Copilot Round 2) -------------
# 回帰しやすい CLI 契約 (stdout のみ・exit 0・JSON schema) を固定する。


def test_handle_operations_list_json_contract(capsys):
    import json

    from hokusai.cli_main import _handle_operations

    rc = _handle_operations(
        _ns(operations_subcommand="list", output="json"), _FakeConfig()
    )
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    names = {op["name"] for op in payload["operations"]}
    assert {"notion.outbox_status", "runtime.health", "workflow.list"} <= names
    # 各 op が契約キーを持つ
    for op in payload["operations"]:
        assert set(op) == {"name", "scope", "summary", "input_schema"}


def test_handle_operations_list_text(capsys):
    from hokusai.cli_main import _handle_operations

    rc = _handle_operations(
        _ns(operations_subcommand="list", output="text"), _FakeConfig()
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "notion.outbox_status" in captured.out
    assert "[read_only]" in captured.out


def test_handle_operations_run_success_json_only(capsys, monkeypatch):
    """run 成功時は stdout に JSON のみ・exit 0 (stderr は空)。"""
    import json

    import hokusai.operations as operations_mod
    from hokusai.cli_main import _handle_operations

    # ReadOnlyStore を fake に差し替え (tmp DB 不要)
    monkeypatch.setattr(
        operations_mod,
        "ReadOnlyStore",
        lambda *a, **k: _FakeStore(pending=3, errors=1),
    )

    class _Cfg(_FakeConfig):
        database_path = ":memory:"

    rc = _handle_operations(
        _ns(operations_subcommand="run", name="notion.outbox_status", params=None),
        _Cfg(),
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {"outbox_pending": 3, "outbox_errors": 1}


def test_handle_operations_run_mutating_rejected(capsys, monkeypatch):
    """mutating scope の operation は run 不可・stderr へ・exit 1。"""
    import hokusai.operations as operations_mod
    from hokusai.cli_main import _handle_operations
    from hokusai.operations import MUTATING, Operation, OperationRegistry

    reg = OperationRegistry()
    reg.register(
        Operation(
            name="danger.do",
            summary="",
            scope=MUTATING,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda params, *, store, config: {},
        )
    )
    # _handle_operations は関数内で `from .operations import default_registry`
    # するため、hokusai.operations 側を差し替えれば足りる。
    monkeypatch.setattr(operations_mod, "default_registry", lambda: reg)

    rc = _handle_operations(
        _ns(operations_subcommand="run", name="danger.do", params=None),
        _FakeConfig(),
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "scope=" in captured.err


# --- ReadOnlyStore: 副作用ゼロの read-only 契約 (PR #143 Copilot Round 5) --


def test_read_only_store_does_not_create_db(tmp_path):
    """存在しない DB パスを読んでも、ファイルを新規作成しない。

    SQLiteStore は mode=rw で DB を新規作成する副作用があるのに対し、
    ReadOnlyStore は mode=ro なので作成しない (read-only 契約の核心)。
    """
    db = tmp_path / "nonexistent.db"
    store = ReadOnlyStore(db)
    # 全メソッドが安全側の既定値を返し、例外を投げない
    assert store.count_notion_sync_pending() == 0
    assert store.count_notion_sync_errors() == 0
    assert store.list_active_workflows() == []
    assert store.has_failed_workflow_started("wf-x") is False
    assert store.list_audit_logs(workflow_id="wf-x", limit=1) == []
    # 副作用がない = ファイルは作られていない
    assert not db.exists()


def test_read_only_store_reads_existing_db(tmp_path):
    """既存 DB に対しては SELECT 結果を正しく返す。"""
    import sqlite3

    db = tmp_path / "wf.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE notion_sync_outbox (idempotency_key TEXT);
        CREATE TABLE notion_sync_errors (workflow_id TEXT, event_type TEXT);
        CREATE TABLE workflows (
            workflow_id TEXT, task_url TEXT, task_title TEXT,
            current_phase INTEGER, updated_at TEXT
        );
        INSERT INTO notion_sync_outbox VALUES ('k1'), ('k2');
        INSERT INTO notion_sync_errors VALUES ('wf-1', 'workflow_started');
        INSERT INTO workflows VALUES ('wf-1', 'u', 't', 3, '2026-01-01');
        INSERT INTO workflows VALUES ('wf-done', 'u', 't', 10, '2026-01-02');
        """
    )
    conn.commit()
    conn.close()

    store = ReadOnlyStore(db)
    assert store.count_notion_sync_pending() == 2
    assert store.count_notion_sync_errors() == 1
    assert store.has_failed_workflow_started("wf-1") is True
    assert store.has_failed_workflow_started("wf-none") is False
    wfs = store.list_active_workflows()
    # current_phase < 10 のみ (wf-done は除外)
    assert [w["workflow_id"] for w in wfs] == ["wf-1"]


def test_read_only_store_rejects_writes(tmp_path):
    """mode=ro 接続なので INSERT/UPDATE は失敗する (実 I/O レベルの read-only 保証)。

    _read は sqlite3.Error を握りつぶし default を返すため、書き込み試行は
    黙って no-op になり DB は変化しない。
    """
    import sqlite3

    db = tmp_path / "wf.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE notion_sync_outbox (idempotency_key TEXT);"
        "INSERT INTO notion_sync_outbox VALUES ('k1');"
    )
    conn.commit()
    conn.close()

    store = ReadOnlyStore(db)
    # _read 経由で書き込みを試みても ro 接続が拒否し default(None) を返す
    result = store._read(
        lambda c: c.execute("INSERT INTO notion_sync_outbox VALUES ('k2')"),
        None,
    )
    assert result is None
    # 件数は増えていない
    assert store.count_notion_sync_pending() == 1
