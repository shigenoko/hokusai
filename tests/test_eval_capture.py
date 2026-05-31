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
    build_eval_replay_result,
    build_review_captures,
    build_verification_captures,
    compute_content_digest,
    fixture_identity,
    fixture_input_identity,
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


def test_build_review_captures_excludes_verification():
    """review capture は kind=review、verification_failure source は除外
    (verification capture が担当、二重取り込み回避)。"""
    review_issues = [
        {"source": "final_review", "rule": "HQ05", "repository": "Default",
         "message": "変更スコープの妥当性", "status": "open"},
        {"source": "verification_failure", "rule": "test",
         "message": "boom", "status": "open"},  # 除外
        {"source": "final_review", "message": ""},  # message 空 → skip
    ]
    caps = build_review_captures("wf-1", review_issues)
    assert len(caps) == 1
    c = caps[0]
    assert c["kind"] == "review"
    assert c["phase"] == 7
    assert c["label"] == "Default:HQ05"
    # 未解決(open)の review 指摘は gate 語彙で "fail"（regression 対象）
    assert c["status"] == "fail"
    assert c["metadata"]["review_status"] == "open"
    assert c["metadata"]["source"] == "final_review"


def test_build_review_captures_skips_missing_source():
    """source 欠落の malformed payload は capture しない（dispatcher guard を
    mirror、PR #160 Copilot Round 1）。"""
    caps = build_review_captures("wf-1", [
        {"rule": "HQ01", "message": "m"},          # source 無し → skip
        {"source": "final_review", "rule": "HQ01"},  # message 無し → skip
        {"source": "final_review", "message": "ok"},  # 両方あり → capture
    ])
    assert len(caps) == 1
    assert caps[0]["metadata"]["source"] == "final_review"


def test_build_review_captures_resolved_status_preserved():
    caps = build_review_captures("wf-1", [
        {"source": "final_review", "rule": "HQ01", "message": "m",
         "status": "resolved"},
    ])
    assert caps[0]["status"] == "resolved"  # 解決済みは fail にしない


def test_persist_review_captures_helper(store):
    from hokusai.local_persistence import persist_review_captures

    rows = [
        {"source": "final_review", "rule": "HQ02", "repository": "X",
         "message": "重複コード", "status": "open"},
    ]
    n = persist_review_captures(store, "wf-1", rows)
    assert n == 1
    caps = store.list_eval_captures(kind="review")
    assert len(caps) == 1
    assert caps[0]["label"] == "X:HQ02"


def test_persist_review_captures_skips_no_workflow_id(store):
    from hokusai.local_persistence import persist_review_captures

    rows = [{"source": "final_review", "rule": "HQ02", "message": "m"}]
    assert persist_review_captures(store, None, rows) == 0
    assert store.list_eval_captures() == []


def test_eval_gate_covers_review_regression(store, capsys):
    """新規の open review 指摘（kind=review, status=fail）が eval gate の
    regression として検出され --fail-on-regression で exit 1 になる。"""
    import argparse
    import json

    from hokusai.cli_main import _handle_eval

    cap = build_review_captures("wf-1", [
        {"source": "final_review", "rule": "HQ05", "repository": "D",
         "message": "新たな review 指摘", "status": "open"},
    ])[0]
    store.record_eval_capture(**cap)

    baseline = store.db_path.parent / "base.json"
    baseline.write_text(json.dumps({"fixtures": []}))

    class _Cfg:
        database_path = store.db_path

    rc = _handle_eval(
        argparse.Namespace(eval_subcommand="gate", baseline=str(baseline),
                           workflow_id=None, limit=10000,
                           fail_on_regression=True, output="json"),
        _Cfg(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert len(payload["regressions"]) == 1
    assert payload["regressions"][0]["kind"] == "review"


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


# --- eval replay (Step 4: 入力ごとの出力 drift) --------------------------


def test_fixture_input_identity_excludes_output():
    """input identity は output_hash を含まず、入力次元のみで同定する。"""
    a = {"kind": "verification", "capture_key": "ck-A", "workflow_id": "wf-1",
         "phase": 6, "label": "Backend:test", "output_hash": "OLD"}
    b = {"kind": "verification", "capture_key": "ck-B", "workflow_id": "wf-1",
         "phase": 6, "label": "Backend:test", "output_hash": "NEW"}
    # capture_key / output_hash が違っても入力が同じなら input identity は一致
    assert fixture_input_identity(a) == fixture_input_identity(b)
    # llm_call は provider/model/purpose/input_hash で同定
    llm = {"audit_id": 1, "provider": "anthropic", "model": "m",
           "purpose": "p", "input_hash": "ih"}
    assert fixture_input_identity(llm).startswith("llm:")
    # どちらでもない → fx: フォールバック
    assert fixture_input_identity({"kind": "x"}).startswith("fx:")


def test_build_eval_replay_result_stable_drift_missing():
    base_stable = {"kind": "verification", "capture_key": "s1",
                   "workflow_id": "wf-1", "phase": 6, "label": "L:a",
                   "output_hash": "H1"}
    base_drift = {"kind": "verification", "capture_key": "d1",
                  "workflow_id": "wf-1", "phase": 6, "label": "L:b",
                  "output_hash": "OLD"}
    base_missing = {"kind": "verification", "capture_key": "m1",
                    "workflow_id": "wf-1", "phase": 6, "label": "L:c",
                    "output_hash": "H3"}
    cur_stable = dict(base_stable, capture_key="s1b")  # 同入力・同出力
    cur_drift = {"kind": "verification", "capture_key": "d2",
                 "workflow_id": "wf-1", "phase": 6, "label": "L:b",
                 "output_hash": "NEW"}  # 同入力・出力変化
    r = build_eval_replay_result(
        [base_stable, base_drift, base_missing], [cur_stable, cur_drift]
    )
    assert r["baseline_count"] == 3
    assert r["current_count"] == 2
    assert len(r["stable"]) == 1
    assert len(r["drift"]) == 1
    assert r["drift"][0]["label"] == "L:b"
    assert r["drift"][0]["baseline_output"] == "OLD"
    assert r["drift"][0]["current_output"] == "NEW"
    assert [f["capture_key"] for f in r["missing"]] == ["m1"]


def test_build_eval_replay_result_collapses_to_latest_output():
    """同一入力に複数 fixture がある場合、created_at 最新の出力で比較する。"""
    base = [{"kind": "verification", "capture_key": "b", "workflow_id": "wf-1",
             "phase": 6, "label": "L", "output_hash": "OLD",
             "created_at": "2026-01-01"}]
    current = [
        {"kind": "verification", "capture_key": "c-old", "workflow_id": "wf-1",
         "phase": 6, "label": "L", "output_hash": "OLD",
         "created_at": "2026-01-01"},
        {"kind": "verification", "capture_key": "c-new", "workflow_id": "wf-1",
         "phase": 6, "label": "L", "output_hash": "NEW",
         "created_at": "2026-05-31"},  # 最新 → こちらが採用され drift
    ]
    r = build_eval_replay_result(base, current)
    assert len(r["drift"]) == 1
    assert r["drift"][0]["current_output"] == "NEW"
    assert r["stable"] == []


def test_build_eval_replay_result_stable_when_identical():
    fx = [{"kind": "verification", "capture_key": "k", "workflow_id": "wf-1",
           "phase": 6, "label": "L", "output_hash": "H"}]
    r = build_eval_replay_result(fx, fx)
    assert len(r["stable"]) == 1
    assert r["drift"] == []
    assert r["missing"] == []
    assert r["out_of_window"] == []


def test_fixture_input_identity_includes_input_hash():
    """同 label でも input_hash が違えば別入力として同定する（Copilot Round 1）。"""
    a = {"kind": "verification", "capture_key": "a", "workflow_id": "wf-1",
         "phase": 6, "label": "L", "input_hash": "IN1", "output_hash": "H"}
    b = dict(a, capture_key="b", input_hash="IN2")
    assert fixture_input_identity(a) != fixture_input_identity(b)


def test_build_eval_replay_result_recency_uses_updated_at():
    """recency は updated_at 優先。古い created_at でも updated_at が新しければ
    その出力を最新として比較する（record_eval_capture は created_at 固定・
    updated_at のみ進む。Copilot Round 1）。"""
    base = [{"kind": "verification", "capture_key": "b", "workflow_id": "wf-1",
             "phase": 6, "label": "L", "output_hash": "OLD",
             "created_at": "2026-01-01", "updated_at": "2026-01-01"}]
    current = [
        # created_at は新しいが updated_at は古い → 採用されない
        {"kind": "verification", "capture_key": "c-newcreate",
         "workflow_id": "wf-1", "phase": 6, "label": "L", "output_hash": "X",
         "created_at": "2026-05-31", "updated_at": "2026-02-01"},
        # created_at は古いが updated_at が最新（古い出力が再観測された）→ 採用
        {"kind": "verification", "capture_key": "c-recent",
         "workflow_id": "wf-1", "phase": 6, "label": "L", "output_hash": "OLD",
         "created_at": "2026-01-01", "updated_at": "2026-06-01"},
    ]
    r = build_eval_replay_result(base, current)
    # updated_at 最新の OLD が現出力 → baseline OLD と一致で stable（drift なし）
    assert len(r["stable"]) == 1
    assert r["drift"] == []


def test_build_eval_replay_result_out_of_window_not_missing():
    """window_start より古く現側に無い baseline 入力は missing でなく
    out_of_window に分類する（--limit truncation の偽陽性防止。Copilot Round 1）。"""
    old = {"kind": "verification", "capture_key": "old", "workflow_id": "wf-1",
           "phase": 6, "label": "L:old", "output_hash": "H",
           "created_at": "2026-01-01"}
    recent = {"kind": "verification", "capture_key": "rec", "workflow_id": "wf-1",
              "phase": 6, "label": "L:rec", "output_hash": "H",
              "created_at": "2026-05-31"}
    # 現側は空（truncation 想定）、window_start は 2026-03-01
    r = build_eval_replay_result(
        [old, recent], [], window_start="2026-03-01"
    )
    assert [f["capture_key"] for f in r["out_of_window"]] == ["old"]
    assert [f["capture_key"] for f in r["missing"]] == ["rec"]


def test_build_eval_replay_result_out_of_window_uses_recency_not_created_at():
    """out_of_window 判定は recency(updated_at) で行う。古い created_at でも
    updated_at が window_start 以降なら missing 扱い（軸混在の回避。Round 2）。"""
    # created_at は古いが updated_at は window 以降 → 再観測されており missing
    reobserved = {"kind": "verification", "capture_key": "ro",
                  "workflow_id": "wf-1", "phase": 6, "label": "L:ro",
                  "output_hash": "H", "created_at": "2026-01-01",
                  "updated_at": "2026-06-01"}
    r = build_eval_replay_result(
        [reobserved], [], window_start="2026-03-01"
    )
    # updated_at(2026-06-01) >= window_start → out_of_window でなく missing
    assert r["out_of_window"] == []
    assert [f["capture_key"] for f in r["missing"]] == ["ro"]


def test_handle_eval_replay_fail_on_drift(store, tmp_path, capsys):
    import argparse
    import json

    from hokusai.cli_main import _handle_eval

    # baseline: 同一入力 (wf-1/phase6/Backend:test) の出力 OLD
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps({"fixtures": [{
        "kind": "verification", "capture_key": "ck-old",
        "workflow_id": "wf-1", "phase": 6, "label": "Backend:test",
        "output_hash": "OLD", "status": "fail",
    }]}))
    # 現 DB: 同一入力だが出力 NEW → drift
    store.record_eval_capture(
        capture_key="ck-new", workflow_id="wf-1", phase=6, kind="verification",
        label="Backend:test", output_hash="NEW", status="fail",
    )

    class _Cfg:
        database_path = store.db_path

    # --fail-on-drift あり → exit 1
    rc = _handle_eval(
        argparse.Namespace(eval_subcommand="replay", baseline=str(baseline),
                           workflow_id=None, limit=10000,
                           fail_on_drift=True, output="json"),
        _Cfg(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert len(payload["drift"]) == 1
    assert payload["drift"][0]["baseline_output"] == "OLD"
    assert payload["drift"][0]["current_output"] == "NEW"

    # --fail-on-drift なし → exit 0（報告のみ）
    rc2 = _handle_eval(
        argparse.Namespace(eval_subcommand="replay", baseline=str(baseline),
                           workflow_id=None, limit=10000,
                           fail_on_drift=False, output="text"),
        _Cfg(),
    )
    capsys.readouterr()
    assert rc2 == 0


def test_handle_eval_replay_missing_baseline(store, capsys):
    import argparse

    from hokusai.cli_main import _handle_eval

    class _Cfg:
        database_path = store.db_path

    rc = _handle_eval(
        argparse.Namespace(eval_subcommand="replay",
                           baseline="/nonexistent/base.json",
                           workflow_id=None, limit=10000,
                           fail_on_drift=False, output="text"),
        _Cfg(),
    )
    assert rc == 1
    assert "baseline 読み込み失敗" in capsys.readouterr().err


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


def test_build_eval_gate_result_out_of_window_not_improvement():
    """window_start より古い baseline fail は improvements でなく out_of_window
    に分類される（--limit truncation の偽陽性防止、PR #154 Copilot Round 1）。"""
    old_fail = {"kind": "verification", "capture_key": "ck-old",
                "status": "fail", "created_at": "2026-01-01T00:00:00"}
    recent_fail = {"kind": "verification", "capture_key": "ck-recent",
                   "status": "fail", "created_at": "2026-05-31T00:00:00"}
    baseline = [old_fail, recent_fail]
    current = []  # 現側ウィンドウには何も無い（truncation 想定）
    r = build_eval_gate_result(
        baseline, current, window_start="2026-03-01T00:00:00"
    )
    # 古い方は out_of_window（improvements に入れない）
    assert [f["capture_key"] for f in r["out_of_window"]] == ["ck-old"]
    # window 内の recent は removed → improvements
    assert [f["capture_key"] for f in r["improvements"]] == ["ck-recent"]


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
