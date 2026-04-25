"""
NotionTaskClient のテスト

検証項目:
- HOKUSAI_SKIP_NOTION=1 で Notion操作がスキップされること
- スキップ時に「成功」メッセージが出力されないこと
- 構造化結果（NotionOperationResult）が正しく返されること
- 例外時に failed 結果が返されること
"""

from unittest.mock import patch, MagicMock

import pytest

from hokusai.integrations.task_backend.notion import (
    NotionTaskClient,
    NotionOperationResult,
    NotionResult,
)


@pytest.fixture(autouse=True)
def _no_skip_notion(monkeypatch):
    """HOKUSAI_SKIP_NOTION を確実に無効化"""
    monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)


@pytest.fixture
def mock_claude():
    """ClaudeCodeClient のモック"""
    return MagicMock()


@pytest.fixture
def client(mock_claude):
    """テスト用 NotionTaskClient"""
    return NotionTaskClient(claude_client=mock_claude)


# === Task A: HOKUSAI_SKIP_NOTION チェック ===


class TestSkipNotionFlag:
    """HOKUSAI_SKIP_NOTION=1 設定時のスキップ動作"""

    def test_update_status_skipped(self, client, mock_claude, monkeypatch, capsys):
        """update_status が SKIP_NOTION で実行されない"""
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        result = client.update_status("https://notion.so/page-abc123", "進行中")

        mock_claude.execute_prompt.assert_not_called()
        assert result.result == NotionResult.SKIPPED
        assert result.operation == "update_status"
        captured = capsys.readouterr()
        assert "⏭️" in captured.out
        assert "📝" not in captured.out

    def test_prepend_content_skipped(self, client, mock_claude, monkeypatch, capsys):
        """prepend_content が SKIP_NOTION で実行されない"""
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        result = client.prepend_content("https://notion.so/page-abc123", "# content")

        mock_claude.execute_prompt.assert_not_called()
        assert result.result == NotionResult.SKIPPED
        captured = capsys.readouterr()
        assert "📝" not in captured.out

    def test_append_progress_skipped(self, client, mock_claude, monkeypatch, capsys):
        """append_progress が SKIP_NOTION で実行されない"""
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        result = client.append_progress("https://notion.so/page-abc123", "progress")

        mock_claude.execute_prompt.assert_not_called()
        assert result.result == NotionResult.SKIPPED
        captured = capsys.readouterr()
        assert "📝" not in captured.out

    def test_update_checkboxes_skipped(self, client, mock_claude, monkeypatch, capsys):
        """update_checkboxes が SKIP_NOTION で実行されない"""
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        result = client.update_checkboxes(
            "https://notion.so/page-abc123", ["item1"]
        )

        mock_claude.execute_prompt.assert_not_called()
        assert result.result == NotionResult.SKIPPED
        captured = capsys.readouterr()
        assert "📝" not in captured.out

    def test_skip_reason_recorded(self, client, monkeypatch):
        """スキップ理由が結果に含まれる"""
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        result = client.update_status("https://notion.so/page-abc123", "完了")

        assert result.reason is not None
        assert "HOKUSAI_SKIP_NOTION" in result.reason


# === Task B: 構造化結果 ===


class TestStructuredResult:
    """NotionOperationResult の構造化結果"""

    @patch("hokusai.integrations.task_backend.notion.get_config")
    def test_success_result(self, mock_config, client, mock_claude):
        """成功時に SUCCESS 結果が返る"""
        mock_config.return_value.command_timeout = 60
        mock_claude.execute_prompt.return_value = "OK"

        result = client.update_status("https://notion.so/page-abc123", "進行中")

        assert isinstance(result, NotionOperationResult)
        assert result.result == NotionResult.SUCCESS
        assert result.is_success is True
        assert result.operation == "update_status"

    @patch("hokusai.integrations.task_backend.notion.get_config")
    def test_failed_result_on_exception(self, mock_config, client, mock_claude):
        """例外時に FAILED 結果が返る"""
        mock_config.return_value.command_timeout = 60
        mock_claude.execute_prompt.side_effect = RuntimeError("connection refused")

        result = client.update_status("https://notion.so/page-abc123", "進行中")

        assert result.result == NotionResult.FAILED
        assert result.is_success is False
        assert "connection refused" in result.reason

    @patch("hokusai.integrations.task_backend.notion.get_config")
    def test_failed_result_does_not_raise(self, mock_config, client, mock_claude):
        """例外時に raise しない（呼び出し側で結果を処理）"""
        mock_config.return_value.command_timeout = 60
        mock_claude.execute_prompt.side_effect = RuntimeError("timeout")

        # 例外が raise されないことを確認
        result = client.prepend_content("https://notion.so/page-abc123", "content")
        assert result.result == NotionResult.FAILED

    @patch("hokusai.integrations.task_backend.notion.get_config")
    def test_append_progress_success(self, mock_config, client, mock_claude):
        """append_progress 成功時の結果"""
        mock_config.return_value.command_timeout = 60
        mock_claude.execute_prompt.return_value = "done"

        result = client.append_progress("https://notion.so/page-abc123", "text")

        assert result.result == NotionResult.SUCCESS
        assert result.operation == "append_progress"

    @patch("hokusai.integrations.task_backend.notion.get_config")
    def test_update_checkboxes_empty_items(self, mock_config, client, mock_claude):
        """空の completed_items は成功扱い"""
        result = client.update_checkboxes("https://notion.so/page-abc123", [])

        mock_claude.execute_prompt.assert_not_called()
        assert result.result == NotionResult.SUCCESS


# === 成功メッセージ不出力の確認 ===


class TestNoFalseSuccessMessage:
    """Notion未接続時に成功メッセージが出力されないことを確認"""

    def test_skip_no_success_emoji(self, client, monkeypatch, capsys):
        """スキップ時に 📝 が出ない"""
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        client.update_status("https://notion.so/page-abc123", "進行中")
        client.prepend_content("https://notion.so/page-abc123", "content")
        client.append_progress("https://notion.so/page-abc123", "progress")
        client.update_checkboxes("https://notion.so/page-abc123", ["item"])

        captured = capsys.readouterr()
        assert "📝" not in captured.out
        assert captured.out.count("⏭️") == 4

    @patch("hokusai.integrations.task_backend.notion.get_config")
    def test_error_no_success_emoji(self, mock_config, client, mock_claude, capsys):
        """エラー時に 📝 が出ない"""
        mock_config.return_value.command_timeout = 60
        mock_claude.execute_prompt.side_effect = RuntimeError("error")

        client.update_status("https://notion.so/page-abc123", "進行中")

        captured = capsys.readouterr()
        assert "📝" not in captured.out
        assert "⚠️" in captured.out
