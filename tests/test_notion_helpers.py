"""
save_content_to_notion の戻り値ハンドリングテスト

検証項目:
- insert_after_existing が True を返した場合 → 成功ログ/表示
- insert_after_existing が False を返した場合 → 警告ログ/表示（成功メッセージなし）
- 例外時 → 警告ログ/表示
- save_to_subpage_or_create の SKIP_NOTION 尊重（Issue #75）
"""

from unittest.mock import MagicMock, patch

import pytest

from hokusai.utils.notion_helpers import (
    save_content_to_notion,
    save_to_subpage_or_create,
)


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


class TestSaveToSubpageOrCreateSkipNotion:
    """save_to_subpage_or_create の HOKUSAI_SKIP_NOTION=1 尊重（Issue #75）"""

    def test_skips_when_env_set(self, monkeypatch, caplog):
        """HOKUSAI_SKIP_NOTION=1 のとき state を変更せず即返す"""
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")
        state = {"task_url": "https://example/issue/1", "phase_subpages": {}}

        # update_subpage_content / create_phase_subpage が呼ばれないことを
        # mock で検証
        with patch(
            "hokusai.utils.notion_helpers.update_subpage_content"
        ) as mock_update, patch(
            "hokusai.utils.notion_helpers.create_phase_subpage"
        ) as mock_create:
            with caplog.at_level("INFO", logger="hokusai"):
                returned = save_to_subpage_or_create(
                    state, "https://example/issue/1", phase=2, content="hello"
                )

        # state は変更されず同じ dict が返る
        assert returned is state
        assert state["phase_subpages"] == {}
        # 副作用なし
        mock_update.assert_not_called()
        mock_create.assert_not_called()
        # ログに skip メッセージ（Issue #113 / PR #114 で env 名固定の文言を
        # 一般化したため、特定 env 名ではなく "スキップ" を含むかで検証）
        log_text = " ".join(r.getMessage() for r in caplog.records)
        assert "スキップ" in log_text
        assert "Phase 2" in log_text

    def test_normal_path_when_env_unset(self, monkeypatch):
        """HOKUSAI_SKIP_NOTION 未設定 + Notion task_url 時は通常通り
        create_phase_subpage を呼ぶ（§15: task_url は Notion ページである必要がある）"""
        monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)
        # task_url が Notion ページ参照（末尾32hex）でないと subpage 保存は skip される
        notion_url = (
            "https://notion.so/workspace/task-aabbccdd11223344aabbccdd11223344"
        )
        state = {"task_url": notion_url, "phase_subpages": {}}

        with patch(
            "hokusai.utils.notion_helpers.create_phase_subpage"
        ) as mock_create:
            mock_create.return_value = "https://notion.so/subpage-xxx"
            result = save_to_subpage_or_create(
                state, notion_url, phase=2, content="hello"
            )

        mock_create.assert_called_once()
        assert result["phase_subpages"][2] == "https://notion.so/subpage-xxx"
