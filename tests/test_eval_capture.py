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
    build_capture_key,
    build_eval_gate_result,
    build_verification_captures,
    compute_content_digest,
    fixture_identity,
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


# --- 明示 capture (Step 4 第2スライス) -----------------------------------


def test_build_verification_captures():
    errors = [
        {"repository": "Backend", "command": "npm test",
         "error_output": "boom\nmore", "full_output_hash": "fh1",
         "success": False},
        {"command": "ruff", "error_output": "lint fail", "success": False},
        {"repository": "X", "command": "ok", "success": True},  # 成功は skip
        "not-a-dict",
    ]
    caps = build_verification_captures("wf-1", errors)
    assert len(caps) == 2
    assert caps[0]["kind"] == "verification"
    assert caps[0]["phase"] == 6
    assert caps[0]["label"] == "Backend:npm test"
    assert caps[0]["output_hash"] == "fh1"  # full_output_hash 優先
    # full_output_hash 採用時は length を None に（hash 対象=全文と整合、
    # PR #152 Copilot Round 2）
    assert caps[0]["output_length"] is None
    assert caps[0]["status"] == "fail"
    assert caps[1]["label"] == "ruff"  # repository 無し
    # full_output_hash 無し → error_output 自身の hash + length（整合）
    assert caps[1]["output_length"] == len("lint fail")


def test_eval_captures_phase_index_exists(store):
    """phase フィルタ用 index が張られている (PR #152 Copilot Round 2)。"""
    with store._connect() as conn:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='eval_captures'"
            ).fetchall()
        }
    assert "idx_eval_captures_phase" in names


def test_build_capture_key_is_deterministic_and_output_sensitive():
    base = dict(workflow_id="wf-1", phase=6, kind="verification", label="x")
    k1 = build_capture_key(**base, output_hash="h1")
    k2 = build_capture_key(**base, output_hash="h1")
    k3 = build_capture_key(**base, output_hash="h2")
    assert k1 == k2          # 決定的
    assert k1 != k3          # 出力が変われば別 key（失敗→修正で別 fixture）


def test_record_and_list_eval_capture(store):
    store.record_eval_capture(
        capture_key="ck1", workflow_id="wf-1", phase=6, kind="verification",
        label="Backend:npm test", input_hash="ih", input_length=8,
        output_hash="oh", output_length=20, status="fail",
        metadata={"repository": "Backend"},
    )
    rows = store.list_eval_captures()
    assert len(rows) == 1
    assert rows[0]["kind"] == "verification"
    assert rows[0]["status"] == "fail"
    assert rows[0]["metadata"] == {"repository": "Backend"}


def test_record_eval_capture_requires_workflow_id(store):
    """Store API レベルで workflow_id 無しを reject (PR #152 Copilot Round 3)。"""
    with pytest.raises(ValueError, match="workflow_id が必須"):
        store.record_eval_capture(capture_key="ck", workflow_id=None,
                                  kind="verification", output_hash="o")
    with pytest.raises(ValueError, match="workflow_id が必須"):
        store.record_eval_capture(capture_key="ck", workflow_id="",
                                  kind="verification", output_hash="o")


def test_eval_captures_workflow_id_not_null_at_schema(store):
    """schema 側でも workflow_id NOT NULL を担保（直 SQL の NULL も DB が弾く、
    PR #152 Copilot Round 4）。"""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO eval_captures (capture_key, workflow_id, "
                "created_at, updated_at) VALUES ('k', NULL, 't', 't')"
            )
            conn.commit()


def test_record_eval_capture_idempotent(store):
    for _ in range(3):
        store.record_eval_capture(
            capture_key="ck1", workflow_id="wf-1", phase=6,
            kind="verification", label="x", output_hash="oh", status="fail",
        )
    assert len(store.list_eval_captures()) == 1


def test_eval_captures_cascade_on_gc(tmp_path):
    from datetime import datetime, timedelta

    s = SQLiteStore(tmp_path / "wf.db")
    old = (datetime.now() - timedelta(days=120)).isoformat()
    with s._connect() as conn:
        conn.execute(
            "INSERT INTO workflows (workflow_id, task_url, task_title, "
            "branch_name, current_phase, state_json, created_at, updated_at, "
            "profile_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wf-old", "u", "t", "b", 10, "{}", old, old, None),
        )
        conn.commit()
    s.record_eval_capture(capture_key="ck", workflow_id="wf-old",
                          kind="verification", output_hash="o")
    counts = s.delete_old_completed_workflows(retention_days=90)
    assert counts["eval_captures"] == 1
    assert s.list_eval_captures(workflow_id="wf-old") == []


def test_persist_verification_captures_helper(store):
    from hokusai.local_persistence import persist_verification_captures

    errors = [
        {"repository": "Backend", "command": "npm test",
         "error_output": "boom", "success": False},
    ]
    n = persist_verification_captures(store, "wf-1", errors)
    assert n == 1
    rows = store.list_eval_captures(kind="verification")
    assert len(rows) == 1
    assert rows[0]["label"] == "Backend:npm test"


def test_persist_verification_captures_skips_when_no_workflow_id(store):
    """workflow_id が無い場合は永続化しない（capture_key 衝突 / GC 孤児防止、
    PR #152 Copilot Round 1）。"""
    from hokusai.local_persistence import persist_verification_captures

    errors = [{"repository": "B", "command": "npm test",
               "error_output": "x", "success": False}]
    assert persist_verification_captures(store, None, errors) == 0
    assert persist_verification_captures(store, "", errors) == 0
    assert store.list_eval_captures() == []


def test_eval_export_limit_applies_after_merge(store, capsys):
    """--limit は audit + captures 合流後に全体へ適用される（個別2倍にしない、
    PR #152 Copilot Round 1）。"""
    import argparse
    import json

    from hokusai.cli_main import _handle_eval

    # audit 2 件 + capture 2 件 = 計 4 件、limit=2 で 2 件に絞られる
    for i in range(2):
        store.add_audit_log(
            "wf-1", 2, "llm_gateway_decision", "log",
            {"prompt_hash": f"h{i}", "context": {"purpose": "p"}},
        )
        store.record_eval_capture(
            capture_key=f"ck{i}", workflow_id="wf-1", kind="verification",
            output_hash=f"o{i}", status="fail",
        )

    class _Cfg:
        database_path = store.db_path

    rc = _handle_eval(
        argparse.Namespace(eval_subcommand="export", since=None,
                           workflow_id=None, limit=2, output="json"),
        _Cfg(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(payload["fixtures"]) == 2  # 4 ではなく limit=2


# --- eval gate (Step 4 第3スライス) --------------------------------------


def _cap_fixture(capture_key, status="fail", label="Backend:test"):
    return {"kind": "verification", "capture_key": capture_key,
            "label": label, "status": status}


def test_fixture_identity():
    assert fixture_identity({"capture_key": "ck1"}) == "capture:ck1"
    assert fixture_identity({"audit_id": 5}) == "audit:5"
    # capture_key も audit_id も無い → 合成キー（落ちない）
    assert fixture_identity({"kind": "x"}).startswith("fx:")


def test_build_eval_gate_result_detects_regression_and_improvement():
    baseline = [_cap_fixture("ck-old-fail"), _cap_fixture("ck-stable")]
    current = [_cap_fixture("ck-stable"), _cap_fixture("ck-new-fail")]
    r = build_eval_gate_result(baseline, current)
    assert r["baseline_count"] == 2
    assert r["current_count"] == 2
    # ck-new-fail が added かつ status=fail → regression
    assert [f["capture_key"] for f in r["regressions"]] == ["ck-new-fail"]
    # ck-old-fail が removed かつ status=fail → improvement
    assert [f["capture_key"] for f in r["improvements"]] == ["ck-old-fail"]


def test_build_eval_gate_result_no_regression_when_stable():
    fx = [_cap_fixture("ck1"), _cap_fixture("ck2")]
    r = build_eval_gate_result(fx, fx)
    assert r["regressions"] == []
    assert r["improvements"] == []
    assert r["added"] == []
    assert r["removed"] == []


def test_load_baseline_fixtures(tmp_path):
    import json

    from hokusai.cli_main import _load_baseline_fixtures

    # {"fixtures": [...]} 形式
    p1 = tmp_path / "b1.json"
    p1.write_text(json.dumps({"fixtures": [{"capture_key": "ck1"}]}))
    assert _load_baseline_fixtures(str(p1)) == [{"capture_key": "ck1"}]
    # bare list 形式
    p2 = tmp_path / "b2.json"
    p2.write_text(json.dumps([{"capture_key": "ck2"}]))
    assert _load_baseline_fixtures(str(p2)) == [{"capture_key": "ck2"}]
    # 不正形式
    p3 = tmp_path / "b3.json"
    p3.write_text(json.dumps({"bad": 1}))
    with pytest.raises(ValueError, match="形式である必要"):
        _load_baseline_fixtures(str(p3))


def test_handle_eval_gate_fail_on_regression(store, tmp_path, capsys):
    import argparse
    import json

    from hokusai.cli_main import _handle_eval

    # baseline は空、現状に verification fail が 1 件 → regression
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps({"fixtures": []}))
    store.record_eval_capture(
        capture_key="ck1", workflow_id="wf-1", phase=6, kind="verification",
        label="Backend:test", output_hash="oh", status="fail",
    )

    class _Cfg:
        database_path = store.db_path

    # --fail-on-regression あり → exit 1
    rc = _handle_eval(
        argparse.Namespace(eval_subcommand="gate", baseline=str(baseline),
                           workflow_id=None, limit=10000,
                           fail_on_regression=True, output="json"),
        _Cfg(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert len(payload["regressions"]) == 1

    # --fail-on-regression なし → exit 0（報告のみ）
    rc2 = _handle_eval(
        argparse.Namespace(eval_subcommand="gate", baseline=str(baseline),
                           workflow_id=None, limit=10000,
                           fail_on_regression=False, output="json"),
        _Cfg(),
    )
    capsys.readouterr()
    assert rc2 == 0


def test_handle_eval_gate_missing_baseline(store, capsys):
    import argparse

    from hokusai.cli_main import _handle_eval

    class _Cfg:
        database_path = store.db_path

    rc = _handle_eval(
        argparse.Namespace(eval_subcommand="gate",
                           baseline="/nonexistent/base.json",
                           workflow_id=None, limit=10000,
                           fail_on_regression=False, output="text"),
        _Cfg(),
    )
    assert rc == 1
    assert "baseline 読み込み失敗" in capsys.readouterr().err


def test_eval_export_includes_captures(store, capsys):
    import argparse
    import json

    from hokusai.cli_main import _handle_eval

    store.record_eval_capture(
        capture_key="ck1", workflow_id="wf-1", phase=6, kind="verification",
        label="Backend:npm test", output_hash="oh", status="fail",
    )

    class _Cfg:
        database_path = store.db_path

    rc = _handle_eval(
        argparse.Namespace(eval_subcommand="export", since=None,
                           workflow_id=None, limit=1000, output="json"),
        _Cfg(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    kinds = {f["kind"] for f in payload["fixtures"]}
    assert "verification" in kinds
