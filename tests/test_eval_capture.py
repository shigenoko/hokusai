"""Eval Capture / export (Step 4 第1スライス) のテスト。

digest ユーティリティ、audit_logs → eval fixture 変換、`list_audit_logs` の
since フィルタ、CLI `hokusai eval export/list` の集約を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.eval_capture import (
    audit_row_to_fixture,
    audit_rows_to_fixtures,
    compute_content_digest,
)
from hokusai.persistence import SQLiteStore

# --- digest --------------------------------------------------------------


def test_compute_content_digest():
    d = compute_content_digest("hello")
    assert d["length"] == 5
    assert len(d["hash"]) == 16
    # 決定的（同入力で同 hash）
    assert compute_content_digest("hello")["hash"] == d["hash"]
    # 本文は返さない
    assert "hello" not in d.values()


def test_compute_content_digest_non_str():
    d = compute_content_digest(12345)
    assert d["length"] == len("12345")


# --- audit_row_to_fixture ------------------------------------------------


def _audit_row(**kw):
    details = {
        "decision": kw.get("decision", "log"),
        "prompt_hash": kw.get("prompt_hash", "abc123"),
        "prompt_length": kw.get("prompt_length", 100),
        "policy_hits": kw.get("policy_hits", []),
        "context": {
            "provider": kw.get("provider", "claude"),
            "model": kw.get("model", "claude-opus-4-8"),
            "purpose": kw.get("purpose", "phase2_research"),
        },
    }
    return {
        "id": kw.get("id", 1),
        "workflow_id": kw.get("workflow_id", "wf-1"),
        "phase": kw.get("phase", 2),
        "action": kw.get("action", "llm_gateway_decision"),
        "status": kw.get("status", "log"),
        "details": details,
        "created_at": kw.get("created_at", "2026-05-31T00:00:00"),
    }


def test_audit_row_to_fixture_maps_fields():
    f = audit_row_to_fixture(_audit_row())
    assert f["kind"] == "llm_call"
    assert f["workflow_id"] == "wf-1"
    assert f["phase"] == 2
    assert f["provider"] == "claude"
    assert f["purpose"] == "phase2_research"
    assert f["input_hash"] == "abc123"
    assert f["input_length"] == 100
    assert f["decision"] == "log"


def test_audit_row_to_fixture_skips_non_llm_action():
    assert audit_row_to_fixture(_audit_row(action="something_else")) is None


def test_audit_row_to_fixture_skips_bad_details():
    row = _audit_row()
    row["details"] = None
    assert audit_row_to_fixture(row) is None


def test_audit_row_to_fixture_handles_non_dict_context():
    """context が dict でなくても AttributeError で落ちない
    (PR #151 Copilot Round 1)。"""
    row = _audit_row()
    row["details"] = {"prompt_hash": "h", "context": "not-a-dict"}
    f = audit_row_to_fixture(row)
    assert f is not None
    assert f["provider"] is None  # 空 dict 扱い
    assert f["input_hash"] == "h"


def test_audit_rows_to_fixtures_filters_none():
    rows = [_audit_row(id=1), _audit_row(id=2, action="other")]
    fixtures = audit_rows_to_fixtures(rows)
    assert len(fixtures) == 1
    assert fixtures[0]["audit_id"] == 1


# --- list_audit_logs since フィルタ ---------------------------------------


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / "wf.db")


def test_list_audit_logs_since_filter(store):
    store.add_audit_log("wf-1", 2, "llm_gateway_decision", "log", {"a": 1})
    rows_all = store.list_audit_logs(action="llm_gateway_decision")
    assert len(rows_all) == 1
    created = rows_all[0]["created_at"]
    # created より後の cutoff → 0 件
    assert store.list_audit_logs(
        action="llm_gateway_decision", since="9999-01-01T00:00:00") == []
    # created 以前の cutoff → 1 件
    assert len(store.list_audit_logs(
        action="llm_gateway_decision", since="2000-01-01T00:00:00")) == 1
    # created ちょうど (>=) → 含む
    assert len(store.list_audit_logs(
        action="llm_gateway_decision", since=created)) == 1


# --- CLI handler ---------------------------------------------------------


def test_handle_eval_export_json(store, capsys):
    import argparse
    import json

    from hokusai.cli_main import _handle_eval

    store.add_audit_log(
        "wf-1", 2, "llm_gateway_decision", "log",
        {"decision": "log", "prompt_hash": "h1", "prompt_length": 42,
         "policy_hits": [], "context": {"provider": "claude",
                                        "purpose": "phase2_research"}},
    )
    # llm_gateway_decision 以外は除外される
    store.add_audit_log("wf-1", 2, "other_action", "log", {})

    class _Cfg:
        database_path = store.db_path

    args = argparse.Namespace(
        eval_subcommand="export", since=None, workflow_id=None,
        limit=1000, output="json",
    )
    rc = _handle_eval(args, _Cfg())
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(payload["fixtures"]) == 1
    assert payload["fixtures"][0]["input_hash"] == "h1"


def test_handle_eval_list_summary(store, capsys):
    import argparse
    import json

    from hokusai.cli_main import _handle_eval

    for purpose in ("phase2_research", "phase2_research", "phase7_review"):
        store.add_audit_log(
            "wf-1", 2, "llm_gateway_decision", "log",
            {"decision": "log", "prompt_hash": "h", "prompt_length": 1,
             "policy_hits": [], "context": {"purpose": purpose}},
        )

    class _Cfg:
        database_path = store.db_path

    args = argparse.Namespace(
        eval_subcommand="list", since=None, workflow_id=None,
        limit=1000, output="json",
    )
    rc = _handle_eval(args, _Cfg())
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert summary["total"] == 3
    assert summary["by_purpose"]["phase2_research"] == 2
    assert summary["by_purpose"]["phase7_review"] == 1


def test_handle_eval_rejects_bad_since(store, capsys):
    import argparse

    from hokusai.cli_main import _handle_eval

    class _Cfg:
        database_path = store.db_path

    args = argparse.Namespace(
        eval_subcommand="export", since="30days", workflow_id=None,
        limit=1000, output="json",
    )
    rc = _handle_eval(args, _Cfg())
    err = capsys.readouterr().err
    assert rc == 1
    assert "Nd / Nh" in err


def test_parse_since():
    from hokusai.cli_main import _parse_since

    assert _parse_since(None) is None
    assert _parse_since("") is None
    assert _parse_since("30d") is not None
    assert _parse_since("12h") is not None
    with pytest.raises(ValueError, match="Nd / Nh"):
        _parse_since("bad")


def test_parse_since_overflow_is_value_error():
    """極端に大きい値は OverflowError でなく ValueError として扱う
    (PR #151 Copilot Round 1: クラッシュさせない)。"""
    from hokusai.cli_main import _parse_since

    with pytest.raises(ValueError, match="大きすぎます"):
        _parse_since("999999999999d")
