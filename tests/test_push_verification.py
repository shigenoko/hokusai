"""
Tests for push verification in review wait nodes.

修正フローからの再開時に、新コミットのプッシュを検証する機能のテスト。
レビュー指摘の自動修正テストも含む。
"""

import pytest
from unittest.mock import Mock, patch

from hokusai.state import WorkflowState, get_current_pr, update_pr_in_list
from hokusai.nodes.phase8.review_wait import (
    _resume_review_wait,
    phase8b_unified_wait_node,
)
from hokusai.nodes.phase8.review_check import (
    phase8d_unified_fix_node,
    phase8d_copilot_fix_node,
    phase8h_human_fix_node,
)
from hokusai.nodes.phase8.review_fix import (
    _build_review_fix_prompt,
    _parse_fix_summaries,
    _auto_fix_review_comments,
)


def _make_state_with_pr(
    human_input_request: str = "review_fix",
    commit_count_before_fix: int | None = 5,
    unreplied_comments: bool = True,
) -> WorkflowState:
    """テスト用の状態を作成"""
    pr = {
        "url": "https://github.com/owner/repo/pull/123",
        "number": 123,
        "repo_name": "Backend",
        "copilot_comments": [],
        "human_comments": [],
    }
    if commit_count_before_fix is not None:
        pr["commit_count_before_fix"] = commit_count_before_fix

    if unreplied_comments:
        pr["copilot_comments"] = [
            {"id": 1, "body": "Fix this", "replied": False, "author": "copilot[bot]"},
        ]

    from hokusai.state import PhaseStatus, PhaseState

    phases = {}
    for i in range(1, 11):
        phases[i] = PhaseState(
            status=PhaseStatus.PENDING.value,
            started_at=None,
            completed_at=None,
            error_message=None,
            retry_count=0,
        )
    phases[9]["status"] = PhaseStatus.IN_PROGRESS.value

    from datetime import datetime

    now = datetime.now().isoformat()

    state = WorkflowState(
        workflow_id="wf-test-push",
        task_url="https://notion.so/test",
        task_title="Test",
        branch_name="feature/test",
        base_branch="main",
        current_phase=9,
        run_mode="step",
        phases=phases,
        schema_change_required=False,
        schema_pr_url=None,
        schema_pr_merged=False,
        pull_requests=[pr],
        current_pr_index=0,
        verification={"build": "not_run", "test": "not_run", "lint": "not_run"},
        verification_errors=[],
        repository_status={},
        repositories=[],
        final_review_passed=False,
        final_review_issues=[],
        final_review_rules={},
        final_review_by_repo={},
        research_result=None,
        work_plan=None,
        implementation_result=None,
        expected_changed_files=[],
        branch_hygiene_issues=[],
        cherry_picked_from=None,
        cherry_picked_commits=[],
        created_at=now,
        updated_at=now,
        total_retry_count=0,
        audit_log=[],
        waiting_for_human=True,
        human_input_request=human_input_request,
        last_environment_error=None,
        waiting_for_pr_approval=False,
        pr_ready_for_review=False,
        waiting_for_copilot_review=False,
        copilot_review_passed=False,
        copilot_review_comments=[],
        copilot_fix_requested=False,
        waiting_for_human_review=False,
        human_review_passed=False,
        human_review_comments=[],
        human_fix_requested=False,
        review_fix_requested=False,
        cross_review_results={},
    )
    return state


class TestPushVerification:
    """プッシュ検証のテスト"""

    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_blocks_reply_when_no_new_commits(self, mock_get_client):
        """新コミットなし → 自動返信をブロック"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5  # 変化なし
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(
            human_input_request="review_fix",
            commit_count_before_fix=5,
        )

        result = phase8b_unified_wait_node(state)

        assert result["waiting_for_human"] is True
        assert result["push_verification_failed"] is True
        assert result["human_input_request"] == "review_fix"
        # 自動返信は呼ばれない（_reply_to_all_comments が実行されない）

    @patch("hokusai.nodes.phase8.review_wait._reply_to_all_comments")
    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_allows_reply_when_new_commits(self, mock_get_client, mock_reply):
        """新コミットあり → 自動返信を許可"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 6  # 1コミット増加
        mock_get_client.return_value = mock_client
        mock_reply.return_value = []

        state = _make_state_with_pr(
            human_input_request="review_fix",
            commit_count_before_fix=5,
        )

        result = phase8b_unified_wait_node(state)

        assert result.get("push_verification_failed") is False
        assert result["waiting_for_human"] is False

    @patch("hokusai.nodes.phase8.review_wait._reply_to_all_comments")
    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_skips_verification_on_recheck(self, mock_get_client, mock_reply):
        """review_status再開時 → 検証スキップ"""
        mock_client = Mock()
        mock_get_client.return_value = mock_client
        mock_reply.return_value = []

        state = _make_state_with_pr(
            human_input_request="review_status",
            commit_count_before_fix=5,
        )

        result = phase8b_unified_wait_node(state)

        # プッシュ検証はスキップされる
        assert result.get("push_verification_failed") is False
        mock_client.get_pr_commit_count.assert_not_called()

    @patch("hokusai.nodes.phase8.review_wait._reply_to_all_comments")
    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_skips_verification_when_no_baseline(self, mock_get_client, mock_reply):
        """ベースラインなし → 検証スキップ（後方互換）"""
        mock_client = Mock()
        mock_get_client.return_value = mock_client
        mock_reply.return_value = []

        state = _make_state_with_pr(
            human_input_request="review_fix",
            commit_count_before_fix=None,  # ベースラインなし
        )

        result = phase8b_unified_wait_node(state)

        # ベースラインがないため検証スキップ
        assert result.get("push_verification_failed") is False
        mock_client.get_pr_commit_count.assert_not_called()

    @patch("hokusai.nodes.phase8.review_fix._auto_fix_review_comments", return_value=False)
    @patch("hokusai.nodes.phase8.review_check._get_git_client_for_pr")
    def test_records_baseline_on_fix_request(self, mock_get_client, mock_auto_fix):
        """修正依頼時にベースラインを記録（自動修正失敗時）"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 7
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(
            human_input_request=None,
            commit_count_before_fix=None,
            unreplied_comments=True,
        )
        # review_check のコメント数用の state を設定
        state["copilot_review_comments"] = [
            {"id": 1, "body": "Fix this", "replied": False, "author": "copilot[bot]"},
        ]

        result = phase8d_unified_fix_node(state)

        # ベースラインが記録された
        current_pr = get_current_pr(result)
        assert current_pr["commit_count_before_fix"] == 7
        assert result["waiting_for_human"] is True
        assert result["human_input_request"] == "review_fix"


class TestPushVerificationLegacy:
    """レガシーフロー（_resume_review_wait）のプッシュ検証テスト"""

    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_blocks_reply_when_no_new_commits_copilot(self, mock_get_client):
        """Copilotフロー: 新コミットなし → ブロック"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(
            human_input_request="copilot_fix",
            commit_count_before_fix=5,
        )

        result = _resume_review_wait(state, "copilot")

        assert result["waiting_for_human"] is True
        assert result["push_verification_failed"] is True

    @patch("hokusai.nodes.phase8.review_wait._reply_to_all_comments")
    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_allows_reply_when_new_commits_human(self, mock_get_client, mock_reply):
        """人間フロー: 新コミットあり → 許可"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 8
        mock_get_client.return_value = mock_client
        mock_reply.return_value = []

        state = _make_state_with_pr(
            human_input_request="human_fix",
            commit_count_before_fix=5,
        )

        result = _resume_review_wait(state, "human")

        assert result.get("push_verification_failed") is False


class TestAutoFixSkipsPushVerification:
    """自動修正時のプッシュ検証スキップのテスト"""

    @patch("hokusai.nodes.phase8.review_wait._reply_to_all_comments")
    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_unified_wait_skips_verification_on_auto_fix(self, mock_get_client, mock_reply):
        """自動修正後（auto_fix_attempts > 0）→ プッシュ検証スキップ"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5  # 変化なし（APIラグ）
        mock_get_client.return_value = mock_client
        mock_reply.return_value = []

        state = _make_state_with_pr(
            human_input_request="review_fix",
            commit_count_before_fix=5,
        )
        state["auto_fix_attempts"] = 1  # 自動修正済み

        result = phase8b_unified_wait_node(state)

        # プッシュ検証はスキップされるべき
        assert result.get("push_verification_failed") is False
        assert result["waiting_for_human"] is False
        mock_client.get_pr_commit_count.assert_not_called()

    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_unified_wait_verifies_on_human_fix(self, mock_get_client):
        """人間修正後（auto_fix_attempts == 0）→ プッシュ検証実行"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5  # 変化なし
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(
            human_input_request="review_fix",
            commit_count_before_fix=5,
        )
        state["auto_fix_attempts"] = 0  # 人間修正

        result = phase8b_unified_wait_node(state)

        # プッシュ検証は実行され、失敗する
        assert result["waiting_for_human"] is True
        assert result["push_verification_failed"] is True
        mock_client.get_pr_commit_count.assert_called_once()

    @patch("hokusai.nodes.phase8.review_wait._get_git_client_for_pr")
    def test_legacy_wait_skips_verification_on_auto_fix(self, mock_get_client):
        """レガシーフロー: 自動修正後 → プッシュ検証スキップ"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5  # 変化なし
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(
            human_input_request="copilot_fix",
            commit_count_before_fix=5,
        )
        state["auto_fix_attempts"] = 1

        result = _resume_review_wait(state, "copilot")

        assert result.get("push_verification_failed") is False
        assert result["waiting_for_human"] is False
        mock_client.get_pr_commit_count.assert_not_called()


class TestParseFixSummaries:
    """対応レポートのパーステスト"""

    def test_parse_standard_format(self):
        """標準形式のパース"""
        response = """修正が完了しました。

指摘1: OkResponseを再利用するようリファクタリングしました
指摘2: PRタイトルをインラインスキーマの$ref置換に修正しました
指摘3: 仕様上の判断のためスキップ"""
        result = _parse_fix_summaries(response, 3)
        assert result == {
            0: "OkResponseを再利用するようリファクタリングしました",
            1: "PRタイトルをインラインスキーマの$ref置換に修正しました",
            2: "仕様上の判断のためスキップ",
        }

    def test_parse_with_code_block(self):
        """コードブロック内の形式"""
        response = """```
指摘1: 冗長なスキーマを削除しました
指摘 2: $refに置換しました
```"""
        result = _parse_fix_summaries(response, 2)
        assert len(result) == 2
        assert result[0] == "冗長なスキーマを削除しました"
        assert result[1] == "$refに置換しました"

    def test_parse_no_match(self):
        """マッチなしの場合は空dict"""
        result = _parse_fix_summaries("修正しました。", 3)
        assert result == {}

    def test_parse_out_of_range_ignored(self):
        """範囲外の指摘番号は無視"""
        response = "指摘0: これは無視\n指摘1: これは有効\n指摘5: これも無視"
        result = _parse_fix_summaries(response, 2)
        assert result == {0: "これは有効"}

    def test_parse_fullwidth_colon(self):
        """全角コロンも対応"""
        response = "指摘1：全角コロンで記載"
        result = _parse_fix_summaries(response, 1)
        assert result == {0: "全角コロンで記載"}


class TestAutoFixReviewComments:
    """レビュー指摘の自動修正テスト"""

    def test_build_review_fix_prompt(self):
        """プロンプト構築のテスト"""
        comments = [
            {"id": 1, "body": "Use logger instead of print", "path": "src/main.py", "line": 42, "author": "copilot[bot]"},
            {"id": 2, "body": "Missing type annotation", "path": "src/utils.py", "line": 10, "author": "reviewer"},
        ]
        prompt = _build_review_fix_prompt(comments, pr_number=123, repo_name="Backend")

        assert "PR #123" in prompt
        assert "src/main.py" in prompt
        assert "42" in prompt
        assert "Use logger instead of print" in prompt
        assert "src/utils.py" in prompt
        assert "Missing type annotation" in prompt
        assert "git のコミットやプッシュは行わないでください" in prompt

    def test_build_review_fix_prompt_missing_fields(self):
        """フィールドが欠けているコメントでもプロンプトが構築できる"""
        comments = [
            {"id": 1, "body": "Fix this"},
        ]
        prompt = _build_review_fix_prompt(comments, pr_number=42)

        assert "PR #42" in prompt
        assert "Fix this" in prompt

    @patch("hokusai.nodes.phase8.review_fix.GitHubHostingClient")
    @patch("hokusai.nodes.phase8.review_fix.ShellRunner")
    @patch("hokusai.nodes.phase8.review_fix.GitClient")
    @patch("hokusai.nodes.phase8.review_fix.ClaudeCodeClient")
    @patch("hokusai.nodes.phase8.review_fix.get_config")
    def test_auto_fix_success_commits_and_pushes(
        self, mock_config, mock_claude_cls, mock_git_cls, mock_shell_cls, mock_hosting_cls,
    ):
        """自動修正成功 → commit&pushが実行される"""
        # config mock
        mock_repo = Mock()
        mock_repo.name = "Backend"
        mock_repo.path = "/tmp/repo"
        mock_cfg = Mock()
        mock_cfg.get_all_repositories.return_value = [mock_repo]
        mock_cfg.skill_timeout = 300
        mock_config.return_value = mock_cfg

        # Claude Code mock
        mock_claude = Mock()
        mock_claude.execute_prompt.return_value = "Fixed the issues"
        mock_claude_cls.return_value = mock_claude

        # Git mock - has uncommitted changes
        mock_git = Mock()
        mock_git.has_uncommitted_changes.return_value = True
        mock_git_cls.return_value = mock_git

        # Shell mock
        mock_shell = Mock()
        mock_shell_cls.return_value = mock_shell

        # GitHubHostingClient mock
        mock_hosting = Mock()
        mock_hosting.push_branch.return_value = True
        mock_hosting_cls.return_value = mock_hosting

        state = _make_state_with_pr()
        current_pr = get_current_pr(state)
        comments = [{"id": 1, "body": "Fix this", "path": "src/main.py", "line": 10, "author": "copilot[bot]"}]

        result = _auto_fix_review_comments(state, current_pr, comments)

        assert result is True
        mock_claude.execute_prompt.assert_called_once()
        # git add -A が呼ばれた
        mock_shell.run_git.assert_any_call("add", "-A", check=True)
        # git commit が呼ばれた
        commit_calls = [c for c in mock_shell.run_git.call_args_list if c[0][0] == "commit"]
        assert len(commit_calls) == 1
        # push が呼ばれた
        mock_hosting.push_branch.assert_called_once_with("feature/test")

    @patch("hokusai.nodes.phase8.review_fix.GitClient")
    @patch("hokusai.nodes.phase8.review_fix.ClaudeCodeClient")
    @patch("hokusai.nodes.phase8.review_fix.get_config")
    def test_auto_fix_no_changes_returns_false(
        self, mock_config, mock_claude_cls, mock_git_cls,
    ):
        """自動修正後に変更なし → False"""
        mock_repo = Mock()
        mock_repo.name = "Backend"
        mock_repo.path = "/tmp/repo"
        mock_cfg = Mock()
        mock_cfg.get_all_repositories.return_value = [mock_repo]
        mock_cfg.skill_timeout = 300
        mock_config.return_value = mock_cfg

        mock_claude = Mock()
        mock_claude.execute_prompt.return_value = "No changes needed"
        mock_claude_cls.return_value = mock_claude

        mock_git = Mock()
        mock_git.has_uncommitted_changes.return_value = False
        mock_git_cls.return_value = mock_git

        state = _make_state_with_pr()
        current_pr = get_current_pr(state)
        comments = [{"id": 1, "body": "Fix this"}]

        result = _auto_fix_review_comments(state, current_pr, comments)

        assert result is False

    @patch("hokusai.nodes.phase8.review_fix.ClaudeCodeClient")
    @patch("hokusai.nodes.phase8.review_fix.get_config")
    def test_auto_fix_exception_returns_false(
        self, mock_config, mock_claude_cls,
    ):
        """自動修正で例外発生 → False"""
        mock_repo = Mock()
        mock_repo.name = "Backend"
        mock_repo.path = "/tmp/repo"
        mock_cfg = Mock()
        mock_cfg.get_all_repositories.return_value = [mock_repo]
        mock_cfg.skill_timeout = 300
        mock_config.return_value = mock_cfg

        mock_claude = Mock()
        mock_claude.execute_prompt.side_effect = Exception("Claude Code error")
        mock_claude_cls.return_value = mock_claude

        state = _make_state_with_pr()
        current_pr = get_current_pr(state)
        comments = [{"id": 1, "body": "Fix this"}]

        result = _auto_fix_review_comments(state, current_pr, comments)

        assert result is False

    @patch("hokusai.nodes.phase8.review_fix._auto_fix_review_comments", return_value=True)
    @patch("hokusai.nodes.phase8.review_check._get_git_client_for_pr")
    def test_unified_fix_auto_fix_success_skips_human_wait(self, mock_get_client, mock_auto_fix):
        """統合フロー: 自動修正成功 → waiting_for_humanを設定しない"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(unreplied_comments=True)
        state["copilot_review_comments"] = [
            {"id": 1, "body": "Fix this", "replied": False, "author": "copilot[bot]"},
        ]

        result = phase8d_unified_fix_node(state)

        assert result.get("waiting_for_human") is not True
        assert result.get("auto_fix_attempts") == 1
        mock_auto_fix.assert_called_once()

    @patch("hokusai.nodes.phase8.review_fix._auto_fix_review_comments", return_value=False)
    @patch("hokusai.nodes.phase8.review_check._get_git_client_for_pr")
    def test_unified_fix_auto_fix_failure_falls_back_to_human(self, mock_get_client, mock_auto_fix):
        """統合フロー: 自動修正失敗 → 人間フォールバック"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(unreplied_comments=True)
        state["copilot_review_comments"] = [
            {"id": 1, "body": "Fix this", "replied": False, "author": "copilot[bot]"},
        ]

        result = phase8d_unified_fix_node(state)

        assert result["waiting_for_human"] is True
        assert result["human_input_request"] == "review_fix"
        assert result.get("auto_fix_attempts") == 1  # インクリメントされたまま（リセットしない）

    @patch("hokusai.nodes.phase8.review_check._get_git_client_for_pr")
    def test_unified_fix_auto_fix_retry_limit(self, mock_get_client):
        """統合フロー: 自動修正回数上限超過 → 人間フォールバック"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(unreplied_comments=True)
        state["copilot_review_comments"] = [
            {"id": 1, "body": "Fix this", "replied": False, "author": "copilot[bot]"},
        ]
        state["auto_fix_attempts"] = 2  # 上限到達

        result = phase8d_unified_fix_node(state)

        assert result["waiting_for_human"] is True
        assert result["human_input_request"] == "review_fix"
        assert result.get("auto_fix_attempts") == 2  # 上限のまま保持

    @patch("hokusai.nodes.phase8.review_fix._auto_fix_review_comments", return_value=True)
    @patch("hokusai.nodes.phase8.review_check._get_git_client_for_pr")
    def test_copilot_fix_auto_fix_success(self, mock_get_client, mock_auto_fix):
        """Copilotフロー: 自動修正成功"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(unreplied_comments=True)
        state["copilot_review_comments"] = [
            {"id": 1, "body": "Fix this", "replied": False, "author": "copilot[bot]"},
        ]

        result = phase8d_copilot_fix_node(state)

        assert result.get("waiting_for_human") is not True
        assert result.get("auto_fix_attempts") == 1

    @patch("hokusai.nodes.phase8.review_fix._auto_fix_review_comments", return_value=True)
    @patch("hokusai.nodes.phase8.review_check._get_git_client_for_pr")
    def test_human_fix_auto_fix_success(self, mock_get_client, mock_auto_fix):
        """人間フロー: 自動修正成功"""
        mock_client = Mock()
        mock_client.get_pr_commit_count.return_value = 5
        mock_get_client.return_value = mock_client

        state = _make_state_with_pr(unreplied_comments=True)
        state["human_review_comments"] = [
            {"id": 1, "body": "Fix this", "replied": False, "author": "reviewer"},
        ]

        result = phase8h_human_fix_node(state)

        assert result.get("waiting_for_human") is not True
        assert result.get("auto_fix_attempts") == 1
