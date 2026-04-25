"""hokusai/prompts/loader.py のテスト"""

import pytest
from pathlib import Path

from hokusai.prompts.loader import (
    get_prompt,
    list_prompts,
    read_prompt_file,
    write_prompt_file,
    _PROMPTS_DIR,
    _load_registry,
)


class TestGetPrompt:
    """get_prompt のテスト"""

    def test_returns_formatted_string(self):
        """変数埋め込みが正しく動作する"""
        result = get_prompt("phase2.task_research", task_url="https://example.com/task/1")
        assert "https://example.com/task/1" in result
        assert "タスクURL: https://example.com/task/1" in result
        assert "{task_url}" not in result

    def test_unknown_id_raises_error(self):
        """未定義 ID でエラーが発生する"""
        with pytest.raises(KeyError, match="Unknown prompt ID"):
            get_prompt("nonexistent.prompt")

    def test_missing_variable_raises_error(self):
        """必要な変数が不足している場合エラーが発生する"""
        with pytest.raises(KeyError):
            get_prompt("phase2.task_research")  # task_url が必要

    def test_static_prompt_no_variables(self):
        """変数なしの静的プロンプトが正しく読み込まれる"""
        result = get_prompt("cross_review.phase2")
        assert "事前調査ドキュメントのレビュアー" in result

    def test_phase2_retry_prompt(self):
        """Phase 2 リトライプロンプトの変数埋め込み"""
        result = get_prompt(
            "phase2.task_research_retry",
            task_url="https://example.com/task/1",
            previous_output="## 事前調査レポート\nテスト",
            validation_error="先頭行が不正です",
        )
        assert "https://example.com/task/1" in result
        assert "## 事前調査レポート\nテスト" in result
        assert "先頭行が不正です" in result

    def test_phase5_implementation_prompt(self):
        """Phase 5 実装プロンプトの動的セクション"""
        result = get_prompt(
            "phase5.implementation",
            repo_section="## 対象リポジトリ\ntest-repo\n",
            coding_rules_section="",
            task_url="https://example.com/task/1",
            work_plan_section="## 作業計画\nステップ1\n",
            expected_files_section="## 変更予定ファイル\n- src/main.ts\n",
        )
        assert "test-repo" in result
        assert "ステップ1" in result
        assert "src/main.ts" in result

    def test_phase4_append_system_prompt(self):
        """Phase 4 静的システムプロンプトの読み込み"""
        result = get_prompt("phase4.append_system_prompt")
        assert "Notionへの書き込みは一切行わないでください" in result


class TestListPrompts:
    """list_prompts のテスト"""

    def test_returns_all_entries(self):
        """全エントリが取得できる"""
        result = list_prompts()
        assert len(result) == 13
        ids = [entry["id"] for entry in result]
        assert "phase2.task_research" in ids
        assert "cross_review.phase4" in ids

    def test_entries_have_required_fields(self):
        """各エントリに必須フィールドがある"""
        result = list_prompts()
        for entry in result:
            assert "id" in entry
            assert "title" in entry
            assert "file" in entry
            assert "kind" in entry

    def test_entries_have_mtime(self):
        """各エントリにmtimeが付与される"""
        result = list_prompts()
        for entry in result:
            assert "mtime" in entry
            assert isinstance(entry["mtime"], float)


class TestReadPromptFile:
    """read_prompt_file のテスト"""

    def test_returns_raw_template(self):
        """生テンプレート（未埋め込み）が取得できる"""
        content = read_prompt_file("phase2.task_research")
        assert "{task_url}" in content
        assert "以下のNotionタスクについて" in content

    def test_unknown_id_raises_error(self):
        """未定義 ID でエラーが発生する"""
        with pytest.raises(KeyError, match="Unknown prompt ID"):
            read_prompt_file("nonexistent.prompt")


class TestWritePromptFile:
    """write_prompt_file のテスト"""

    def test_saves_content(self, tmp_path, monkeypatch):
        """ファイル保存が動作する"""
        # テスト用の一時レジストリを設定
        test_file = tmp_path / "test.md"
        test_file.write_text("{task_url} テスト", encoding="utf-8")

        import hokusai.prompts.loader as loader
        original_registry = loader._registry
        monkeypatch.setattr(loader, "_PROMPTS_DIR", tmp_path)
        monkeypatch.setattr(loader, "_registry", [
            {"id": "test.prompt", "file": "test.md", "variables": ["task_url"], "title": "Test", "kind": "prompt"},
        ])

        try:
            write_prompt_file("test.prompt", "新しい {task_url} テスト")
            assert test_file.read_text(encoding="utf-8") == "新しい {task_url} テスト"
        finally:
            loader._registry = original_registry

    def test_validates_required_variables(self, tmp_path, monkeypatch):
        """必須変数が欠落している場合エラーが発生する"""
        test_file = tmp_path / "test.md"
        test_file.write_text("{task_url} テスト", encoding="utf-8")

        import hokusai.prompts.loader as loader
        original_registry = loader._registry
        monkeypatch.setattr(loader, "_PROMPTS_DIR", tmp_path)
        monkeypatch.setattr(loader, "_registry", [
            {"id": "test.prompt", "file": "test.md", "variables": ["task_url"], "title": "Test", "kind": "prompt"},
        ])

        try:
            with pytest.raises(ValueError, match="必須変数が見つかりません"):
                write_prompt_file("test.prompt", "変数なしのテスト")
        finally:
            loader._registry = original_registry

    def test_empty_content_raises_error(self, tmp_path, monkeypatch):
        """空内容で保存するとエラーが発生する"""
        test_file = tmp_path / "test.md"
        test_file.write_text("dummy", encoding="utf-8")

        import hokusai.prompts.loader as loader
        original_registry = loader._registry
        monkeypatch.setattr(loader, "_PROMPTS_DIR", tmp_path)
        monkeypatch.setattr(loader, "_registry", [
            {"id": "test.prompt", "file": "test.md", "variables": [], "title": "Test", "kind": "prompt"},
        ])

        try:
            with pytest.raises(ValueError, match="テンプレートの内容が空です"):
                write_prompt_file("test.prompt", "")
        finally:
            loader._registry = original_registry

    def test_no_variables_prompt_saves(self, tmp_path, monkeypatch):
        """変数なしプロンプトの保存が成功する"""
        test_file = tmp_path / "test.md"
        test_file.write_text("元のテキスト", encoding="utf-8")

        import hokusai.prompts.loader as loader
        original_registry = loader._registry
        monkeypatch.setattr(loader, "_PROMPTS_DIR", tmp_path)
        monkeypatch.setattr(loader, "_registry", [
            {"id": "test.prompt", "file": "test.md", "variables": [], "title": "Test", "kind": "prompt"},
        ])

        try:
            write_prompt_file("test.prompt", "新しいテキスト")
            assert test_file.read_text(encoding="utf-8") == "新しいテキスト"
        finally:
            loader._registry = original_registry


class TestTemplateSyntaxValidation:
    """テンプレート構文バリデーションのテスト"""

    def _setup_test_registry(self, tmp_path, monkeypatch, variables=None):
        """テスト用の一時レジストリを設定するヘルパー"""
        if variables is None:
            variables = ["task_url"]
        test_file = tmp_path / "test.md"
        test_file.write_text("{task_url} テスト", encoding="utf-8")

        import hokusai.prompts.loader as loader
        monkeypatch.setattr(loader, "_PROMPTS_DIR", tmp_path)
        monkeypatch.setattr(loader, "_registry", [
            {"id": "test.prompt", "file": "test.md", "variables": variables, "title": "Test", "kind": "prompt"},
        ])
        return test_file

    def test_unbalanced_single_brace_rejected(self, tmp_path, monkeypatch):
        """単一波括弧を含むテンプレートが保存拒否される"""
        self._setup_test_registry(tmp_path, monkeypatch, variables=[])
        # { "a": 1 } は未定義変数として検出される
        with pytest.raises(ValueError):
            write_prompt_file("test.prompt", 'JSON: { "a": 1 }')

    def test_truly_broken_braces_rejected(self, tmp_path, monkeypatch):
        """壊れた波括弧構文が保存拒否される"""
        self._setup_test_registry(tmp_path, monkeypatch, variables=[])
        with pytest.raises(ValueError, match="テンプレート構文エラー"):
            write_prompt_file("test.prompt", "壊れた {テンプレート")

    def test_unknown_placeholder_rejected(self, tmp_path, monkeypatch):
        """未定義のプレースホルダを含むテンプレートが保存拒否される"""
        self._setup_test_registry(tmp_path, monkeypatch, variables=["task_url"])
        with pytest.raises(ValueError, match="未定義の変数"):
            write_prompt_file("test.prompt", "{task_url} と {unknown_var} テスト")

    def test_escaped_braces_allowed(self, tmp_path, monkeypatch):
        """エスケープされた波括弧 {{}} は許可される"""
        self._setup_test_registry(tmp_path, monkeypatch, variables=["task_url"])
        # {{index}} はエスケープされた波括弧なので有効
        write_prompt_file("test.prompt", "{task_url} key={{index}}")
        test_file = tmp_path / "test.md"
        assert "key={{index}}" in test_file.read_text(encoding="utf-8")

    def test_valid_template_passes(self, tmp_path, monkeypatch):
        """正しいテンプレートは検証を通過する"""
        self._setup_test_registry(tmp_path, monkeypatch, variables=["task_url"])
        write_prompt_file("test.prompt", "URL: {task_url} です")
        test_file = tmp_path / "test.md"
        assert test_file.read_text(encoding="utf-8") == "URL: {task_url} です"


class TestRegistryIntegrity:
    """レジストリとファイルの整合性テスト"""

    def test_all_registry_files_exist(self):
        """registry.yaml に定義された全ファイルが実在する"""
        registry = _load_registry()
        for entry in registry:
            path = _PROMPTS_DIR / entry["file"]
            assert path.exists(), f"File not found: {entry['file']} (id: {entry['id']})"

    def test_all_templates_have_required_variables(self):
        """全テンプレートに必須変数プレースホルダが含まれている"""
        import re
        registry = _load_registry()
        for entry in registry:
            path = _PROMPTS_DIR / entry["file"]
            content = path.read_text(encoding="utf-8")
            for var in entry.get("variables", []):
                pattern = r"(?<!\{)\{" + re.escape(var) + r"\}(?!\})"
                assert re.search(pattern, content), (
                    f"Template {entry['id']} is missing variable {{{var}}}"
                )


class TestPhase2Equivalence:
    """差し替え前後の出力互換性テスト"""

    def test_task_research_prompt_equivalent(self):
        """Phase 2 事前調査プロンプトが同等の出力を生成する"""
        from hokusai.nodes.phase2_research import _build_task_research_prompt

        result = _build_task_research_prompt("https://example.com/task/1")
        assert "タスクURL: https://example.com/task/1" in result
        assert "## 調査手順" in result
        assert "## 出力ルール（厳守）" in result
        assert "## 必須セクション" in result

    def test_task_research_retry_prompt_equivalent(self):
        """Phase 2 リトライプロンプトが同等の出力を生成する"""
        from hokusai.nodes.phase2_research import _build_research_retry_prompt

        result = _build_research_retry_prompt(
            task_url="https://example.com/task/1",
            previous_output="前回の出力",
            validation_error="検証エラー",
        )
        assert "検証エラー" in result
        assert "前回の出力" in result
        assert "タスクURL: https://example.com/task/1" in result
        assert "## 絶対厳守ルール" in result


class TestCrossReviewPrompts:
    """Cross Review プロンプトのテスト"""

    def test_cross_review_proxy_get(self):
        """CROSS_REVIEW_PROMPTS プロキシの get が動作する"""
        from hokusai.constants import CROSS_REVIEW_PROMPTS

        result = CROSS_REVIEW_PROMPTS.get(2, "")
        assert "事前調査ドキュメントのレビュアー" in result

    def test_cross_review_proxy_getitem(self):
        """CROSS_REVIEW_PROMPTS プロキシの [] アクセスが動作する"""
        from hokusai.constants import CROSS_REVIEW_PROMPTS

        result = CROSS_REVIEW_PROMPTS[3]
        assert "設計ドキュメントのレビュアー" in result

    def test_cross_review_proxy_contains(self):
        """CROSS_REVIEW_PROMPTS プロキシの in 演算子が動作する"""
        from hokusai.constants import CROSS_REVIEW_PROMPTS

        assert 2 in CROSS_REVIEW_PROMPTS
        assert 3 in CROSS_REVIEW_PROMPTS
        assert 4 in CROSS_REVIEW_PROMPTS
        assert 99 not in CROSS_REVIEW_PROMPTS

    def test_cross_review_proxy_missing_key(self):
        """存在しないフェーズでデフォルト値を返す"""
        from hokusai.constants import CROSS_REVIEW_PROMPTS

        result = CROSS_REVIEW_PROMPTS.get(99, "default")
        assert result == "default"
