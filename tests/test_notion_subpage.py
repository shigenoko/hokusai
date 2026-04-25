"""
Notion子ページアーキテクチャのテスト

検証項目:
- notion_helpers の子ページ関連関数の引数検証
- Phase 2/3/4 統合テスト（子ページ作成 + state への URL 保存 + 実行順序）
- cross-review が子ページに保存されることの検証
- 冪等性テスト（再実行時に子ページが増殖しないこと）
- 永続化互換性テスト（SQLite 保存→再開→参照サイクル）
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from hokusai.state import create_initial_state


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _build_state(**overrides):
    """テスト用の最小限 state を生成"""
    state = create_initial_state(
        task_url="https://notion.so/workspace/task-aabbccdd11223344aabbccdd11223344",
        task_title="テストタスク",
        branch_name="feature/test",
    )
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# notion_helpers 関数テスト
# ---------------------------------------------------------------------------
class TestCreatePhaseSubpage:
    """create_phase_subpage の引数検証（2段階方式）"""

    @patch("hokusai.utils.notion_helpers.update_subpage_content")
    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_creates_subpage_and_returns_url(self, MockClient, mock_update, monkeypatch):
        """タイトルのみ作成 → update_subpage_content で本文書き込み"""
        monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)
        mock_instance = MagicMock()
        mock_instance._extract_page_id.return_value = "aabbccdd-1122-3344-aabb-ccdd11223344"
        mock_instance.claude.execute_prompt.return_value = (
            "作成完了: https://www.notion.so/workspace/new-page-1234567890abcdef1234567890abcdef"
        )
        MockClient.return_value = mock_instance
        mock_update.return_value = True

        from hokusai.utils.notion_helpers import create_phase_subpage

        url = create_phase_subpage(
            "https://notion.so/task-aabbccdd11223344aabbccdd11223344",
            phase=2,
            title="Phase 2: 事前調査",
            content="## 調査レポート\nテスト内容",
        )

        assert url == "https://www.notion.so/workspace/new-page-1234567890abcdef1234567890abcdef"
        # Step 1: タイトルのみ作成
        mock_instance.claude.execute_prompt.assert_called_once()
        prompt_arg = mock_instance.claude.execute_prompt.call_args[0][0]
        assert "タイトルのみ" in prompt_arg or "本文は空" in prompt_arg
        # Step 2: 本文書き込み
        mock_update.assert_called_once_with(
            "https://www.notion.so/workspace/new-page-1234567890abcdef1234567890abcdef",
            "## 調査レポート\nテスト内容",
        )

    @patch("hokusai.utils.notion_helpers.update_subpage_content")
    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_returns_none_when_update_fails(self, MockClient, mock_update, monkeypatch):
        """update_subpage_content 失敗時に None を返す"""
        monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)
        mock_instance = MagicMock()
        mock_instance._extract_page_id.return_value = "aabbccdd-1122-3344-aabb-ccdd11223344"
        mock_instance.claude.execute_prompt.return_value = (
            "作成完了: https://www.notion.so/workspace/new-page-1234567890abcdef1234567890abcdef"
        )
        MockClient.return_value = mock_instance
        mock_update.return_value = False  # 本文書き込み失敗

        from hokusai.utils.notion_helpers import create_phase_subpage

        url = create_phase_subpage(
            "https://notion.so/task-aabbccdd11223344aabbccdd11223344",
            phase=2,
            title="Phase 2: 事前調査",
            content="## 調査レポート\nテスト内容",
        )

        assert url is None
        mock_update.assert_called_once()

    def test_skip_notion_returns_none(self, monkeypatch):
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        from hokusai.utils.notion_helpers import create_phase_subpage

        url = create_phase_subpage("https://notion.so/task", 2, "title", "content")
        assert url is None

    def test_empty_content_returns_none(self, monkeypatch):
        monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)

        from hokusai.utils.notion_helpers import create_phase_subpage

        url = create_phase_subpage("https://notion.so/task", 2, "title", "")
        assert url is None


class TestUpdateSubpageContent:
    """update_subpage_content の引数検証"""

    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_updates_existing_page(self, MockClient, monkeypatch):
        monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)
        mock_instance = MagicMock()
        mock_instance._extract_page_id.return_value = "12345678-1234-1234-1234-123456789012"
        mock_instance.claude.execute_prompt.return_value = "更新完了"
        MockClient.return_value = mock_instance

        from hokusai.utils.notion_helpers import update_subpage_content

        result = update_subpage_content("https://notion.so/page", "新しい内容")
        assert result is True


class TestAppendToSubpage:
    """append_to_subpage の引数検証"""

    @patch("hokusai.integrations.notion_mcp.NotionMCPClient")
    def test_appends_content(self, MockClient, monkeypatch):
        monkeypatch.delenv("HOKUSAI_SKIP_NOTION", raising=False)
        mock_instance = MagicMock()
        mock_instance.append_content.return_value = True
        MockClient.return_value = mock_instance

        from hokusai.utils.notion_helpers import append_to_subpage

        result = append_to_subpage("https://notion.so/page", "追記コンテンツ")
        assert result is True
        mock_instance.append_content.assert_called_once_with("https://notion.so/page", "追記コンテンツ")


class TestSaveToSubpageOrCreate:
    """save_to_subpage_or_create の冪等性検証"""

    @patch("hokusai.utils.notion_helpers.create_phase_subpage")
    def test_creates_new_subpage_when_none_exists(self, mock_create):
        mock_create.return_value = "https://notion.so/new-subpage"

        from hokusai.utils.notion_helpers import save_to_subpage_or_create

        state = _build_state()
        state = save_to_subpage_or_create(state, state["task_url"], 3, "コンテンツ", "wf-test")

        mock_create.assert_called_once()
        assert state["phase_subpages"][3] == "https://notion.so/new-subpage"

    @patch("hokusai.utils.notion_helpers.update_subpage_content")
    @patch("hokusai.utils.notion_helpers.create_phase_subpage")
    def test_updates_existing_subpage(self, mock_create, mock_update):
        from hokusai.utils.notion_helpers import save_to_subpage_or_create

        mock_update.return_value = True
        state = _build_state()
        state["phase_subpages"] = {3: "https://notion.so/existing-subpage"}

        state = save_to_subpage_or_create(state, state["task_url"], 3, "新コンテンツ", "wf-test")

        mock_create.assert_not_called()
        mock_update.assert_called_once_with("https://notion.so/existing-subpage", "新コンテンツ")
        # URL は変更されない
        assert state["phase_subpages"][3] == "https://notion.so/existing-subpage"

    @patch("hokusai.utils.notion_helpers.save_content_to_notion")
    @patch("hokusai.utils.notion_helpers.update_subpage_content")
    @patch("hokusai.utils.notion_helpers.create_phase_subpage")
    def test_update_fail_invalidates_url_and_creates_new(
        self, mock_create, mock_update, mock_save_notion,
    ):
        """既存子ページ更新失敗時（削除済み等）→ URL無効化 → 新規作成"""
        from hokusai.utils.notion_helpers import save_to_subpage_or_create

        mock_update.return_value = False  # 更新失敗（削除済み）
        mock_create.return_value = "https://notion.so/new-recreated-subpage"

        state = _build_state()
        state["phase_subpages"] = {3: "https://notion.so/deleted-subpage"}

        state = save_to_subpage_or_create(state, state["task_url"], 3, "コンテンツ", "wf-test")

        # 既存URL更新を試行
        mock_update.assert_called_once_with("https://notion.so/deleted-subpage", "コンテンツ")
        # 新規作成に切替
        mock_create.assert_called_once()
        # 親タスクへのフォールバック保存は行わない
        mock_save_notion.assert_not_called()
        # state に新しいURLが記録される
        assert state["phase_subpages"][3] == "https://notion.so/new-recreated-subpage"


class TestSaveToSubpageFailFast:
    """子ページ新規作成失敗時のテスト"""

    @patch("hokusai.utils.notion_helpers.create_phase_subpage")
    def test_create_failure_raises_runtime_error(self, mock_create):
        """create_phase_subpage() が None → RuntimeError"""
        from hokusai.utils.notion_helpers import save_to_subpage_or_create

        mock_create.return_value = None  # 作成失敗

        state = _build_state()
        with pytest.raises(RuntimeError, match="子ページの作成に失敗しました"):
            save_to_subpage_or_create(state, state["task_url"], 2, "コンテンツ", "wf-test")

    @patch("hokusai.utils.notion_helpers.create_phase_subpage")
    @patch("hokusai.utils.notion_helpers.update_subpage_content")
    def test_deleted_url_then_create_failure_raises(self, mock_update, mock_create):
        """既存URL更新失敗 → 新規作成も失敗 → RuntimeError"""
        from hokusai.utils.notion_helpers import save_to_subpage_or_create

        mock_update.return_value = False  # 更新失敗（削除済み）
        mock_create.return_value = None  # 新規作成も失敗

        state = _build_state()
        state["phase_subpages"] = {2: "https://notion.so/deleted-subpage"}

        with pytest.raises(RuntimeError, match="子ページの作成に失敗しました"):
            save_to_subpage_or_create(state, state["task_url"], 2, "コンテンツ", "wf-test")

        # 既存URL更新を試行後、新規作成も試行
        mock_update.assert_called_once()
        mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 2 統合テスト
# ---------------------------------------------------------------------------
class TestPhase2SubpageIntegration:
    """Phase 2 の調査レポートが子ページとして保存されることを検証"""

    @patch("hokusai.nodes.phase2_research._validate_research_output")
    @patch("hokusai.nodes.phase2_research._verify_subpage_content")
    @patch("hokusai.nodes.phase2_research._verify_notion_state")
    @patch("hokusai.nodes.phase2_research.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase2_research.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase2_research.ClaudeCodeClient")
    def test_research_saved_as_subpage(self, MockClient, mock_save_subpage, mock_cross_review, mock_verify, mock_verify_content, mock_validate_output):
        from hokusai.nodes.phase2_research import phase2_research_node

        state = _build_state()

        mock_instance = MagicMock()
        mock_instance.execute_prompt.return_value = "## 事前調査結果\nテスト調査内容\nGenerated by `/task-research`"
        MockClient.return_value = mock_instance

        # save_to_subpage_or_create が state を返すようにモック
        mock_save_subpage.side_effect = lambda s, *a, **kw: s

        phase2_research_node(state)

        mock_save_subpage.assert_called_once()
        call_args = mock_save_subpage.call_args
        # save_to_subpage_or_create(state, task_url, phase=2, content=..., workflow_id=...)
        assert call_args.kwargs.get("phase", call_args[0][2] if len(call_args[0]) > 2 else None) == 2

    @patch("hokusai.nodes.phase2_research._validate_research_output")
    @patch("hokusai.nodes.phase2_research._verify_subpage_content")
    @patch("hokusai.nodes.phase2_research._verify_notion_state")
    @patch("hokusai.nodes.phase2_research.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase2_research.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase2_research.ClaudeCodeClient")
    def test_subpage_created_before_cross_review(self, MockClient, mock_save_subpage, mock_cross_review, mock_verify, mock_verify_content, mock_validate_output):
        """子ページ作成が cross-review より先に実行されることを検証"""
        from hokusai.nodes.phase2_research import phase2_research_node

        call_order = []

        def track_save(s, *a, **kw):
            call_order.append("save_subpage")
            return s

        def track_cross_review(s, *a, **kw):
            call_order.append("cross_review")
            return s

        mock_save_subpage.side_effect = track_save
        mock_cross_review.side_effect = track_cross_review

        state = _build_state()
        mock_instance = MagicMock()
        mock_instance.execute_prompt.return_value = "## 事前調査結果\nテスト\nGenerated by `/task-research`"
        MockClient.return_value = mock_instance

        phase2_research_node(state)

        assert call_order == ["save_subpage", "cross_review"]


# ---------------------------------------------------------------------------
# Phase 3 統合テスト
# ---------------------------------------------------------------------------
class TestPhase3SubpageIntegration:
    """Phase 3 の設計チェック結果が子ページとして保存されることを検証"""

    @patch("hokusai.nodes.phase3_design._validate_design_output")
    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_design_saved_as_subpage(self, MockClient, mock_save_subpage, mock_cross_review, mock_verify_content, mock_validate_output):
        from hokusai.nodes.phase3_design import phase3_design_node

        state = _build_state(research_result="## 調査結果\nテスト")

        mock_instance = MagicMock()
        mock_instance.execute_prompt.return_value = "## 設計チェック\nテスト設計"
        MockClient.return_value = mock_instance
        mock_save_subpage.side_effect = lambda s, *a, **kw: s

        phase3_design_node(state)

        mock_save_subpage.assert_called_once()
        call_args = mock_save_subpage.call_args
        assert call_args.kwargs.get("phase", call_args[0][2] if len(call_args[0]) > 2 else None) == 3
        saved_content = call_args.kwargs.get("content") or call_args[0][3]
        assert "# Phase 3: 設計" in saved_content
        assert "## 最新版ドキュメント" in saved_content

    @patch("hokusai.nodes.phase3_design._validate_design_output")
    @patch("hokusai.nodes.phase3_design._verify_design_subpage_content")
    @patch("hokusai.nodes.phase3_design.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase3_design.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase3_design.ClaudeCodeClient")
    def test_subpage_created_before_cross_review(self, MockClient, mock_save_subpage, mock_cross_review, mock_verify_content, mock_validate_output):
        """子ページ作成が cross-review より先に実行されることを検証"""
        from hokusai.nodes.phase3_design import phase3_design_node

        call_order = []

        def track_save(s, *a, **kw):
            call_order.append("save_subpage")
            return s

        def track_cross_review(s, *a, **kw):
            call_order.append("cross_review")
            return s

        mock_save_subpage.side_effect = track_save
        mock_cross_review.side_effect = track_cross_review

        state = _build_state(research_result="## 調査結果\nテスト")
        mock_instance = MagicMock()
        mock_instance.execute_prompt.return_value = "## 設計チェック\nテスト設計"
        MockClient.return_value = mock_instance

        phase3_design_node(state)

        assert call_order == ["save_subpage", "cross_review"]


# ---------------------------------------------------------------------------
# Phase 4 統合テスト
# ---------------------------------------------------------------------------
class TestPhase4SubpageIntegration:
    """Phase 4 の開発計画が子ページとして保存されることを検証"""

    @patch("hokusai.nodes.phase4_plan.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase4_plan.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase4_plan.ClaudeCodeClient")
    def test_plan_saved_as_subpage(self, MockClient, mock_save_subpage, mock_cross_review):
        from hokusai.nodes.phase4_plan import phase4_plan_node

        state = _build_state()

        mock_instance = MagicMock()
        mock_instance.execute_skill.return_value = {"output": "## 開発計画\n- [ ] **1.1** API変更\n- [ ] **1.2** 実装\n- [ ] **2.0** テスト"}
        MockClient.return_value = mock_instance
        mock_save_subpage.side_effect = lambda s, *a, **kw: s

        phase4_plan_node(state)

        mock_save_subpage.assert_called_once()
        call_args = mock_save_subpage.call_args
        assert call_args.kwargs.get("phase", call_args[0][2] if len(call_args[0]) > 2 else None) == 4
        saved_content = call_args.kwargs.get("content") or call_args[0][3]
        assert "# Phase 4: 作業計画" in saved_content
        assert "## 最新版ドキュメント" in saved_content

    @patch("hokusai.nodes.phase4_plan.execute_cross_review", side_effect=lambda s, *a, **kw: s)
    @patch("hokusai.nodes.phase4_plan.save_to_subpage_or_create")
    @patch("hokusai.nodes.phase4_plan.ClaudeCodeClient")
    def test_subpage_created_before_cross_review(self, MockClient, mock_save_subpage, mock_cross_review):
        """子ページ作成が cross-review より先に実行されることを検証"""
        from hokusai.nodes.phase4_plan import phase4_plan_node

        call_order = []

        def track_save(s, *a, **kw):
            call_order.append("save_subpage")
            return s

        def track_cross_review(s, *a, **kw):
            call_order.append("cross_review")
            return s

        mock_save_subpage.side_effect = track_save
        mock_cross_review.side_effect = track_cross_review

        state = _build_state()
        mock_instance = MagicMock()
        valid_plan = (
            "## 開発計画\n"
            "- [ ] **1.1** APIスキーマ変更\n"
            "- [ ] **1.2** バックエンド実装\n"
            "- [ ] **2.0** フロントエンド実装\n"
        )
        mock_instance.execute_skill.return_value = {"output": valid_plan}
        MockClient.return_value = mock_instance

        phase4_plan_node(state)

        assert call_order == ["save_subpage", "cross_review"]


# ---------------------------------------------------------------------------
# cross-review の子ページ保存テスト
# ---------------------------------------------------------------------------
class TestCrossReviewSubpageSave:
    """cross-review callout が子ページに保存されることを検証"""

    @patch("hokusai.utils.notion_helpers.append_to_subpage")
    @patch("hokusai.utils.notion_helpers.save_content_to_notion")
    @patch("hokusai.utils.notion_helpers.generate_cross_review_callout", return_value="callout content")
    def test_saves_to_subpage_when_url_exists(
        self, mock_gen, mock_save_notion, mock_append,
    ):
        from hokusai.utils.cross_review import _save_review_to_notion

        state = _build_state()
        state["phase_subpages"] = {3: "https://notion.so/subpage-3"}

        _save_review_to_notion(state, {"findings": []}, phase=3)

        mock_append.assert_called_once_with("https://notion.so/subpage-3", "callout content")
        mock_save_notion.assert_not_called()

    @patch("hokusai.utils.notion_helpers.append_to_subpage")
    @patch("hokusai.utils.notion_helpers.save_content_to_notion")
    @patch("hokusai.utils.notion_helpers.generate_cross_review_callout", return_value="callout content")
    def test_falls_back_to_task_page(
        self, mock_gen, mock_save_notion, mock_append,
    ):
        from hokusai.utils.cross_review import _save_review_to_notion

        state = _build_state()
        # phase_subpages が空 → フォールバック

        _save_review_to_notion(state, {"findings": []}, phase=3)

        mock_append.assert_not_called()
        mock_save_notion.assert_called_once()


# ---------------------------------------------------------------------------
# 永続化互換性テスト
# ---------------------------------------------------------------------------
class TestPersistenceCompatibility:
    """phase_subpages の SQLite 保存→再開→参照サイクルの検証"""

    def test_phase_subpages_int_key_conversion(self):
        """SQLite 保存後に文字列化されたキーが整数に正しく変換されること"""
        from hokusai.persistence.sqlite_store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = SQLiteStore(db_path)

            state = _build_state()
            state["phase_subpages"] = {
                2: "https://notion.so/phase-2",
                3: "https://notion.so/phase-3",
            }

            store.save_workflow(state["workflow_id"], state)
            loaded = store.load_workflow(state["workflow_id"])

            # キーが整数に変換されていること
            assert 2 in loaded["phase_subpages"]
            assert 3 in loaded["phase_subpages"]
            assert loaded["phase_subpages"][2] == "https://notion.so/phase-2"

    def test_cross_review_results_int_key_conversion(self):
        """cross_review_results のキーも整数に正しく変換されること"""
        from hokusai.persistence.sqlite_store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = SQLiteStore(db_path)

            state = _build_state()
            state["cross_review_results"] = {
                2: {"assessment": "approve"},
                4: {"assessment": "request_changes"},
            }

            store.save_workflow(state["workflow_id"], state)
            loaded = store.load_workflow(state["workflow_id"])

            assert 2 in loaded["cross_review_results"]
            assert 4 in loaded["cross_review_results"]

    def test_legacy_state_without_phase_subpages(self):
        """旧 state（phase_subpages フィールドなし）からの読み込みでエラーにならないこと"""
        from hokusai.persistence.sqlite_store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = SQLiteStore(db_path)

            # phase_subpages を含まない旧 state を直接保存
            state = _build_state()
            del state["phase_subpages"]

            store.save_workflow(state["workflow_id"], state)
            loaded = store.load_workflow(state["workflow_id"])

            # phase_subpages がなくてもエラーにならない
            assert loaded.get("phase_subpages") is None or loaded.get("phase_subpages") == {}

    def test_checkpoint_preserves_phase_subpages(self):
        """チェックポイント経由でも phase_subpages が保持されること"""
        from hokusai.persistence.sqlite_store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = SQLiteStore(db_path)

            state = _build_state()
            state["phase_subpages"] = {2: "https://notion.so/phase-2"}

            store.save_workflow(state["workflow_id"], state)
            store.save_checkpoint(state["workflow_id"], 2, state)

            loaded = store.load_checkpoint(state["workflow_id"], 2)
            assert 2 in loaded["phase_subpages"]
            assert loaded["phase_subpages"][2] == "https://notion.so/phase-2"

    def test_legacy_phase_page_fields_are_dropped_on_load(self):
        """旧 state の phase_page_status / phase_page_last_review_round は読込時に除去される"""
        from hokusai.persistence.sqlite_store import SQLiteStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = SQLiteStore(db_path)

            state = _build_state()
            state["phase_page_status"] = {2: "draft"}
            state["phase_page_last_review_round"] = {2: 1}

            store.save_workflow(state["workflow_id"], state)
            loaded = store.load_workflow(state["workflow_id"])

            assert "phase_page_status" not in loaded
            assert "phase_page_last_review_round" not in loaded
