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
# 配線テストの分布（PR #121 Copilot Round 2 指摘を受けて再構成）:
# - phase2/3/4: 既存 entry-point テスト (`tests/test_phase{2,3,4}_*.py`) が
#   `ClaudeCodeClient.execute_prompt/execute_skill` を mock 済みなので、
#   そちらの既存テストに workflow_id/phase の kwargs assert を追記して担保。
# - phase5/8: 既存テストが薄いため、本ファイルで `_execute_implementation` /
#   `_auto_fix_review_comments` を直接呼ぶ unit テストを追加して担保。
# - phase7: 既に helper (`_review_single_repo` / `_review_all_repositories`)
#   が切り出されているので本ファイルで直接 unit テスト。
# - cross_review: 内部依存を mock した unit テスト。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# phase5_implement: _execute_implementation → execute_prompt に
#                   workflow_id/phase=5 が届く
# ---------------------------------------------------------------------------

def test_phase5_execute_implementation_passes_workflow_id(monkeypatch, tmp_path):
    """`_execute_implementation` 内の execute_prompt 呼び出しに workflow_id/phase=5 が届く"""
    from hokusai.nodes import phase5_implement

    mock_claude = MagicMock()
    mock_claude.execute_prompt.return_value = "implementation result"

    # ClaudeCodeClient と各依存を mock
    monkeypatch.setattr(phase5_implement, "ClaudeCodeClient",
                        lambda working_dir=None: mock_claude)
    monkeypatch.setattr(phase5_implement, "_build_implementation_prompt",
                        lambda *a, **kw: "implementation prompt")
    monkeypatch.setattr(phase5_implement, "_build_retry_prompt",
                        lambda *a, **kw: "retry prompt")

    repo = type("Repo", (), {"name": "Backend", "path": tmp_path})()
    monkeypatch.setattr(phase5_implement, "resolve_runtime_repositories",
                        lambda state, config: [repo])

    # `_commit_and_push` などコミット系を no-op に
    monkeypatch.setattr(phase5_implement, "_commit_and_push",
                        lambda *a, **kw: None)

    class _Cfg:
        skill_timeout = 300
        project_root = tmp_path

    monkeypatch.setattr(phase5_implement, "get_config", lambda: _Cfg())

    state = {
        "workflow_id": "wf-phase5-test",
        "audit_log": [],
        "branch_name": "",  # _commit_and_push を skip させる
        "repositories": [],
    }
    try:
        phase5_implement._execute_implementation(
            state, is_retry=False, phase7_retry_count=0, phase6_retry_count=0,
        )
    except Exception:
        # 周辺依存（state 操作 / git 検証）で失敗してもこのテストの目的
        # （execute_prompt の kwargs 検証）は execute_prompt 呼び出しが
        # 発火していれば達せる
        pass

    assert mock_claude.execute_prompt.called
    kwargs = mock_claude.execute_prompt.call_args.kwargs
    assert kwargs.get("workflow_id") == "wf-phase5-test"
    assert kwargs.get("phase") == 5


# ---------------------------------------------------------------------------
# phase8/review_fix: _auto_fix_review_comments → execute_prompt に
#                   workflow_id/phase=8 が届く
# ---------------------------------------------------------------------------

def test_phase8_auto_fix_review_comments_passes_workflow_id(monkeypatch, tmp_path):
    """`_auto_fix_review_comments` 内の execute_prompt に workflow_id/phase=8 が届く"""
    from hokusai.nodes.phase8 import review_fix

    mock_claude = MagicMock()
    mock_claude.execute_prompt.return_value = "fixed"

    mock_git = MagicMock()
    mock_git.has_uncommitted_changes.return_value = False  # 早期 return さ せる

    monkeypatch.setattr(review_fix, "ClaudeCodeClient",
                        lambda working_dir=None: mock_claude)
    monkeypatch.setattr(review_fix, "GitClient", lambda path: mock_git)
    monkeypatch.setattr(review_fix, "_build_review_fix_prompt",
                        lambda comments, pr_number, repo_name: "fix prompt")

    class _Repo:
        path = tmp_path
        name = "Backend"

    class _Cfg:
        skill_timeout = 300
        project_root = tmp_path

    monkeypatch.setattr(review_fix, "get_config", lambda: _Cfg())
    monkeypatch.setattr(review_fix, "get_runtime_repository",
                        lambda state, config, repo_name: _Repo())

    state = {
        "workflow_id": "wf-phase8-test",
        "branch_name": "feature/test",
        "audit_log": [],
    }
    current_pr = {"number": 1, "repo_name": "Backend"}
    comments = [{"id": 1, "body": "test", "path": "x.py", "line": 1}]

    review_fix._auto_fix_review_comments(state, current_pr, comments)

    assert mock_claude.execute_prompt.called
    kwargs = mock_claude.execute_prompt.call_args.kwargs
    assert kwargs.get("workflow_id") == "wf-phase8-test"
    assert kwargs.get("phase") == 8


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
