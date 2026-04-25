"""
CodexClient / クロスLLMレビューのテスト
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from hokusai.integrations.codex import CodexClient, reset_codex_client
from hokusai.config.models import CrossReviewConfig, WorkflowConfig
from hokusai.constants import CROSS_REVIEW_PROMPTS
from hokusai.state import WorkflowState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_codex():
    """各テスト後にCodexClientシングルトンをリセット"""
    yield
    reset_codex_client()


@pytest.fixture
def sample_review_result() -> dict:
    """レビュー結果サンプル"""
    return {
        "findings": [
            {
                "category": "completeness",
                "severity": "major",
                "title": "テスト計画が未記載",
                "description": "テスト方針が明記されていません。",
                "suggestion": "テスト計画セクションを追加してください。",
            }
        ],
        "overall_assessment": "request_changes",
        "summary": "概ね良好だが、テスト計画が不足しています。",
        "confidence_score": 0.85,
    }


@pytest.fixture
def approve_review_result() -> dict:
    """承認レビュー結果"""
    return {
        "findings": [],
        "overall_assessment": "approve",
        "summary": "問題ありません。",
        "confidence_score": 0.95,
    }


@pytest.fixture
def critical_review_result() -> dict:
    """critical findings 含むレビュー結果"""
    return {
        "findings": [
            {
                "category": "feasibility",
                "severity": "critical",
                "title": "存在しないAPIを参照",
                "description": "記載されたAPIエンドポイントは実在しません。",
            }
        ],
        "overall_assessment": "request_changes",
        "summary": "重大な問題があります。",
        "confidence_score": 0.9,
    }


# ---------------------------------------------------------------------------
# TestCodexClient
# ---------------------------------------------------------------------------

class TestCodexClient:
    """CodexClient 単体テスト"""

    @patch.dict("os.environ", {"CODEX_PATH": "/usr/local/bin/codex"})
    def test_find_codex_command_env(self):
        """環境変数 CODEX_PATH から検出"""
        client = CodexClient()
        assert client.codex_path == "/usr/local/bin/codex"

    @patch.dict("os.environ", {}, clear=True)
    @patch("shutil.which", return_value="/opt/homebrew/bin/codex")
    def test_find_codex_command_which(self, mock_which):
        """shutil.which から検出"""
        # 環境変数 CODEX_PATH をクリア
        import os
        os.environ.pop("CODEX_PATH", None)
        client = CodexClient()
        assert client.codex_path == "/opt/homebrew/bin/codex"

    @patch.dict("os.environ", {}, clear=True)
    @patch("pathlib.Path.exists", return_value=False)
    @patch("shutil.which", return_value=None)
    def test_find_codex_command_not_found(self, mock_which, mock_exists):
        """コマンドが見つからない場合 FileNotFoundError"""
        import os
        os.environ.pop("CODEX_PATH", None)
        with pytest.raises(FileNotFoundError, match="codexコマンドが見つかりません"):
            CodexClient()

    @patch.dict("os.environ", {"CODEX_PATH": "/usr/local/bin/codex"})
    @patch("subprocess.run")
    def test_review_document_with_schema(self, mock_run, sample_review_result):
        """構造化出力でレビュー実行"""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(sample_review_result),
            stderr="",
        )

        client = CodexClient()
        result = client.review_document(
            document="# テスト計画\n...",
            review_prompt="レビューしてください",
            schema_path="/path/to/schema.json",
        )

        assert result["overall_assessment"] == "request_changes"
        assert len(result["findings"]) == 1
        # --output-schema が渡されていることを確認
        call_args = mock_run.call_args[0][0]
        assert "--output-schema" in call_args
        assert "/path/to/schema.json" in call_args

    @patch.dict("os.environ", {"CODEX_PATH": "/usr/local/bin/codex"})
    @patch("subprocess.run")
    def test_review_document_timeout(self, mock_run):
        """タイムアウト"""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=300)

        client = CodexClient(timeout=300)
        with pytest.raises(TimeoutError, match="タイムアウト"):
            client.review_document(
                document="...",
                review_prompt="レビュー",
            )

    @patch.dict("os.environ", {"CODEX_PATH": "/usr/local/bin/codex"})
    @patch("subprocess.run")
    def test_review_document_failure(self, mock_run):
        """実行失敗"""
        mock_run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="Error: model not found",
        )

        client = CodexClient()
        with pytest.raises(RuntimeError, match="Codex実行エラー"):
            client.review_document(
                document="...",
                review_prompt="レビュー",
            )

    @patch.dict("os.environ", {"CODEX_PATH": "/usr/local/bin/codex"})
    @patch("subprocess.run")
    def test_review_document_json_in_markdown(self, mock_run, sample_review_result):
        """Markdown内にJSONが埋まった出力のパース"""
        output = f"Here is my review:\n```json\n{json.dumps(sample_review_result)}\n```"
        mock_run.return_value = Mock(
            returncode=0,
            stdout=output,
            stderr="",
        )

        client = CodexClient()
        result = client.review_document(
            document="...",
            review_prompt="レビュー",
        )
        assert result["overall_assessment"] == "request_changes"

    @patch.dict("os.environ", {"CODEX_PATH": "/usr/local/bin/codex"})
    @patch("subprocess.run")
    def test_review_document_unparseable_output(self, mock_run):
        """パースできない出力はフォールバック"""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="This is not JSON at all.",
            stderr="",
        )

        client = CodexClient()
        result = client.review_document(
            document="...",
            review_prompt="レビュー",
        )
        assert result["overall_assessment"] == "needs_discussion"
        assert result["parse_error"] is True


# ---------------------------------------------------------------------------
# TestExecuteCrossReview
# ---------------------------------------------------------------------------

class TestExecuteCrossReview:
    """execute_cross_review のテスト"""

    def _make_config(self, **overrides) -> WorkflowConfig:
        """テスト用WorkflowConfig生成"""
        cr_kwargs = {
            "enabled": True,
            "model": "codex-mini-latest",
            "phases": [2, 4],
            "timeout": 300,
            "on_failure": "warn",
            "max_correction_rounds": 2,
        }
        cr_kwargs.update(overrides)
        return WorkflowConfig(
            cross_review=CrossReviewConfig(**cr_kwargs),
        )

    def test_disabled(self, minimal_state):
        """cross_review.enabled = False → スキップ"""
        from hokusai.utils.cross_review import execute_cross_review

        config = self._make_config(enabled=False)
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=4)

        assert result["cross_review_results"] == {}

    def test_phase_not_in_config(self, minimal_state):
        """対象外のPhase → スキップ"""
        from hokusai.utils.cross_review import execute_cross_review

        config = self._make_config(phases=[2])
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=4)

        assert 4 not in result["cross_review_results"]

    @patch("hokusai.utils.cross_review.CodexClient")
    def test_codex_not_installed_warn(self, mock_cls, minimal_state):
        """未インストール + warn → 続行"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_cls.side_effect = FileNotFoundError("codex not found")
        config = self._make_config(on_failure="warn")
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=2)

        assert result["waiting_for_human"] is False
        # 監査ログにスキップが記録
        assert any(e["action"] == "cross_review_skipped" for e in result["audit_log"])

    @patch("hokusai.utils.cross_review.CodexClient")
    def test_codex_not_installed_block(self, mock_cls, minimal_state):
        """未インストール + block → waiting_for_human"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_cls.side_effect = FileNotFoundError("codex not found")
        config = self._make_config(on_failure="block")
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=2)

        assert result["waiting_for_human"] is True
        assert "Codex CLI" in result["human_input_request"]

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_approve(self, mock_cls, mock_notion, minimal_state, approve_review_result):
        """approve → 正常に続行"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.return_value = approve_review_result
        mock_cls.return_value = mock_client

        config = self._make_config()
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=4)

        assert result["cross_review_results"][4]["overall_assessment"] == "approve"
        assert result["waiting_for_human"] is False

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_request_changes_warn(self, mock_cls, mock_notion, minimal_state, sample_review_result):
        """request_changes + warn → 続行"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.return_value = sample_review_result
        mock_cls.return_value = mock_client

        config = self._make_config(on_failure="warn")
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=4)

        assert result["cross_review_results"][4]["overall_assessment"] == "request_changes"
        assert result["waiting_for_human"] is False

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_request_changes_block_critical(self, mock_cls, mock_notion, minimal_state, critical_review_result):
        """critical + block → waiting_for_human"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.return_value = critical_review_result
        mock_cls.return_value = mock_client

        config = self._make_config(on_failure="block")
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=2)

        assert result["waiting_for_human"] is True
        assert "critical" in result["human_input_request"]

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_state_update(self, mock_cls, mock_notion, minimal_state, sample_review_result):
        """cross_review_results に格納されること"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.return_value = sample_review_result
        mock_cls.return_value = mock_client

        config = self._make_config()
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=2)

        assert 2 in result["cross_review_results"]
        assert result["cross_review_results"][2] == sample_review_result

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_audit_log(self, mock_cls, mock_notion, minimal_state, approve_review_result):
        """監査ログに記録されること"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.return_value = approve_review_result
        mock_cls.return_value = mock_client

        config = self._make_config()
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=4)

        log_entries = [e for e in result["audit_log"] if e["action"] == "cross_review_completed"]
        assert len(log_entries) == 1
        assert log_entries[0]["details"]["assessment"] == "approve"

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_execution_error_warn(self, mock_cls, mock_notion, minimal_state):
        """実行エラー + warn → 続行"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.side_effect = RuntimeError("API error")
        mock_cls.return_value = mock_client

        config = self._make_config(on_failure="warn")
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=2)

        assert result["waiting_for_human"] is False
        assert any(e["action"] == "cross_review_failed" for e in result["audit_log"])

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_execution_error_block(self, mock_cls, mock_notion, minimal_state):
        """実行エラー + block → waiting_for_human"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.side_effect = TimeoutError("timeout")
        mock_cls.return_value = mock_client

        config = self._make_config(on_failure="block")
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=4)

        assert result["waiting_for_human"] is True

    @patch("hokusai.utils.cross_review.CodexClient")
    def test_execution_error_skip(self, mock_cls, minimal_state):
        """実行エラー + skip → スキップとして続行"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.side_effect = RuntimeError("API error")
        mock_cls.return_value = mock_client

        config = self._make_config(on_failure="skip")
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=4)

        assert result["waiting_for_human"] is False
        assert any(e["action"] == "cross_review_skipped" for e in result["audit_log"])

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_retry_by_max_correction_rounds(self, mock_cls, mock_notion, minimal_state, approve_review_result):
        """max_correction_rounds 回数まで再試行される"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.side_effect = [
            RuntimeError("temporary failure"),
            approve_review_result,
        ]
        mock_cls.return_value = mock_client

        config = self._make_config(max_correction_rounds=2)
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some doc", phase=4)

        assert mock_client.review_document.call_count == 2
        assert result["cross_review_results"][4]["overall_assessment"] == "approve"

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_phase3_supported(self, mock_cls, mock_notion, minimal_state, approve_review_result):
        """Phase 3 もクロスレビュー対象として処理できる"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.return_value = approve_review_result
        mock_cls.return_value = mock_client

        config = self._make_config(phases=[3])
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "some design doc", phase=3)

        assert result["cross_review_results"][3]["overall_assessment"] == "approve"

    def test_empty_document_skipped(self, minimal_state):
        """空ドキュメント → スキップ"""
        from hokusai.utils.cross_review import execute_cross_review

        config = self._make_config()
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "", phase=2)

        assert result["cross_review_results"] == {}


# ---------------------------------------------------------------------------
# TestGenerateCrossReviewCallout
# ---------------------------------------------------------------------------

class TestGenerateCrossReviewCallout:
    """Notion callout 生成テスト"""

    def test_basic_callout(self, sample_review_result):
        """基本的な callout 生成"""
        from hokusai.utils.notion_helpers import generate_cross_review_callout

        callout = generate_cross_review_callout(sample_review_result, phase=4)
        assert "Cross-LLM Review" in callout
        assert "request_changes" in callout
        assert "テスト計画が未記載" in callout

    def test_callout_with_no_findings(self, approve_review_result):
        """findings が空の場合"""
        from hokusai.utils.notion_helpers import generate_cross_review_callout

        callout = generate_cross_review_callout(approve_review_result, phase=2)
        assert "approve" in callout
        assert "Findings" not in callout


# ---------------------------------------------------------------------------
# TestCrossReviewConfig
# ---------------------------------------------------------------------------

class TestCrossReviewConfig:
    """CrossReviewConfig のテスト"""

    def test_default_values(self):
        """デフォルト値の確認"""
        config = CrossReviewConfig()
        assert config.enabled is False
        assert config.model == "codex-mini-latest"
        assert config.phases == [2, 4]
        assert config.timeout == 300
        assert config.on_failure == "warn"
        assert config.max_correction_rounds == 2

    def test_parse_from_dict(self):
        """YAML相当のdictからパース"""
        from hokusai.config.loaders import _parse_cross_review_config

        config_dict = {
            "cross_review": {
                "enabled": True,
                "model": "codex-latest",
                "phases": [4],
                "timeout": 600,
                "on_failure": "block",
                "max_correction_rounds": 3,
            }
        }
        result = _parse_cross_review_config(config_dict)
        assert result.enabled is True
        assert result.model == "codex-latest"
        assert result.phases == [4]
        assert result.timeout == 600
        assert result.on_failure == "block"
        assert result.max_correction_rounds == 3

    def test_parse_empty(self):
        """cross_review セクションがない場合"""
        from hokusai.config.loaders import _parse_cross_review_config

        result = _parse_cross_review_config({})
        assert result.enabled is False

    def test_parse_invalid_values(self):
        """不正値は安全なデフォルトにフォールバック"""
        from hokusai.config.loaders import _parse_cross_review_config

        config_dict = {
            "cross_review": {
                "phases": ["x", 99],
                "on_failure": "invalid",
                "max_correction_rounds": 0,
            }
        }
        result = _parse_cross_review_config(config_dict)
        assert result.phases == [2, 4]
        assert result.on_failure == "warn"
        assert result.max_correction_rounds == 2

    def test_workflow_config_includes_cross_review(self):
        """WorkflowConfig に cross_review が含まれること"""
        config = WorkflowConfig()
        assert hasattr(config, "cross_review")
        assert isinstance(config.cross_review, CrossReviewConfig)
        assert config.cross_review.enabled is False


# ---------------------------------------------------------------------------
# Phase node integration tests (cross-review gate)
# ---------------------------------------------------------------------------

class TestPhaseNodeCrossReviewGate:
    """Phase 2/4 で block 時に完了扱いにならないことを検証"""

    @patch("hokusai.nodes.phase2_research._validate_research_output")
    @patch("hokusai.nodes.phase2_research._verify_subpage_content")
    @patch("hokusai.nodes.phase2_research.save_to_subpage_or_create", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase2_research.execute_cross_review")
    @patch("hokusai.nodes.phase2_research.ClaudeCodeClient")
    def test_phase2_blocked_does_not_complete(
        self,
        mock_claude_cls,
        mock_cross_review,
        mock_save,
        mock_verify_content,
        mock_validate_output,
        minimal_state,
    ):
        from hokusai.nodes.phase2_research import phase2_research_node

        mock_claude = Mock()
        mock_claude.execute_prompt.return_value = "## 事前調査結果\ncontent"
        mock_claude_cls.return_value = mock_claude

        blocked_state = dict(minimal_state)
        blocked_state["waiting_for_human"] = True
        blocked_state["human_input_request"] = "cross_review_blocked"
        mock_cross_review.return_value = blocked_state

        config = WorkflowConfig(cross_review=CrossReviewConfig(enabled=True, on_failure="block"))
        with patch("hokusai.nodes.phase2_research.get_config", return_value=config):
            result = phase2_research_node(minimal_state)

        assert result["phases"][2]["status"] == "failed"
        assert result["waiting_for_human"] is True

    @patch("hokusai.nodes.phase4_plan.save_to_subpage_or_create", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase4_plan.execute_cross_review")
    @patch("hokusai.nodes.phase4_plan.ClaudeCodeClient")
    def test_phase4_blocked_does_not_complete(
        self,
        mock_claude_cls,
        mock_cross_review,
        mock_save,
        minimal_state,
    ):
        from hokusai.nodes.phase4_plan import phase4_plan_node

        mock_claude = Mock()
        # 妥当性検証を通過する有効な計画を返す
        valid_plan = (
            "## 開発計画\n"
            "- [ ] **1.1** APIスキーマ変更\n"
            "- [ ] **1.2** バックエンド実装\n"
            "- [ ] **2.0** フロントエンド実装\n"
            "### 変更予定ファイル\n- a.py"
        )
        mock_claude.execute_skill.return_value = {"work_plan": valid_plan}
        mock_claude_cls.return_value = mock_claude

        blocked_state = dict(minimal_state)
        blocked_state["waiting_for_human"] = True
        blocked_state["human_input_request"] = "cross_review_blocked"
        mock_cross_review.return_value = blocked_state

        config = WorkflowConfig(cross_review=CrossReviewConfig(enabled=True, on_failure="block"))
        with patch("hokusai.nodes.phase4_plan.get_config", return_value=config):
            result = phase4_plan_node(minimal_state)

        assert result["phases"][4]["status"] == "failed"
        assert result["waiting_for_human"] is True
        # 子ページ方式では save_to_subpage_or_create は cross-review の前に実行される
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# cross_review 設定とログ出力テスト
# ---------------------------------------------------------------------------

class TestCrossReviewConfigAndLogging:
    """cross_review 設定の実効値・ログ出力・review_status のテスト"""

    def test_config_model_override(self):
        """設定で model を上書きした場合、その値が CodexClient に使われること"""
        config = WorkflowConfig(
            cross_review=CrossReviewConfig(enabled=True, model="codex-mini-latest"),
        )
        assert config.cross_review.model == "codex-mini-latest"

    def test_config_default_model(self):
        """デフォルトのモデルは codex-mini-latest"""
        config = CrossReviewConfig()
        assert config.model == "codex-mini-latest"

    def test_cross_review_prompts_require_japanese_output(self):
        """Phase 2/3/4 の cross review prompt が日本語固定を明示すること"""
        for phase in (2, 3, 4):
            prompt = CROSS_REVIEW_PROMPTS[phase]
            assert "必ず日本語で出力してください" in prompt
            assert "summary" in prompt
            assert "findings.title" in prompt
            assert "findings.description" in prompt
            assert "findings.suggestion" in prompt
            assert "英語では出力しないでください" in prompt

    def test_log_cross_review_config(self):
        """_log_cross_review_config がログに設定値を出力すること"""
        from hokusai.workflow import _log_cross_review_config
        import logging

        config = WorkflowConfig(
            cross_review=CrossReviewConfig(
                enabled=True,
                model="codex-mini-latest",
                on_failure="block",
                phases=[2, 3, 4],
            ),
        )

        with patch("hokusai.workflow.logger") as mock_logger:
            _log_cross_review_config(config)
            mock_logger.info.assert_called_once()
            log_msg = mock_logger.info.call_args[0][0]
            assert "enabled=True" in log_msg
            assert "model=codex-mini-latest" in log_msg
            assert "on_failure=block" in log_msg
            assert "[2, 3, 4]" in log_msg

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_execute_cross_review_passes_japanese_prompt(
        self, mock_cls, mock_notion, minimal_state, approve_review_result,
    ):
        """execute_cross_review が日本語固定の prompt を CodexClient に渡すこと"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.return_value = approve_review_result
        mock_cls.return_value = mock_client

        config = WorkflowConfig(
            cross_review=CrossReviewConfig(enabled=True, phases=[3]),
        )
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            execute_cross_review(minimal_state, "test design document", phase=3)

        review_prompt = mock_client.review_document.call_args.kwargs["review_prompt"]
        assert "必ず日本語で出力してください" in review_prompt
        assert "英語では出力しないでください" in review_prompt

    @patch("hokusai.utils.cross_review._save_review_to_notion")
    @patch("hokusai.utils.cross_review.CodexClient")
    def test_review_status_completed_on_success(
        self, mock_cls, mock_notion, minimal_state, approve_review_result,
    ):
        """レビュー成功時に review_status=completed が設定されること"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.return_value = approve_review_result
        mock_cls.return_value = mock_client

        config = WorkflowConfig(
            cross_review=CrossReviewConfig(enabled=True, phases=[2]),
        )
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "test document", phase=2)

        assert result["phases"][2].get("review_status") == "completed"

    def test_review_status_not_run_when_disabled(self, minimal_state):
        """クロスレビュー無効時に review_status=not_run が設定されること"""
        from hokusai.utils.cross_review import execute_cross_review

        config = WorkflowConfig(
            cross_review=CrossReviewConfig(enabled=False),
        )
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "test document", phase=2)

        assert result["phases"][2].get("review_status") == "not_run"

    @patch("hokusai.utils.cross_review.CodexClient")
    def test_review_status_failed_on_error(self, mock_cls, minimal_state):
        """レビュー実行エラー時に review_status=failed が設定されること"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.side_effect = RuntimeError("model not supported")
        mock_cls.return_value = mock_client

        config = WorkflowConfig(
            cross_review=CrossReviewConfig(
                enabled=True, phases=[2], on_failure="warn", max_correction_rounds=1,
            ),
        )
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "test document", phase=2)

        assert result["phases"][2].get("review_status") == "failed"
        assert result["cross_review_results"] == {}

    @patch("hokusai.utils.cross_review.CodexClient")
    def test_block_mode_stops_on_failure(self, mock_cls, minimal_state):
        """on_failure=block でレビュー失敗時にフェーズが停止すること"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.side_effect = RuntimeError("model not supported")
        mock_cls.return_value = mock_client

        config = WorkflowConfig(
            cross_review=CrossReviewConfig(
                enabled=True, phases=[2], on_failure="block", max_correction_rounds=1,
            ),
        )
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "test document", phase=2)

        assert result["waiting_for_human"] is True
        assert result["phases"][2].get("review_status") == "failed"

    @patch("hokusai.utils.cross_review.CodexClient")
    def test_error_detail_includes_model(self, mock_cls, minimal_state):
        """エラー監査ログにモデル名が含まれること"""
        from hokusai.utils.cross_review import execute_cross_review

        mock_client = Mock()
        mock_client.review_document.side_effect = RuntimeError("model not supported")
        mock_cls.return_value = mock_client

        config = WorkflowConfig(
            cross_review=CrossReviewConfig(
                enabled=True, model="o4-mini", phases=[2],
                on_failure="warn", max_correction_rounds=1,
            ),
        )
        with patch("hokusai.utils.cross_review.get_config", return_value=config):
            result = execute_cross_review(minimal_state, "test document", phase=2)

        # 監査ログに model 情報が含まれること
        failed_log = [
            log for log in result["audit_log"]
            if log.get("action") == "cross_review_failed"
        ]
        assert len(failed_log) == 1
        assert "model=o4-mini" in failed_log[0].get("error", "")
