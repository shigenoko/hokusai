"""Phase 6 / Phase 7 から Review Issues DB へのキュー積み込みテスト（#36 / v0.5.0）

各 phase ノードが state["pending_review_issues"] に正しい構造の payload を
追加するかを検証する。Notion 同期そのものは dispatcher 側のテストで担保する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.nodes.phase6_verify import _build_verification_review_issue_payloads
from hokusai.nodes.phase7_review import _build_review_issue_payloads


# ---------------------------------------------------------------------------
# Phase 7: _build_review_issue_payloads
# ---------------------------------------------------------------------------


def test_phase7_payloads_emits_one_per_ng_rule():
    review_by_repo = {
        "Backend": {
            "passed": False,
            "issues": ["[Backend] ルール NG"],
            "rules": {
                "P01": {"name": "Secure default", "result": "NG", "note": "missing csrf"},
                "P02": {"name": "Lint pass", "result": "OK", "note": ""},
                "P03": {"name": "Type pass", "result": "NG", "note": ""},
            },
        },
        "Frontend": {
            "passed": True,
            "issues": [],
            "rules": {
                "P01": {"name": "Secure default", "result": "OK", "note": ""},
            },
        },
    }
    state = {"workflow_id": "wf-123"}
    payloads = _build_review_issue_payloads(review_by_repo, state)

    # NG ルール 2 件
    assert len(payloads) == 2

    p01 = next(p for p in payloads if p["rule"] == "P01")
    assert p01["source"] == "final_review"
    assert p01["repository"] == "Backend"
    assert p01["severity"] == "high"
    assert p01["status"] == "open"
    assert p01["workflow_id"] == "wf-123"
    assert "Secure default" in p01["message"]
    assert "missing csrf" in p01["message"]

    p03 = next(p for p in payloads if p["rule"] == "P03")
    # note が空でも message は name のみで作られる
    assert p03["message"] == "Type pass"


def test_phase7_payloads_skips_ok_rules():
    review_by_repo = {
        "Backend": {
            "passed": True,
            "issues": [],
            "rules": {
                "P01": {"name": "x", "result": "OK", "note": ""},
                "P02": {"name": "y", "result": "OK", "note": ""},
            },
        },
    }
    payloads = _build_review_issue_payloads(review_by_repo, {"workflow_id": "wf"})
    assert payloads == []


def test_phase7_payloads_does_not_include_operator():
    """operator は workflow.py の drain で補うので payload には含めない"""
    review_by_repo = {
        "Backend": {
            "passed": False,
            "issues": [],
            "rules": {
                "P01": {"name": "x", "result": "NG", "note": ""},
            },
        },
    }
    payloads = _build_review_issue_payloads(review_by_repo, {"workflow_id": "wf"})
    assert "operator" not in payloads[0]


def test_phase7_payloads_include_dedupe_key_with_repository():
    """payload に dedupe_key を含め、repository が hash 入力に反映される（PR #37 Copilot 指摘）"""
    review_by_repo = {
        "Backend": {
            "passed": False,
            "issues": [],
            "rules": {
                "P01": {"name": "x", "result": "NG", "note": ""},
            },
        },
        "Frontend": {
            "passed": False,
            "issues": [],
            "rules": {
                "P01": {"name": "x", "result": "NG", "note": ""},
            },
        },
    }
    payloads = _build_review_issue_payloads(review_by_repo, {"workflow_id": "wf"})
    assert len(payloads) == 2
    backend = next(p for p in payloads if p["repository"] == "Backend")
    frontend = next(p for p in payloads if p["repository"] == "Frontend")
    # dedupe_key が含まれる
    assert "dedupe_key" in backend
    assert "dedupe_key" in frontend
    # 16 hex 文字
    assert len(backend["dedupe_key"]) == 16
    # 同じ source / rule / message でも repository が違うと別キー
    assert backend["dedupe_key"] != frontend["dedupe_key"]


# ---------------------------------------------------------------------------
# Phase 6: _build_verification_review_issue_payloads
# ---------------------------------------------------------------------------


def test_phase6_payloads_emits_one_per_failed_entry():
    errors = [
        {"repository": "Backend", "command": "build", "success": True, "error_output": None},
        {
            "repository": "Backend",
            "command": "test",
            "success": False,
            "error_output": "FAIL: tests/test_auth.py::test_login\n  AssertionError: 401",
        },
        {
            "repository": "Frontend",
            "command": "lint",
            "success": False,
            "error_output": "error: 'useEffect' is missing in deps",
        },
    ]
    state = {"workflow_id": "wf-456"}
    payloads = _build_verification_review_issue_payloads(errors, state)

    assert len(payloads) == 2

    p_test = next(p for p in payloads if p["rule"] == "test")
    assert p_test["source"] == "verification_failure"
    assert p_test["repository"] == "Backend"
    assert p_test["severity"] == "high"
    assert p_test["status"] == "open"
    # message は error_output の先頭行
    assert "tests/test_auth.py" in p_test["message"]

    p_lint = next(p for p in payloads if p["rule"] == "lint")
    assert p_lint["repository"] == "Frontend"
    assert "useEffect" in p_lint["message"]


def test_phase6_payloads_uses_fallback_message_when_no_error_output():
    errors = [
        {"repository": "Backend", "command": "lint", "success": False, "error_output": None},
        {"repository": "Backend", "command": "test", "success": False, "error_output": ""},
    ]
    payloads = _build_verification_review_issue_payloads(errors, {"workflow_id": "wf"})
    assert len(payloads) == 2
    for p in payloads:
        assert "failed" in p["message"]


def test_phase6_payloads_skips_successful_entries():
    errors = [
        {"repository": "Backend", "command": "build", "success": True, "error_output": None},
        {"repository": "Backend", "command": "test", "success": True, "error_output": None},
    ]
    payloads = _build_verification_review_issue_payloads(errors, {"workflow_id": "wf"})
    assert payloads == []


def test_phase6_payloads_include_dedupe_key_with_repository():
    """payload に dedupe_key を含め、repository 違いで別キーになる（PR #37 Copilot 指摘）"""
    errors = [
        {
            "repository": "Backend",
            "command": "build",
            "success": False,
            "error_output": "same error message",
        },
        {
            "repository": "Frontend",
            "command": "build",
            "success": False,
            "error_output": "same error message",
        },
    ]
    payloads = _build_verification_review_issue_payloads(errors, {"workflow_id": "wf"})
    assert len(payloads) == 2
    backend = next(p for p in payloads if p["repository"] == "Backend")
    frontend = next(p for p in payloads if p["repository"] == "Frontend")
    assert "dedupe_key" in backend
    assert "dedupe_key" in frontend
    assert backend["dedupe_key"] != frontend["dedupe_key"]


def test_phase6_dedupe_uses_full_error_output_not_just_first_line():
    """先頭行が同じバナーで詳細が違う失敗を別レコードとして扱う

    PR #37 Copilot 2 回目指摘: test runner が共通バナーを先頭行に出すと、
    別ケースが同じ Notion ページに集約されてしまう。
    """
    banner = "==== test session starts ===="
    errors = [
        {
            "repository": "Backend",
            "command": "test",
            "success": False,
            "error_output": f"{banner}\nFAILED tests/test_a.py::test_alpha",
        },
        {
            "repository": "Backend",
            "command": "test",
            "success": False,
            "error_output": f"{banner}\nFAILED tests/test_b.py::test_beta",
        },
    ]
    payloads = _build_verification_review_issue_payloads(errors, {"workflow_id": "wf"})
    assert len(payloads) == 2
    # message は先頭行（表示用、両方同じバナー）
    assert payloads[0]["message"] == banner
    assert payloads[1]["message"] == banner
    # dedupe_key は detail 込みで違う（衝突回避）
    assert payloads[0]["dedupe_key"] != payloads[1]["dedupe_key"]
