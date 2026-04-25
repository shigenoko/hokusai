"""
Phase 3 設計チェックノードのテスト

テスト対象:
- phase3_design_node: execute_prompt() による直接実行
- _build_design_check_prompt: プロンプト構築
- _validate_design_output: 出力品質検証
- _verify_design_subpage_content: 保存後検証
- NOTION_WRITE_TOOLS: Notion書き込み遮断
"""

from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 直接プロンプト実行テスト
# ---------------------------------------------------------------------------

class TestPhase3DirectPrompt:
    """Phase 3 が execute_prompt() で直接設計チェックを実行することを検証"""

    def test_notion_write_tools_defined(self):
        """NOTION_WRITE_TOOLS 定数が定義されていること"""
        from hokusai.nodes.phase3_design import NOTION_WRITE_TOOLS
        assert "mcp__notion__notion-update-page" in NOTION_WRITE_TOOLS
        assert "mcp__notion__notion-create-pages" in NOTION_WRITE_TOOLS
        assert "mcp__notion__notion-create-comment" in NOTION_WRITE_TOOLS

    def test_build_prompt_contains_required_elements(self):
        """_build_design_check_prompt が必須要素を含むこと"""
        from hokusai.nodes.phase3_design import _build_design_check_prompt

        prompt = _build_design_check_prompt(
            task_url="https://www.notion.so/test-page",
            research_result="## 調査結果\nテスト内容",
        )
        assert "https://www.notion.so/test-page" in prompt
        assert "## 設計チェック" in prompt
        assert "前置き" in prompt
        assert "### 概要" in prompt
        assert "### 設計方針" in prompt
        assert "### リスク" in prompt
        assert "### 確認事項" in prompt

    def test_build_prompt_includes_research_result(self):
        """_build_design_check_prompt に research_result が含まれること"""
        from hokusai.nodes.phase3_design import _build_design_check_prompt

        prompt = _build_design_check_prompt(
            task_url="https://www.notion.so/test",
            research_result="### タスク概要\nユーザー管理で質問一覧表示",
        )
        assert "ユーザー管理で質問一覧表示" in prompt

    def test_build_prompt_includes_cross_review_context(self):
        """_build_design_check_prompt に cross_review_context が含まれること"""
        from hokusai.nodes.phase3_design import _build_design_check_prompt

        prompt = _build_design_check_prompt(
            task_url="https://www.notion.so/test",
            research_result="## 調査結果\nテスト",
            cross_review_context="[major] パフォーマンス問題: N+1クエリ",
        )
        assert "[major] パフォーマンス問題" in prompt

    def test_build_prompt_without_cross_review(self):
        """cross_review_context が空の場合でもプロンプトが正常に構築される"""
        from hokusai.nodes.phase3_design import _build_design_check_prompt

        prompt = _build_design_check_prompt(
            task_url="https://www.notion.so/test",
            research_result="## 調査結果\nテスト",
            cross_review_context="",
        )
        assert "クロスLLMレビュー指摘事項" not in prompt

    def _build_state(self):
        phase_template = {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "error_message": None,
            "retry_count": 0,
        }
        return {
            "task_url": "https://www.notion.so/task-page-aabbccdd",
            "task_name": "テストタスク",
            "repo_path": "/tmp/test",
            "workflow_id": "test-wf-001",
            "phases": {i: {**phase_template} for i in range(1, 11)},
            "audit_log": [],
            "schema_change_required": False,
            "research_result": "## 調査結果\nテスト調査内容",
        }

    @patch("hokusai.nodes.phase3_design._validate_design_output")
    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase3_design.get_config")
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_uses_execute_prompt_not_execute_skill(
        self, mock_claude_cls, mock_config, mock_save, mock_cross_review,
        mock_verify_content, mock_validate_output,
    ):
        """execute_prompt() が呼ばれ、execute_skill() は呼ばれないこと"""
        mock_config.return_value.skill_timeout = 300
        mock_client = MagicMock()
        mock_client.execute_prompt.return_value = "## 設計チェック\n\nテスト内容"
        mock_claude_cls.return_value = mock_client

        state = self._build_state()
        mock_save.side_effect = lambda s, *a, **kw: s

        from hokusai.nodes.phase3_design import phase3_design_node
        phase3_design_node(state)

        mock_client.execute_prompt.assert_called_once()
        mock_client.execute_skill.assert_not_called()
        # プロンプトに必須要素が含まれること
        prompt_arg = mock_client.execute_prompt.call_args.kwargs.get("prompt") or mock_client.execute_prompt.call_args[0][0]
        assert "## 設計チェック" in prompt_arg
        assert "task-page-aabbccdd" in prompt_arg
        # disallowed_tools が渡されること
        call_kwargs = mock_client.execute_prompt.call_args
        assert call_kwargs.kwargs.get("disallowed_tools") is not None
        assert "mcp__notion__notion-update-page" in call_kwargs.kwargs["disallowed_tools"]

    @patch("hokusai.nodes.phase3_design._validate_design_output")
    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase3_design.get_config")
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_saves_raw_output_to_notion(
        self, mock_claude_cls, mock_config, mock_save, mock_cross_review,
        mock_verify_content, mock_validate_output,
    ):
        """save_to_subpage_or_create にテンプレート化されたページ本文が渡されること"""
        mock_config.return_value.skill_timeout = 300
        raw = "## 設計チェック\n\n### 概要\n設計内容\n\n### 設計方針\n方針内容"
        mock_client = MagicMock()
        mock_client.execute_prompt.return_value = raw
        mock_claude_cls.return_value = mock_client

        state = self._build_state()
        mock_save.side_effect = lambda s, *a, **kw: s

        from hokusai.nodes.phase3_design import phase3_design_node
        phase3_design_node(state)

        call_kwargs = mock_save.call_args
        saved_content = call_kwargs.kwargs.get("content") or call_kwargs[0][3]
        assert "# Phase 3: 設計" in saved_content
        assert "## 進捗チェックリスト" in saved_content
        assert "## 最新版ドキュメント" in saved_content
        assert raw in saved_content


# ---------------------------------------------------------------------------
# 出力品質検証テスト
# ---------------------------------------------------------------------------

class TestValidateDesignOutput:
    """raw_output の品質検証テスト"""

    FULL_REPORT = (
        "## 設計チェック\n\n"
        "### 概要\n"
        "ユーザー管理で質問一覧表示機能のチェック\n\n"
        "### 設計方針\n"
        "DataTableコンポーネントを使用して一覧表示する\n\n"
        "### リスク\n"
        "パフォーマンス影響は軽微\n\n"
        "### 確認事項\n"
        "テンプレートIDの表示要否\n"
    )

    def test_full_report_passes(self):
        """フルレポート出力は検証を通過する"""
        from hokusai.nodes.phase3_design import _validate_design_output
        _validate_design_output(self.FULL_REPORT)

    def test_full_report_with_3_sections_passes(self):
        """必須セクション 3/4 でも検証を通過する"""
        from hokusai.nodes.phase3_design import _validate_design_output
        report = (
            "## 設計チェック\n\n"
            "### 概要\nテスト\n\n"
            "### 設計方針\n方針\n\n"
            "### リスク\nなし\n"
        )
        _validate_design_output(report)

    def test_preface_fails(self):
        """前置き文がある出力は fail する"""
        from hokusai.nodes.phase3_design import _validate_design_output
        output = "了解しました。設計チェックを行います。\n\n" + self.FULL_REPORT
        with pytest.raises(RuntimeError, match="許可開始見出しで始まっていません"):
            _validate_design_output(output)

    def test_empty_output_fails(self):
        """空出力は fail する"""
        from hokusai.nodes.phase3_design import _validate_design_output
        with pytest.raises(RuntimeError, match="raw_output が空"):
            _validate_design_output("")

    def test_only_2_sections_fails(self):
        """必須セクション 2/4 以下は fail する"""
        from hokusai.nodes.phase3_design import _validate_design_output
        output = (
            "## 設計チェック\n\n"
            "### 概要\nテスト\n\n"
            "### 設計方針\n方針\n"
        )
        with pytest.raises(RuntimeError, match="必須セクションが不足"):
            _validate_design_output(output)

    def test_meta_explanation_fails(self):
        """禁止メタ説明を含む出力は fail する"""
        from hokusai.nodes.phase3_design import _validate_design_output
        output = self.FULL_REPORT + "\n手動でNotionに貼り付けてください。\n"
        with pytest.raises(RuntimeError, match="禁止メタ説明"):
            _validate_design_output(output)

    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase3_design.get_config")
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_phase3_stops_before_save_on_validation_failure(
        self, mock_claude_cls, mock_config, mock_save, mock_cross_review,
        mock_verify_content,
    ):
        """_validate_design_output 失敗時に保存前に停止する"""
        mock_config.return_value.skill_timeout = 300
        mock_client = MagicMock()
        # 前置き文付きの出力（検証で拒否される）
        mock_client.execute_prompt.return_value = "設計チェックが完了しました。\n結果は問題ありません。"
        mock_claude_cls.return_value = mock_client

        phase_template = {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "error_message": None,
            "retry_count": 0,
        }
        state = {
            "task_url": "https://www.notion.so/task-page-aabbccdd",
            "task_name": "テストタスク",
            "repo_path": "/tmp/test",
            "workflow_id": "test-wf-001",
            "phases": {i: {**phase_template} for i in range(1, 11)},
            "audit_log": [],
            "schema_change_required": False,
            "research_result": "## 調査結果\nテスト",
        }

        from hokusai.nodes.phase3_design import phase3_design_node
        with pytest.raises(RuntimeError, match="出力検証失敗"):
            phase3_design_node(state)

        mock_save.assert_not_called()
        assert state["phases"][3]["status"] == "failed"


# ---------------------------------------------------------------------------
# 保存後検証テスト
# ---------------------------------------------------------------------------

class TestVerifyDesignSubpageContent:
    """_verify_design_subpage_content の検証テスト"""

    def _build_state(self):
        phase_template = {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "error_message": None,
            "retry_count": 0,
        }
        return {
            "task_url": "https://www.notion.so/task-page-aabbccdd",
            "task_name": "テストタスク",
            "repo_path": "/tmp/test",
            "workflow_id": "test-wf-001",
            "phases": {i: {**phase_template} for i in range(1, 11)},
            "audit_log": [],
            "schema_change_required": False,
            "research_result": "",
        }

    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_passes_when_full_content_saved(self, MockClient):
        """子ページに全文が保存されていれば検証通過"""
        from hokusai.nodes.phase3_design import _verify_design_subpage_content

        raw_output = (
            "## 設計チェック\n\n"
            "### 概要\n設計チェック内容\n\n"
            "### 設計方針\nDataTableコンポーネントを使用\n\n"
            "### リスク\nパフォーマンス影響は軽微\n"
        )
        mock_instance = MagicMock()
        mock_instance.get_page_content.return_value = raw_output
        MockClient.return_value = mock_instance

        state = self._build_state()
        state["phase_subpages"] = {3: "https://notion.so/subpage-3"}

        _verify_design_subpage_content(state, raw_output)

    def test_fails_when_no_subpage_url(self):
        """phase_subpages[3] が未登録 → RuntimeError"""
        from hokusai.nodes.phase3_design import _verify_design_subpage_content

        state = self._build_state()
        with pytest.raises(RuntimeError, match="子ページURLが未登録"):
            _verify_design_subpage_content(state, "## 設計チェック\nテスト")

    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_fails_when_content_empty(self, MockClient):
        """子ページ本文が空 → RuntimeError"""
        from hokusai.nodes.phase3_design import _verify_design_subpage_content

        mock_instance = MagicMock()
        mock_instance.get_page_content.return_value = ""
        MockClient.return_value = mock_instance

        state = self._build_state()
        state["phase_subpages"] = {3: "https://notion.so/subpage-3"}

        with pytest.raises(RuntimeError, match="子ページ本文が空"):
            _verify_design_subpage_content(state, "## 設計チェック\nテスト内容テスト内容テスト")

    def test_skipped_when_notion_disabled(self, monkeypatch):
        """HOKUSAI_SKIP_NOTION=1 では検証をスキップ"""
        from hokusai.nodes.phase3_design import _verify_design_subpage_content

        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")
        state = self._build_state()
        _verify_design_subpage_content(state, "any content")

    @patch("time.sleep")
    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_retries_on_get_page_content_timeout(self, MockClient, mock_sleep):
        """get_page_content タイムアウト時にリトライして成功する"""
        from hokusai.nodes.phase3_design import _verify_design_subpage_content

        raw_output = (
            "## 設計チェック\n\n"
            "### 概要\n設計チェック内容\n\n"
            "### 設計方針\nDataTableコンポーネントを使用\n\n"
            "### リスク\nパフォーマンス影響は軽微\n"
        )
        mock_instance = MagicMock()
        mock_instance.get_page_content.side_effect = [
            Exception("プロンプトの実行がタイムアウトしました"),
            raw_output,
        ]
        MockClient.return_value = mock_instance

        state = self._build_state()
        state["phase_subpages"] = {3: "https://notion.so/subpage-3"}

        _verify_design_subpage_content(state, raw_output)
        assert mock_instance.get_page_content.call_count == 2
        mock_sleep.assert_called_once_with(5)

    @patch("time.sleep")
    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_fails_after_max_retries(self, MockClient, mock_sleep):
        """最大リトライ回数を超えると RuntimeError"""
        from hokusai.nodes.phase3_design import _verify_design_subpage_content

        mock_instance = MagicMock()
        mock_instance.get_page_content.side_effect = Exception("タイムアウト")
        MockClient.return_value = mock_instance

        state = self._build_state()
        state["phase_subpages"] = {3: "https://notion.so/subpage-3"}

        with pytest.raises(RuntimeError, match="3回試行"):
            _verify_design_subpage_content(state, "## 設計チェック\nテスト")
        assert mock_instance.get_page_content.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("hokusai.nodes.phase3_design._validate_design_output")
    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase3_design.get_config")
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_phase3_calls_verify_after_save(
        self, mock_claude_cls, mock_config, mock_save, mock_cross_review,
        mock_verify_content, mock_validate_output,
    ):
        """phase3_design_node が save 後に _verify_design_subpage_content を呼ぶこと"""
        mock_config.return_value.skill_timeout = 300
        mock_client = MagicMock()
        mock_client.execute_prompt.return_value = "## 設計チェック\n\nテスト内容"
        mock_claude_cls.return_value = mock_client
        mock_save.side_effect = lambda s, *a, **kw: s

        state = self._build_state()
        state["research_result"] = "## 調査結果\nテスト"

        from hokusai.nodes.phase3_design import phase3_design_node
        phase3_design_node(state)

        mock_verify_content.assert_called_once()
        call_args = mock_verify_content.call_args
        assert call_args[0][0] is state
        assert "## 設計チェック" in call_args[0][1]

    @patch("hokusai.nodes.phase3_design._validate_design_output")
    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase3_design.get_config")
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_phase3_continues_when_verify_content_fails(
        self, mock_claude_cls, mock_config, mock_save, mock_cross_review,
        mock_verify_content, mock_validate_output,
    ):
        """_verify_design_subpage_content 失敗時でも Phase 3 が完了すること（警告のみ）"""
        mock_config.return_value.skill_timeout = 300
        mock_client = MagicMock()
        mock_client.execute_prompt.return_value = "## 設計チェック\n\nテスト内容"
        mock_claude_cls.return_value = mock_client
        mock_save.side_effect = lambda s, *a, **kw: s
        mock_verify_content.side_effect = RuntimeError("Phase 3 本文検証失敗: 子ページ本文が空です。")

        state = self._build_state()
        state["research_result"] = "## 調査結果\nテスト"

        from hokusai.nodes.phase3_design import phase3_design_node
        result = phase3_design_node(state)

        # 検証失敗でもフェーズは完了し、design_result が保存されている
        assert result["phases"][3]["status"] == "completed"
        assert result["design_result"] == "## 設計チェック\n\nテスト内容"
