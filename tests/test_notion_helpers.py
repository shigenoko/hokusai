"""
save_content_to_notion の戻り値ハンドリングテスト

検証項目:
- insert_after_existing が True を返した場合 → 成功ログ/表示
- insert_after_existing が False を返した場合 → 警告ログ/表示（成功メッセージなし）
- 例外時 → 警告ログ/表示
"""

from unittest.mock import patch, MagicMock

import pytest

from hokusai.utils.notion_helpers import save_content_to_notion


@pytest.fixture(autouse=True)
def _no_skip_notion(monkeypatch):
    """HOKUSAI_SKIP_NOTION を確実に無効化"""
    monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)


@pytest.fixture
def mock_notion_client():
    """NotionMCPClient のモック（関数内遅延 import のためモジュール側をパッチ）"""
    with patch("hokusai.integrations.notion_mcp.NotionMCPClient") as cls:
        client = MagicMock()
        cls.return_value = client
        yield client


class TestSaveContentToNotionReturnHandling:
    """insert_after_existing の戻り値に応じたログ/表示の検証"""

    def test_success_shows_success_message(self, mock_notion_client, capsys):
        """True 戻り時に成功メッセージが出る"""
        mock_notion_client.insert_after_existing.return_value = True

        save_content_to_notion("https://notion.so/page-aabbccdd11223344aabbccdd11223344", "# content")

        captured = capsys.readouterr()
        assert "📝 コンテンツをNotionに保存しました" in captured.out
        assert "⚠️" not in captured.out

    def test_failure_shows_warning_not_success(self, mock_notion_client, capsys):
        """False 戻り時に成功メッセージが出ず、警告メッセージが出る"""
        mock_notion_client.insert_after_existing.return_value = False

        save_content_to_notion("https://notion.so/page-aabbccdd11223344aabbccdd11223344", "# content")

        captured = capsys.readouterr()
        assert "📝" not in captured.out
        assert "⚠️  Notionへの保存に失敗しました" in captured.out

    def test_exception_shows_warning(self, mock_notion_client, capsys):
        """例外時に警告メッセージが出る"""
        mock_notion_client.insert_after_existing.side_effect = RuntimeError("接続エラー")

        save_content_to_notion("https://notion.so/page-aabbccdd11223344aabbccdd11223344", "# content")

        captured = capsys.readouterr()
        assert "📝" not in captured.out
        assert "⚠️" in captured.out
        assert "接続エラー" in captured.out

    def test_empty_content_skips_save(self, mock_notion_client, capsys):
        """空コンテンツは保存をスキップ"""
        save_content_to_notion("https://notion.so/page-aabbccdd11223344aabbccdd11223344", "")

        mock_notion_client.insert_after_existing.assert_not_called()

    def test_skip_notion_env(self, mock_notion_client, capsys, monkeypatch):
        """HOKUSAI_SKIP_NOTION 設定時はスキップ"""
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        save_content_to_notion("https://notion.so/page-aabbccdd11223344aabbccdd11223344", "# content")

        mock_notion_client.insert_after_existing.assert_not_called()
        captured = capsys.readouterr()
        assert "⏭️" in captured.out

    def test_after_marker_passed_through(self, mock_notion_client):
        """after_marker が insert_after_existing に渡される"""
        mock_notion_client.insert_after_existing.return_value = True

        save_content_to_notion(
            "https://notion.so/page-aabbccdd11223344aabbccdd11223344",
            "# content",
            after_marker="::: callout...:::",
        )

        mock_notion_client.insert_after_existing.assert_called_once_with(
            "https://notion.so/page-aabbccdd11223344aabbccdd11223344",
            "# content",
            after_marker="::: callout...:::",
        )
