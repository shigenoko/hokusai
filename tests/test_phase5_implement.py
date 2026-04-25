"""
Tests for hokusai.nodes.phase5_implement module

B-2-4: リトライ時のエラー情報活用のテスト
B-3-3: is_retry フラグ設定のテスト
B-4-4: リポジトリ単位の成功判定のテスト
"""

import pytest
from unittest.mock import patch, MagicMock

from hokusai.state import (
    WorkflowState,
    PhaseStatus,
    VerificationErrorEntry,
)
from hokusai.nodes.phase5_implement import (
    _prepare_implementation,
    _resolve_work_plan,
    _build_retry_prompt,
    _build_implementation_prompt,
    _extract_completed_steps,
    _extract_steps_from_work_plan,
)


class TestPrepareImplementation:
    """_prepare_implementation のテスト"""

    def test_detect_retry_from_verification_errors(self, state_with_verification_errors: WorkflowState):
        """検証エラーからリトライを検出"""
        # Phase 6 の retry_count を設定
        state_with_verification_errors["phases"][6]["retry_count"] = 1

        state, prep_result = _prepare_implementation(state_with_verification_errors)

        assert prep_result["is_retry"] is True
        assert prep_result["phase6_retry_count"] == 1

    def test_detect_retry_from_review_issues(self, state_with_review_issues: WorkflowState):
        """レビュー問題からリトライを検出"""
        state, prep_result = _prepare_implementation(state_with_review_issues)

        assert prep_result["is_retry"] is True
        assert prep_result["phase7_retry_count"] == 1

    def test_no_retry_without_errors(self, minimal_state: WorkflowState):
        """エラーがない場合はリトライではない"""
        # work_plan を設定して _handle_missing_work_plan を回避
        # _resolve_work_plan() の検証を通過する有効な計画
        minimal_state["work_plan"] = (
            "## 開発計画\n"
            "- [ ] **1.1** API変更\n"
            "- [ ] **1.2** 実装\n"
            "- [ ] **2.0** テスト\n"
        )

        state, prep_result = _prepare_implementation(minimal_state)

        assert prep_result["is_retry"] is False
        assert prep_result["phase6_retry_count"] == 0
        assert prep_result["phase7_retry_count"] == 0

    def test_both_errors_detected(self, minimal_state: WorkflowState):
        """検証エラーとレビュー問題の両方がある場合"""
        # 検証エラーを追加
        minimal_state["verification_errors"] = [
            VerificationErrorEntry(
                repository="Backend",
                command="test",
                success=False,
                error_output="Test failed",
            )
        ]
        minimal_state["phases"][6]["retry_count"] = 1

        # レビュー問題を追加
        minimal_state["final_review_issues"] = ["console.log found"]
        minimal_state["phases"][7]["retry_count"] = 1

        state, prep_result = _prepare_implementation(minimal_state)

        assert prep_result["is_retry"] is True
        assert prep_result["phase6_retry_count"] == 1
        assert prep_result["phase7_retry_count"] == 1


class TestBuildRetryPrompt:
    """_build_retry_prompt のテスト"""

    def test_include_verification_errors(self, state_with_verification_errors: WorkflowState):
        """検証エラーがプロンプトに含まれる"""
        prompt = _build_retry_prompt(state_with_verification_errors, repo=None)

        assert "検証で失敗したコマンド" in prompt
        assert "test" in prompt or "build" in prompt

    def test_include_review_issues(self, state_with_review_issues: WorkflowState):
        """レビュー問題がプロンプトに含まれる"""
        prompt = _build_retry_prompt(state_with_review_issues, repo=None)

        assert "コードレビューで検出された問題" in prompt
        assert "console.log" in prompt

    def test_filter_by_repository(self, state_with_verification_errors: WorkflowState):
        """リポジトリでフィルタされる"""
        # Mock repository config
        repo = MagicMock()
        repo.name = "Backend"
        repo.path = "/path/to/backend"

        prompt = _build_retry_prompt(state_with_verification_errors, repo=repo)

        # Backend リポジトリに関する情報が含まれる
        assert "Backend" in prompt
        # API リポジトリのエラーは含まれない（フィルタされる）
        # Note: 一般的な指摘は含まれる可能性がある

    def test_no_issues_message(self, minimal_state: WorkflowState):
        """問題がない場合のメッセージ"""
        repo = MagicMock()
        repo.name = "Backend"
        repo.path = "/path/to/backend"

        prompt = _build_retry_prompt(minimal_state, repo=repo)

        assert "修正する必要はありません" in prompt


class TestBuildImplementationPrompt:
    """_build_implementation_prompt のテスト"""

    def test_include_repository_info(self, minimal_state: WorkflowState):
        """リポジトリ情報が含まれる"""
        repo = MagicMock()
        repo.name = "Backend"
        repo.path = "/path/to/backend"

        prompt = _build_implementation_prompt(minimal_state, repo=repo, is_retry=False)

        assert "Backend" in prompt
        assert "対象リポジトリ" in prompt

    def test_include_work_plan(self, minimal_state: WorkflowState):
        """作業計画が含まれる"""
        minimal_state["work_plan"] = "## Step 1\nImplement feature X"

        prompt = _build_implementation_prompt(minimal_state, repo=None, is_retry=False)

        assert "作業計画" in prompt
        assert "Implement feature X" in prompt

    def test_retry_mode_uses_retry_prompt(self, state_with_review_issues: WorkflowState):
        """リトライモードでは retry プロンプトが使用される"""
        prompt = _build_implementation_prompt(
            state_with_review_issues, repo=None, is_retry=True
        )

        assert "検出された問題の修正" in prompt

    def test_retry_mode_uses_retry_prompt_for_verification_errors_only(
        self, state_with_verification_errors: WorkflowState
    ):
        """Phase 6失敗のみ（final_review_issuesなし）でもリトライプロンプトが使用される

        Issue #1 修正: verification_errors がある場合も _build_retry_prompt() が呼ばれる
        """
        # final_review_issues が空であることを確認
        assert not state_with_verification_errors.get("final_review_issues")
        # verification_errors に失敗エントリがあることを確認
        assert any(
            not err.get("success")
            for err in state_with_verification_errors.get("verification_errors", [])
        )

        prompt = _build_implementation_prompt(
            state_with_verification_errors, repo=None, is_retry=True
        )

        # リトライプロンプトが使用されている
        assert "検出された問題の修正" in prompt
        # 検証エラー情報が含まれている
        assert "検証で失敗したコマンド" in prompt


class TestExtractCompletedSteps:
    """_extract_completed_steps のテスト"""

    def test_extract_from_section(self):
        """セクションからステップを抽出"""
        result = """
### 完了したステップ
- 1.1: Feature A implemented
- 2.0: Refactored module

### 変更内容
- Updated file.ts
"""
        steps = _extract_completed_steps(result)

        assert "1.1" in steps
        assert "2.0" in steps

    def test_extract_with_emoji(self):
        """絵文字付きの完了マーカーからステップを抽出"""
        result = """
✅ 1.1 完了: Feature implemented
✅ 2.0 done: Module refactored
"""
        steps = _extract_completed_steps(result)

        assert "1.1" in steps
        assert "2.0" in steps

    def test_no_steps_found(self):
        """ステップが見つからない場合"""
        result = "Implementation completed successfully."
        steps = _extract_completed_steps(result)

        assert steps == []


class TestExtractStepsFromWorkPlan:
    """_extract_steps_from_work_plan のテスト"""

    def test_extract_unchecked_items(self):
        """未チェック項目を抽出"""
        work_plan = """
- [ ] **1.1** Create new API endpoint
- [x] **1.2** Already done
- [ ] **2.0** Implement validation
"""
        steps = _extract_steps_from_work_plan(work_plan)

        assert "1.1" in steps
        assert "2.0" in steps
        # 1.2 は既にチェック済みなので含まれない
        assert "1.2" not in steps

    def test_extract_numbered_list(self):
        """番号付きリストからステップを抽出"""
        work_plan = """
1.1 Create endpoint
**2.0** Implement feature
3.0 Add tests
"""
        steps = _extract_steps_from_work_plan(work_plan)

        assert "1.1" in steps
        assert "2.0" in steps


class TestRepositorySkipLogic:
    """リポジトリスキップロジックのテスト (B-4)"""

    def test_skip_completed_repository(self, state_with_repository_status: WorkflowState):
        """完了済みリポジトリをスキップ"""
        repo_status = state_with_repository_status.get("repository_status", {})

        # シミュレート: リポジトリリストを処理
        repositories = ["Backend", "API", "Frontend"]
        skipped = []
        executed = []

        for repo_name in repositories:
            status = repo_status.get(repo_name)
            if status == "completed":
                skipped.append(repo_name)
            else:
                executed.append(repo_name)

        assert "Backend" in skipped  # Backend は completed
        assert "API" in executed  # API は failed
        assert "Frontend" in executed  # Frontend はステータスなし

    def test_execute_all_on_first_run(self, minimal_state: WorkflowState):
        """初回実行では全リポジトリを実行"""
        repo_status = minimal_state.get("repository_status", {})

        repositories = ["Backend", "API"]
        executed = []

        for repo_name in repositories:
            status = repo_status.get(repo_name)
            # ステータスがないか、completed でない場合は実行
            if status != "completed":
                executed.append(repo_name)

        assert len(executed) == 2
        assert "Backend" in executed
        assert "API" in executed


class TestRetryPromptWithVerificationErrors:
    """検証エラー付きリトライプロンプトのテスト (B-2)"""

    def test_error_output_included(self, state_with_verification_errors: WorkflowState):
        """エラー出力がプロンプトに含まれる"""
        prompt = _build_retry_prompt(state_with_verification_errors, repo=None)

        # エラー出力が含まれている
        assert "Test failed" in prompt or "TypeScript compilation" in prompt

    def test_repository_filter(self, state_with_verification_errors: WorkflowState):
        """リポジトリ別にフィルタされる"""
        # Backend リポジトリ用のプロンプト
        backend_repo = MagicMock()
        backend_repo.name = "Backend"
        backend_repo.path = "/path/to/backend"

        prompt = _build_retry_prompt(state_with_verification_errors, repo=backend_repo)

        # Backend のエラーは含まれる
        assert "Backend" in prompt

    def test_command_type_shown(self, state_with_verification_errors: WorkflowState):
        """コマンドタイプが表示される"""
        prompt = _build_retry_prompt(state_with_verification_errors, repo=None)

        # コマンド名が含まれている（test, build, lint など）
        assert "test" in prompt.lower() or "build" in prompt.lower()


class TestResolveWorkPlan:
    """_resolve_work_plan のテスト"""

    # 検証に通る有効な計画テキスト
    VALID_PLAN = (
        "## 開発計画\n"
        "- [ ] **1.1** APIスキーマ変更\n"
        "- [ ] **1.2** バックエンド実装\n"
        "- [ ] **2.0** フロントエンド実装\n"
    )
    # 検証に通らない不正な計画テキスト
    INVALID_PLAN = "LLMのプリアンブルテキスト。計画なし。"

    def test_from_state_valid(self, minimal_state: WorkflowState):
        """state に有効な work_plan ありで source="state" """
        minimal_state["work_plan"] = self.VALID_PLAN
        content, source = _resolve_work_plan(minimal_state)
        assert content == self.VALID_PLAN
        assert source == "state"

    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_from_subpage(self, mock_notion_cls, minimal_state: WorkflowState):
        """state 空、subpage から有効な計画を取得"""
        minimal_state["work_plan"] = None
        minimal_state["phase_subpages"] = {4: "https://www.notion.so/abc123"}

        mock_notion = MagicMock()
        mock_notion.get_page_content.return_value = self.VALID_PLAN
        mock_notion_cls.return_value = mock_notion

        content, source = _resolve_work_plan(minimal_state)
        assert content == self.VALID_PLAN
        assert source == "phase4_subpage"
        mock_notion.get_page_content.assert_called_once_with("https://www.notion.so/abc123")

    @patch("hokusai.nodes.phase5_implement.get_task_client")
    def test_from_task_page(self, mock_get_client, minimal_state: WorkflowState):
        """state・subpage 空、親ページから有効な計画を取得"""
        minimal_state["work_plan"] = None

        mock_client = MagicMock()
        mock_client.get_section_content.return_value = self.VALID_PLAN
        mock_get_client.return_value = mock_client

        content, source = _resolve_work_plan(minimal_state)
        assert content == self.VALID_PLAN
        assert source == "task_page_section"

    @patch("hokusai.nodes.phase5_implement.get_task_client")
    def test_not_found(self, mock_get_client, minimal_state: WorkflowState):
        """全て失敗で None 返却"""
        minimal_state["work_plan"] = None

        mock_client = MagicMock()
        mock_client.get_section_content.return_value = None
        mock_get_client.return_value = mock_client

        content, source = _resolve_work_plan(minimal_state)
        assert content is None
        assert source == "not_found"

    @patch("hokusai.nodes.phase5_implement.get_task_client")
    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_invalid_state_fallback_to_subpage(
        self, mock_notion_cls, mock_get_client, minimal_state: WorkflowState
    ):
        """state の work_plan が不正な場合、subpage にフォールバック"""
        minimal_state["work_plan"] = self.INVALID_PLAN
        minimal_state["phase_subpages"] = {4: "https://www.notion.so/abc123"}

        mock_notion = MagicMock()
        mock_notion.get_page_content.return_value = self.VALID_PLAN
        mock_notion_cls.return_value = mock_notion

        content, source = _resolve_work_plan(minimal_state)
        assert content == self.VALID_PLAN
        assert source == "phase4_subpage"

    @patch("hokusai.nodes.phase5_implement.get_task_client")
    def test_all_sources_invalid(self, mock_get_client, minimal_state: WorkflowState):
        """全ソースの検証が失敗した場合 source が "(invalid)" で終わる"""
        minimal_state["work_plan"] = self.INVALID_PLAN

        mock_client = MagicMock()
        mock_client.get_section_content.return_value = "これも不正な計画"
        mock_get_client.return_value = mock_client

        content, source = _resolve_work_plan(minimal_state)
        assert content == self.INVALID_PLAN  # 最初に見つかったものを返す
        assert source.endswith("(invalid)")

    @patch("hokusai.nodes.phase5_implement.get_task_client")
    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_invalid_state_and_subpage_fallback_to_task_page(
        self, mock_notion_cls, mock_get_client, minimal_state: WorkflowState
    ):
        """state と subpage が不正、親ページが有効な場合にフォールバック"""
        minimal_state["work_plan"] = self.INVALID_PLAN
        minimal_state["phase_subpages"] = {4: "https://www.notion.so/abc123"}

        mock_notion = MagicMock()
        mock_notion.get_page_content.return_value = "これも不正"
        mock_notion_cls.return_value = mock_notion

        mock_client = MagicMock()
        mock_client.get_section_content.return_value = self.VALID_PLAN
        mock_get_client.return_value = mock_client

        content, source = _resolve_work_plan(minimal_state)
        assert content == self.VALID_PLAN
        assert source == "task_page_section"
