"""LLM Gateway interceptor への workflow_id / phase 伝播テスト（F4 案 A2）

F4 案 A2 で各 phase node 内の client 呼び出し
（claude.execute_skill / claude.execute_prompt / client.review_document）
に `workflow_id=state.get("workflow_id") or None, phase=<phase 番号>`
を渡す配線を入れた。本テストはその配線が回帰しないことを保証する。

各 phase node の関連 client メソッドを mock し、`workflow_id` / `phase`
が期待値で client に届くことを assert する。phase 番号は phase node ごとに
ハードコード（state["current_phase"] を見ない）なので、テストも同 phase
番号で期待値を組む。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 注記: phase2_research / phase3_design / phase4_plan / phase5_implement /
# phase8/review_fix の entry point ノードは Notion / config / git client 等の
# 依存が深く、軽量 mock で「execute_prompt が呼ばれた」だけを assert する
# テストを書くと依存が崩れて何も検証されない（PR #121 Copilot Round 1 指摘）。
# それらの phase node については本ファイルでは個別 unit テストを置かず、
# 配線は (1) phase7_review の helper unit テスト + (2) cross_review の unit
# テストで代表させ、phase2-5/8 はコードレビューで担保する方針とする。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# phase7_review: _review_all_repositories → _review_single_repo →
#                execute_prompt に workflow_id/phase=7 が届く
# ---------------------------------------------------------------------------

def test_phase7_review_single_repo_passes_workflow_id_to_execute_prompt(tmp_path):
    """_review_single_repo(workflow_id=..., phase=7) → execute_prompt に伝播"""
    from hokusai.nodes import phase7_review

    mock_claude = MagicMock()
    mock_claude.execute_prompt.return_value = "## レビュー結果\nP01: OK\n"

    with patch("hokusai.nodes.phase7_review.ClaudeCodeClient",
               return_value=mock_claude), \
         patch("hokusai.nodes.phase7_review._parse_review_result",
               return_value={"passed": True, "issues": [], "rules": {}}):
        phase7_review._review_single_repo(
            repo_name="Backend",
            repo_path=tmp_path,
            review_prompt="please review",
            timeout=10,
            workflow_id="wf-phase7-test",
            phase=7,
        )

    assert mock_claude.execute_prompt.called
    kwargs = mock_claude.execute_prompt.call_args.kwargs
    assert kwargs["workflow_id"] == "wf-phase7-test"
    assert kwargs["phase"] == 7


def test_phase7_review_all_repositories_forwards_workflow_id(tmp_path):
    """_review_all_repositories → _review_single_repo に workflow_id を中継する"""
    from hokusai.nodes import phase7_review

    repo = type("Repo", (), {"name": "Backend", "path": tmp_path})()

    with patch.object(phase7_review, "_review_single_repo") as mock_single:
        mock_single.return_value = {"passed": True, "issues": [], "rules": {}}

        phase7_review._review_all_repositories(
            repositories=[repo],
            review_prompt="prompt",
            timeout=10,
            workflow_id="wf-phase7-all",
            phase=7,
        )

    assert mock_single.called
    kwargs = mock_single.call_args.kwargs
    assert kwargs["workflow_id"] == "wf-phase7-all"
    assert kwargs["phase"] == 7


# ---------------------------------------------------------------------------
# utils/cross_review: client.review_document に workflow_id/phase が届く
# ---------------------------------------------------------------------------

def test_cross_review_passes_workflow_id_to_review_document(monkeypatch):
    """cross_review(state, phase, document) → review_document に伝播"""
    from hokusai.utils import cross_review as cr_module

    mock_client = MagicMock()
    mock_client.review_document.return_value = {
        "findings": [],
        "overall_assessment": "approve",
        "summary": "ok",
    }

    # cross_review 関数の内部依存を mock
    monkeypatch.setattr(cr_module, "_create_review_client",
                        lambda config: mock_client)

    # config: cross_review.enabled=True / phase included
    class _CR:
        enabled = True
        phases = [3]
        provider = "codex"
        on_failure = "skip"
        max_correction_rounds = 1
        max_findings = 100
        min_confidence = 0.0

    class _Cfg:
        cross_review = _CR()

    monkeypatch.setattr(cr_module, "get_config", lambda: _Cfg())

    state = {
        "workflow_id": "wf-cross-test",
        "audit_log": [],
        "cross_review_results": {},
        "cross_review_statuses": {},
    }
    try:
        cr_module.execute_cross_review(state, phase=3, document="hello world")
    except Exception:
        # cross_review の他の post 処理（Notion 保存等）が失敗しても
        # review_document 呼び出しが起きていればこのテストの目的は達せる
        pass

    assert mock_client.review_document.called
    kwargs = mock_client.review_document.call_args.kwargs
    assert kwargs.get("workflow_id") == "wf-cross-test"
    assert kwargs.get("phase") == 3


# ---------------------------------------------------------------------------
# 共通: state.workflow_id が空 / 未設定なら None が helper まで届く（後方互換）
# ---------------------------------------------------------------------------

def test_phase7_single_repo_empty_workflow_id_becomes_none(tmp_path):
    """workflow_id 引数が None でも helper まで None が届く（既存挙動維持）"""
    from hokusai.nodes import phase7_review

    mock_claude = MagicMock()
    mock_claude.execute_prompt.return_value = ""

    with patch("hokusai.nodes.phase7_review.ClaudeCodeClient",
               return_value=mock_claude), \
         patch("hokusai.nodes.phase7_review._parse_review_result",
               return_value={"passed": True, "issues": [], "rules": {}}):
        phase7_review._review_single_repo(
            repo_name="Backend",
            repo_path=tmp_path,
            review_prompt="prompt",
            timeout=10,
            # workflow_id / phase 未指定（default None）
        )

    kwargs = mock_claude.execute_prompt.call_args.kwargs
    assert kwargs["workflow_id"] is None
    assert kwargs["phase"] is None
