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


def test_phase7_node_appends_payloads_to_pending_review_issues(monkeypatch):
    """phase7_review_node 自体が helper の戻り値を state["pending_review_issues"]
    に append することを検証（PR #37 Copilot 7 回目指摘: helper 単体テストだけだと
    node が helper を呼び忘れ／append 漏れの regression を検出できない）。"""
    from hokusai.nodes import phase7_review

    # 必要な前提を最小限 mock
    monkeypatch.setattr(phase7_review, "should_skip_phase", lambda s, p: False)
    monkeypatch.setattr(
        phase7_review,
        "resolve_runtime_repositories",
        lambda state, config: [type("Repo", (), {"name": "Backend", "path": __import__("pathlib").Path("/tmp")})()],
    )
    monkeypatch.setattr(
        phase7_review,
        "_review_all_repositories",
        lambda repos, prompt, timeout: {
            "Backend": {
                "passed": False,
                "issues": ["NG: P01 missing csrf"],
                "rules": {
                    "P01": {"name": "Secure default", "result": "NG", "note": "missing csrf"},
                },
            },
        },
    )
    monkeypatch.setattr(
        phase7_review,
        "_load_builtin_checklist",
        lambda: "## P01 required\n",
    )
    monkeypatch.setattr(
        phase7_review,
        "_extract_required_rule_ids",
        lambda content: [],
    )
    # config も最小限の attrs を持つ stub
    fake_config = type(
        "Cfg",
        (),
        {
            "skill_timeout": 60,
            "max_retry_count": 3,
            "review_checklist": {},
        },
    )()
    monkeypatch.setattr(phase7_review, "get_config", lambda: fake_config)
    # add_audit_log / update_phase_status / update_repository_phase_status は state を返す薄い op で代替
    monkeypatch.setattr(phase7_review, "add_audit_log", lambda s, *a, **kw: s)
    monkeypatch.setattr(phase7_review, "update_phase_status", lambda s, *a, **kw: s)
    monkeypatch.setattr(
        phase7_review, "update_repository_phase_status", lambda s, *a, **kw: s
    )
    # _build_review_prompt は副作用なしの薄いプロンプト生成
    monkeypatch.setattr(phase7_review, "_build_review_prompt", lambda *a, **kw: "prompt")

    state = {
        "workflow_id": "wf-test",
        "phases": {7: {"retry_count": 0}},
    }
    new_state = phase7_review.phase7_review_node(state)  # type: ignore[arg-type]

    pending = new_state.get("pending_review_issues") or []
    assert len(pending) == 1
    assert pending[0]["source"] == "final_review"
    assert pending[0]["rule"] == "P01"
    assert pending[0]["repository"] == "Backend"
    assert "dedupe_key" in pending[0]


def test_phase6_node_appends_payloads_to_pending_review_issues(monkeypatch):
    """phase6_verify_node 自体が verification 失敗時に
    state["pending_review_issues"] を populate することを検証（PR #37 Copilot
    7 回目指摘）。"""
    from hokusai.nodes import phase6_verify
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setattr(phase6_verify, "should_skip_phase", lambda s, p: False)
    monkeypatch.setattr(
        phase6_verify,
        "resolve_runtime_repositories",
        lambda state, config: [
            SimpleNamespace(
                name="Backend",
                path=Path("/tmp"),
                source_path=Path("/tmp"),
                base_branch="main",
                worktree_created=False,
                build_command="echo build",
                test_command="echo test",
                lint_command="echo lint",
            )
        ],
    )

    # 全コマンドが失敗するシナリオ
    def _fake_run(cmd, cwd, timeout):
        return SimpleNamespace(
            success=False,
            stdout="==== test session starts ====\nFAILED tests/x.py\n",
            stderr="",
            return_code=1,
            timed_out=False,
        )

    monkeypatch.setattr(phase6_verify, "_run_command_with_output", _fake_run)
    monkeypatch.setattr(phase6_verify, "_analyze_failures", lambda *a, **kw: None)
    monkeypatch.setattr(phase6_verify, "get_config", lambda: SimpleNamespace(
        command_timeout=60,
        max_retry_count=3,
        build_command=None,
        test_command=None,
        lint_command=None,
    ))
    monkeypatch.setattr(phase6_verify, "get_repository_state", lambda s, n: None)
    monkeypatch.setattr(phase6_verify, "init_repository_state", lambda **kw: {})
    monkeypatch.setattr(phase6_verify, "add_audit_log", lambda s, *a, **kw: s)
    monkeypatch.setattr(phase6_verify, "update_phase_status", lambda s, *a, **kw: s)
    monkeypatch.setattr(
        phase6_verify, "update_repository_phase_status", lambda s, *a, **kw: s
    )

    state = {
        "workflow_id": "wf-test",
        "phases": {6: {"retry_count": 0}, 7: {"status": "pending"}, 8: {"status": "pending"}},
        "branch_name": "feature/x",
        "total_retry_count": 0,
    }
    new_state = phase6_verify.phase6_verify_node(state)  # type: ignore[arg-type]

    pending = new_state.get("pending_review_issues") or []
    # build/test/lint 3 つすべて失敗するので 3 件の payload
    assert len(pending) == 3
    for p in pending:
        assert p["source"] == "verification_failure"
        assert p["repository"] == "Backend"
        assert p["rule"] in {"build", "test", "lint"}
        assert "dedupe_key" in p


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
