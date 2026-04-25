"""
Tests for scripts/dashboard.py module

C-3-4: ダッシュボードのPR表示修正の動作確認テスト
"""

import json
import os
import pytest
from datetime import datetime
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# テスト対象をインポート
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.dashboard import (
    _BG_MAX_AGE_SECONDS,
    _BG_META_DIR,
    _check_command_string,
    _get_running_identifiers,
    _hygiene_action_banner,
    _cross_review_blocked_banner,
    _find_cross_review_blocked_phase,
    _launch_hokusai_background,
    _phase6_settings_link,
    _pid_is_alive,
    _remove_running_meta,
    _resolve_config_path,
    _resolve_hokusai_args,
    _run_hokusai_command,
    _save_running_meta,
    _verify_pid_is_hokusai,
    apply_cross_review_fixes,
    continue_ignoring_cross_review,
    is_workflow_running,
    continue_workflow_auto_mode,
    continue_workflow_step_mode,
    cross_review_badge,
    classify_verification_error,
    phase6_failure_summary,
    render_phase6_failure_panel,
    validate_command_fields,
    verification_badge,
    get_waiting_status,
    get_workflow_detail,
    get_workflows,
    render_detail_page,
    render_rulebook_page,
    render_phase_table,
    render_prs,
    render_workflow_list,
    render_step_controls,
    render_html,
    render_phase_page_links,
    render_prompts_page,
    render_settings_page,
    rerun_cross_review_for_phase,
    submit_phase_decision,
    _get_store,
    DashboardHandler,
    list_config_files,
    load_config_yaml,
    save_config_yaml,
    retry_phase,
    start_workflow_auto_mode,
    start_workflow_step_mode,
    validate_config,
    _notion_status_badge,
    _notion_warning_banner,
    waiting_status_label,
    handle_pr_review_action,
    render_pr_progress,
    CHECKPOINT_DB_PATH,
)
from hokusai.persistence.sqlite_store import SQLiteStore


@pytest.fixture
def temp_db():
    """一時的なテストデータベースを作成"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    # テーブル作成
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE workflows (
                workflow_id TEXT PRIMARY KEY,
                task_url TEXT NOT NULL,
                task_title TEXT,
                branch_name TEXT,
                current_phase INTEGER DEFAULT 1,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()

    yield db_path

    # クリーンアップ
    db_path.unlink(missing_ok=True)


@pytest.fixture
def sample_state_with_prs():
    """PRを含むサンプルステート"""
    return {
        "workflow_id": "wf-test001",
        "task_url": "https://notion.so/test",
        "task_title": "Test Task",
        "branch_name": "feature/test",
        "current_phase": 8,
        "pull_requests": [
            {
                "repo_name": "Backend",
                "title": "feat: Add new feature",
                "url": "https://github.com/test/backend/pull/123",
                "number": 123,
                "owner": "test",
                "repo": "backend",
                "status": "draft",
                "github_status": "open",
                "copilot_review_passed": True,
            },
            {
                "repo_name": "API",
                "title": "feat: API changes",
                "url": "https://github.com/test/api/pull/456",
                "number": 456,
                "owner": "test",
                "repo": "api",
                "status": "draft",
                "github_status": "open",
                "copilot_review_passed": False,
            },
        ],
        "phases": {},
        "verification": {"build": "pass", "test": "pass", "lint": "pass"},
    }


@pytest.fixture
def sample_state_with_legacy_pr():
    """旧フィールドを含むサンプルステート"""
    return {
        "workflow_id": "wf-legacy001",
        "task_url": "https://notion.so/legacy",
        "task_title": "Legacy Task",
        "branch_name": "feature/legacy",
        "current_phase": 8,
        # 旧フィールド（pull_requestsではなく個別フィールド）
        "backend_pr_url": "https://github.com/test/backend/pull/100",
        "backend_pr_number": 100,
        "backend_pr_title": "Legacy PR",
        # pull_requestsは空
        "pull_requests": [],
        "phases": {},
        "verification": {},
    }


class TestRenderPrs:
    """render_prs 関数のテスト"""

    def test_empty_list_returns_message(self):
        """空のリストの場合はメッセージを返す"""
        result = render_prs([])
        assert "PRはありません" in result

    def test_renders_pr_table(self, sample_state_with_prs):
        """PRリストがあればテーブルをレンダリング"""
        prs = sample_state_with_prs["pull_requests"]
        result = render_prs(prs)

        # テーブル構造
        assert "<table>" in result
        assert "</table>" in result
        assert "<thead>" in result
        assert "<tbody>" in result

        # PR情報が含まれる
        assert "#123" in result
        assert "#456" in result
        assert "test/backend" in result
        assert "test/api" in result

    def test_copilot_pass_shows_checkmark(self, sample_state_with_prs):
        """Copilotレビュー通過時はチェックマークを表示"""
        prs = sample_state_with_prs["pull_requests"]
        result = render_prs(prs)

        # 少なくとも1つのPRがcopilot_review_passed=Trueなのでチェックマーク表示
        assert "✅" in result


class TestGetWorkflowDetail:
    """get_workflow_detail 関数のテスト"""

    def test_returns_state_with_prs(self, temp_db, sample_state_with_prs):
        """PRを含む状態を正しく返す"""
        # データ挿入
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                """
                INSERT INTO workflows
                (workflow_id, task_url, task_title, branch_name, current_phase, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample_state_with_prs["workflow_id"],
                    sample_state_with_prs["task_url"],
                    sample_state_with_prs["task_title"],
                    sample_state_with_prs["branch_name"],
                    sample_state_with_prs["current_phase"],
                    json.dumps(sample_state_with_prs),
                    "2026-02-22T12:00:00",
                    "2026-02-22T12:00:00",
                ),
            )
            conn.commit()

        # SQLiteStoreをモック
        store = SQLiteStore(temp_db)
        with patch("scripts.dashboard._get_store", return_value=store):
            result = get_workflow_detail(sample_state_with_prs["workflow_id"])

        assert result is not None
        assert "pull_requests" in result
        assert len(result["pull_requests"]) == 2

    def test_migrates_legacy_pr_fields(self, temp_db, sample_state_with_legacy_pr):
        """旧PRフィールドが新フォーマットに移行される"""
        # データ挿入
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                """
                INSERT INTO workflows
                (workflow_id, task_url, task_title, branch_name, current_phase, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample_state_with_legacy_pr["workflow_id"],
                    sample_state_with_legacy_pr["task_url"],
                    sample_state_with_legacy_pr["task_title"],
                    sample_state_with_legacy_pr["branch_name"],
                    sample_state_with_legacy_pr["current_phase"],
                    json.dumps(sample_state_with_legacy_pr),
                    "2026-02-22T12:00:00",
                    "2026-02-22T12:00:00",
                ),
            )
            conn.commit()

        # SQLiteStoreをモック
        store = SQLiteStore(temp_db)
        with patch("scripts.dashboard._get_store", return_value=store):
            result = get_workflow_detail(sample_state_with_legacy_pr["workflow_id"])

        assert result is not None
        assert "pull_requests" in result
        # 旧フィールドから移行されたPR
        assert len(result["pull_requests"]) == 1
        assert result["pull_requests"][0]["url"] == "https://github.com/test/backend/pull/100"
        assert result["pull_requests"][0]["number"] == 100

    def test_returns_none_for_nonexistent(self, temp_db):
        """存在しないワークフローIDの場合はNoneを返す"""
        store = SQLiteStore(temp_db)
        with patch("scripts.dashboard._get_store", return_value=store):
            result = get_workflow_detail("nonexistent-id")

        assert result is None


class TestGetWorkflows:
    """get_workflows 関数のテスト"""

    def test_returns_workflow_list(self, temp_db, sample_state_with_prs):
        """ワークフロー一覧を返す"""
        # データ挿入
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                """
                INSERT INTO workflows
                (workflow_id, task_url, task_title, branch_name, current_phase, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample_state_with_prs["workflow_id"],
                    sample_state_with_prs["task_url"],
                    sample_state_with_prs["task_title"],
                    sample_state_with_prs["branch_name"],
                    sample_state_with_prs["current_phase"],
                    json.dumps(sample_state_with_prs),
                    "2026-02-22T12:00:00",
                    "2026-02-22T12:00:00",
                ),
            )
            conn.commit()

        # DB_PATHとstoreをモック
        store = SQLiteStore(temp_db)
        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._get_store", return_value=store):
            result = get_workflows()

        assert len(result) == 1
        assert result[0]["workflow_id"] == "wf-test001"
        assert "test/backend" in result[0]["repos"]
        assert "test/api" in result[0]["repos"]

    def test_extracts_repos_from_prs(self, temp_db, sample_state_with_prs):
        """PRからリポジトリ情報を抽出"""
        # データ挿入
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                """
                INSERT INTO workflows
                (workflow_id, task_url, task_title, branch_name, current_phase, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample_state_with_prs["workflow_id"],
                    sample_state_with_prs["task_url"],
                    sample_state_with_prs["task_title"],
                    sample_state_with_prs["branch_name"],
                    sample_state_with_prs["current_phase"],
                    json.dumps(sample_state_with_prs),
                    "2026-02-22T12:00:00",
                    "2026-02-22T12:00:00",
                ),
            )
            conn.commit()

        store = SQLiteStore(temp_db)
        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._get_store", return_value=store):
            result = get_workflows()

        assert len(result[0]["repos"]) == 2
        assert "test/backend" in result[0]["repos"]
        assert "test/api" in result[0]["repos"]


class TestSQLiteStoreMigration:
    """SQLiteStore マイグレーション機能のテスト"""

    @pytest.fixture
    def sample_state_without_multi_repo_fields(self):
        """マルチリポジトリフィールドを含まない旧ステート"""
        return {
            "workflow_id": "wf-old001",
            "task_url": "https://notion.so/old",
            "task_title": "Old Task",
            "branch_name": "feature/old",
            "current_phase": 5,
            "pull_requests": [],
            "phases": {},
            "verification": {},
            # repository_status, verification_errors, repositories が存在しない
        }

    def test_migrates_multi_repo_fields_on_load(self, temp_db, sample_state_without_multi_repo_fields):
        """ロード時にマルチリポジトリフィールドがマイグレーションされる

        Issue #2 修正: repository_status/verification_errors/repositories の欠損補完
        """
        state = sample_state_without_multi_repo_fields
        # 旧ステートにフィールドがないことを確認
        assert "repository_status" not in state
        assert "verification_errors" not in state
        assert "repositories" not in state

        # データ挿入
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                """
                INSERT INTO workflows
                (workflow_id, task_url, task_title, branch_name, current_phase, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state["workflow_id"],
                    state["task_url"],
                    state["task_title"],
                    state["branch_name"],
                    state["current_phase"],
                    json.dumps(state),
                    "2026-02-22T12:00:00",
                    "2026-02-22T12:00:00",
                ),
            )
            conn.commit()

        # SQLiteStoreでロード
        store = SQLiteStore(temp_db)
        result = store.load_workflow(state["workflow_id"])

        # マイグレーションされたフィールドが存在する
        assert result is not None
        assert "repository_status" in result
        assert result["repository_status"] == {}
        assert "verification_errors" in result
        assert result["verification_errors"] == []
        assert "repositories" in result
        assert result["repositories"] == []

    def test_preserves_existing_multi_repo_fields(self, temp_db, sample_state_with_prs):
        """既存のマルチリポジトリフィールドは保持される"""
        state = sample_state_with_prs
        # 既存フィールドを追加
        state["repository_status"] = {"Backend": "completed", "API": "failed"}
        state["verification_errors"] = [
            {"repository": "API", "command": "test", "success": False, "error_output": "Test failed"}
        ]
        state["repositories"] = []

        # データ挿入
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                """
                INSERT INTO workflows
                (workflow_id, task_url, task_title, branch_name, current_phase, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state["workflow_id"],
                    state["task_url"],
                    state["task_title"],
                    state["branch_name"],
                    state["current_phase"],
                    json.dumps(state),
                    "2026-02-22T12:00:00",
                    "2026-02-22T12:00:00",
                ),
            )
            conn.commit()

        # SQLiteStoreでロード
        store = SQLiteStore(temp_db)
        result = store.load_workflow(state["workflow_id"])

        # 既存のフィールドが保持されている
        assert result is not None
        assert result["repository_status"] == {"Backend": "completed", "API": "failed"}
        assert len(result["verification_errors"]) == 1
        assert result["verification_errors"][0]["repository"] == "API"

    def test_find_workflow_by_task_url_also_migrates(self, temp_db, sample_state_without_multi_repo_fields):
        """find_workflow_by_task_url でもマイグレーションが実行される"""
        state = sample_state_without_multi_repo_fields

        # データ挿入
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                """
                INSERT INTO workflows
                (workflow_id, task_url, task_title, branch_name, current_phase, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state["workflow_id"],
                    state["task_url"],
                    state["task_title"],
                    state["branch_name"],
                    state["current_phase"],
                    json.dumps(state),
                    "2026-02-22T12:00:00",
                    "2026-02-22T12:00:00",
                ),
            )
            conn.commit()

        # find_workflow_by_task_url でロード
        store = SQLiteStore(temp_db)
        result = store.find_workflow_by_task_url(state["task_url"])

        # マイグレーションされている
        assert result is not None
        assert "repository_status" in result
        assert "verification_errors" in result
        assert "repositories" in result


class TestSettingsConfigIO:
    """Settingsページの設定I/Oテスト"""

    def test_load_config_yaml_reads_example_file(self):
        """既存設定ファイルとの互換確認（example-github-issue.yaml）"""
        data = load_config_yaml("example-github-issue")
        assert isinstance(data, dict)
        assert "project_root" in data
        assert "base_branch" in data
        assert "task_backend" in data
        assert "git_hosting" in data

    def test_list_config_files_includes_examples(self):
        """設定ファイル一覧にexample設定が含まれる"""
        configs = list_config_files()
        assert "example-github-issue" in configs
        assert "example-gitlab" in configs

    def test_save_config_yaml_creates_backup_and_persists(self, tmp_path):
        """保存時に .bak バックアップを作成し、内容を更新する"""
        config_dir = tmp_path / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "demo.yaml"
        config_path.write_text(
            "project_root: /tmp\nbase_branch: main\n",
            encoding="utf-8",
        )

        new_data = {
            "project_root": str(tmp_path),
            "base_branch": "develop",
            "max_retry_count": 3,
        }

        with patch("scripts.dashboard.CONFIGS_DIR", config_dir):
            ok = save_config_yaml("demo", new_data)
            assert ok is True
            loaded = load_config_yaml("demo")

        assert loaded["base_branch"] == "develop"
        backup_path = config_dir / "demo.yaml.bak"
        assert backup_path.exists()
        backup_text = backup_path.read_text(encoding="utf-8")
        assert "base_branch: main" in backup_text


class TestSettingsValidation:
    """Settingsページのバリデーションテスト"""

    def test_validate_config_success(self, tmp_path):
        """必要項目・型・パスが正しい場合は検証成功"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)

        data = {
            "project_root": str(tmp_path),
            "base_branch": "main",
            "max_retry_count": 3,
            "retry_delay_seconds": 5,
            "skill_timeout": 600,
            "command_timeout": 300,
            "repositories": [
                {
                    "name": "Backend",
                    "path": str(repo_dir),
                    "base_branch": "main",
                    "default_target": True,
                }
            ],
        }

        valid, errors, _warnings = validate_config(data)
        assert valid is True
        assert errors == []

    def test_validate_config_fails_required_fields(self):
        """必須項目欠落時は検証失敗"""
        valid, errors, _ = validate_config({})
        assert valid is False
        assert any("project_root" in e for e in errors)
        assert any("base_branch" in e for e in errors)

    def test_validate_config_fails_invalid_numeric_fields(self, tmp_path):
        """数値項目の型不正を検出する"""
        data = {
            "project_root": str(tmp_path),
            "base_branch": "main",
            "max_retry_count": "three",
            "retry_delay_seconds": "five",
            "skill_timeout": "slow",
            "command_timeout": "long",
        }
        valid, errors, _ = validate_config(data)
        assert valid is False
        assert any("max_retry_count" in e for e in errors)
        assert any("retry_delay_seconds" in e for e in errors)
        assert any("skill_timeout" in e for e in errors)
        assert any("command_timeout" in e for e in errors)

    def test_validate_config_fails_invalid_paths(self, tmp_path):
        """存在しない project_root / repositories[].path を検出する"""
        missing_path = tmp_path / "missing"
        data = {
            "project_root": str(missing_path),
            "base_branch": "main",
            "repositories": [
                {"name": "API", "path": str(missing_path / "repo"), "base_branch": "main"}
            ],
        }
        valid, errors, _ = validate_config(data)
        assert valid is False
        assert any("project_root のパスが存在しません" in e for e in errors)
        assert any("repositories[0].path のパスが存在しません" in e for e in errors)


class TestSettingsPageRendering:
    """Settings UIの退行防止テスト（HTML/JS契約）"""

    def test_settings_page_includes_repository_editor(self):
        """repositories[] 編集UIが表示される"""
        html = render_settings_page(["my-project"])
        assert "repositoriesContainer" in html
        assert "addRepositoryRow" in html
        assert "collectRepositoriesData" in html

    def test_settings_page_includes_yaml_mode_and_validation(self):
        """YAMLモード切替と構文検証UIが存在する"""
        html = render_settings_page(["my-project"])
        assert "switchEditorMode('yaml')" in html
        assert "validateYaml()" in html
        assert "YAML構文エラー" in html
        assert "saveYamlConfig" in html

    def test_settings_page_includes_validate_and_retry_buttons(self):
        """検証のみ・保存してPhase6再実行ボタンが存在する"""
        html = render_settings_page(["my-project"])
        assert "validateConfigOnly()" in html
        assert "saveAndRetryPhase6()" in html
        assert "btnSaveAndRetryPhase6" in html

    def test_settings_page_includes_local_storage_controls(self):
        """ダッシュボード固有設定のコントロールが存在する"""
        html = render_settings_page(["my-project"])
        assert "theme" in html
        assert "listLimit" in html
        assert "saveDashboardSettings" in html

    def test_render_html_includes_theme_and_responsive_behaviors(self):
        """テーマ切替・レスポンシブ用のJS/CSSが含まれる"""
        html = render_html("<p>dummy</p>")
        assert "applyTheme(" in html
        assert "matchMedia('(prefers-color-scheme: dark)')" in html
        assert "@media (max-width: 768px)" in html

    def test_render_detail_and_rulebook_pages(self):
        """既存ページ（詳細/rulebook）がレンダリングできる"""
        state = {
            "workflow_id": "wf-1",
            "task_title": "Task",
            "task_url": "https://example.com",
            "branch_name": "feature/x",
            "base_branch": "main",
            "current_phase": 1,
            "verification": {},
            "phases": {},
            "audit_log": [],
            "pull_requests": [],
            "final_review_rules": {},
            "final_review_by_repo": {},
        }
        detail = render_detail_page(state)
        rulebook = render_rulebook_page("my-project")
        assert "フェーズ進捗" in detail
        assert "Pull Requests" in detail
        assert "レビュールールブック" in rulebook


class TestCrossReviewSettings:
    """クロスLLMレビュー設定UIのテスト"""

    def test_settings_page_includes_cross_review_fields(self):
        """クロスLLMレビュー設定フィールドが表示される"""
        html = render_settings_page(["my-project"])
        assert "cfg_cross_review_enabled" in html
        assert "cfg_cross_review_model" in html
        assert "cfg_cross_review_phase" in html
        assert "cfg_cross_review_timeout" in html
        assert "cfg_cross_review_on_failure" in html
        assert "cfg_cross_review_max_rounds" in html
        assert "クロスLLMレビュー" in html

    def test_validate_cross_review_valid(self, tmp_path):
        """有効なcross_review設定は検証成功"""
        data = {
            "project_root": str(tmp_path),
            "base_branch": "main",
            "cross_review": {
                "enabled": True,
                "model": "codex-mini-latest",
                "phases": [2, 4],
                "timeout": 300,
                "on_failure": "warn",
                "max_correction_rounds": 2,
            },
        }
        valid, errors, _ = validate_config(data)
        assert valid is True

    def test_validate_cross_review_invalid_on_failure(self, tmp_path):
        """不正なon_failure値を検出"""
        data = {
            "project_root": str(tmp_path),
            "base_branch": "main",
            "cross_review": {"on_failure": "invalid"},
        }
        valid, errors, _ = validate_config(data)
        assert valid is False
        assert any("on_failure" in e for e in errors)

    def test_validate_cross_review_invalid_phases(self, tmp_path):
        """不正なphases値を検出"""
        data = {
            "project_root": str(tmp_path),
            "base_branch": "main",
            "cross_review": {"phases": [0, 99]},
        }
        valid, errors, _ = validate_config(data)
        assert valid is False
        assert any("phases" in e for e in errors)

    def test_validate_cross_review_invalid_max_rounds(self, tmp_path):
        """不正なmax_correction_rounds値を検出"""
        data = {
            "project_root": str(tmp_path),
            "base_branch": "main",
            "cross_review": {"max_correction_rounds": 0},
        }
        valid, errors, _ = validate_config(data)
        assert valid is False
        assert any("max_correction_rounds" in e for e in errors)

    def test_populate_and_collect_cross_review_in_form(self):
        """フォームのpopulate/collectにcross_reviewが含まれる"""
        html = render_settings_page(["my-project"])
        # populateForm内でcross_reviewが処理される
        assert "crossReview.enabled" in html or "cross_review" in html
        # getFormData内でcross_reviewオブジェクトが構築される
        assert "data.cross_review" in html

    def test_model_is_select_with_three_options(self):
        """モデル選択がセレクトボックスで3つの選択肢を持つ"""
        html = render_settings_page(["my-project"])
        assert 'select id="cfg_cross_review_model"' in html
        assert "codex-mini-latest" in html
        assert "claude-code" in html
        assert "gemini-cli" in html

    def test_validate_cross_review_invalid_model(self, tmp_path):
        """不正なmodel値を検出"""
        data = {
            "project_root": str(tmp_path),
            "base_branch": "main",
            "cross_review": {"model": "invalid-model"},
        }
        valid, errors, _ = validate_config(data)
        assert valid is False
        assert any("model" in e for e in errors)

    def test_validate_cross_review_valid_models(self, tmp_path):
        """有効なmodel値はすべて検証成功"""
        for model in ("codex-mini-latest", "claude-code", "gemini-cli"):
            data = {
                "project_root": str(tmp_path),
                "base_branch": "main",
                "cross_review": {"model": model},
            }
            valid, errors, _ = validate_config(data)
            assert valid is True, f"model={model} should be valid, errors={errors}"


class TestStepModeActions:
    """stepモード実行ヘルパーのテスト"""

    def test_resolve_config_path(self):
        assert _resolve_config_path("example-github-issue") is not None
        assert _resolve_config_path("not-found-config") is None

    def test_run_hokusai_command_success(self):
        completed = subprocess.CompletedProcess(
            args=["hokusai"],
            returncode=0,
            stdout="ok",
            stderr="",
        )
        with patch("scripts.dashboard.subprocess.run", return_value=completed) as mock_run:
            result = _run_hokusai_command(["hokusai", "list"])
        assert result["success"] is True
        assert result["stdout"] == "ok"
        # stdin=DEVNULL が渡されていることを確認
        assert mock_run.call_args[1]["stdin"] == subprocess.DEVNULL

    def test_run_hokusai_command_fallback_to_uv(self):
        completed = subprocess.CompletedProcess(
            args=["uv", "run", "hokusai"],
            returncode=0,
            stdout="ok",
            stderr="",
        )
        with patch("scripts.dashboard.shutil.which") as mock_which, \
             patch("scripts.dashboard.subprocess.run", return_value=completed) as mock_run:
            mock_which.side_effect = lambda name: None if name == "hokusai" else "/opt/homebrew/bin/uv"
            result = _run_hokusai_command(["hokusai", "list"])

        assert result["success"] is True
        called_args = mock_run.call_args[0][0]
        assert called_args[:3] == ["uv", "run", "hokusai"]
        assert called_args[3:] == ["list"]

    def test_start_workflow_step_mode_success(self):
        bg_result = {"launched": True, "pid": 12345, "log_file": "/tmp/test.log"}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result) as mock_launch:
            result = start_workflow_step_mode("https://notion.so/task", "example-github-issue")

        assert result["success"] is True
        assert result["pid"] == 12345
        assert result["workflow_id"].startswith("wf-")
        # identifier が start:{workflow_id} 形式であること
        call_args = mock_launch.call_args
        assert call_args[0][1] == f"start:{result['workflow_id']}"
        # HOKUSAI_WORKFLOW_ID が env_extra に含まれること
        assert call_args[1]["env_extra"]["HOKUSAI_WORKFLOW_ID"] == result["workflow_id"]
        # HOKUSAI_CONFIG_NAME が env_extra に含まれること
        assert call_args[1]["env_extra"]["HOKUSAI_CONFIG_NAME"] == "example-github-issue"

    def test_start_workflow_step_mode_no_config_name(self):
        bg_result = {"launched": True, "pid": 12345, "log_file": "/tmp/test.log"}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result) as mock_launch:
            result = start_workflow_step_mode("https://notion.so/task")

        assert result["success"] is True
        # config_name が None の場合は HOKUSAI_CONFIG_NAME が含まれない
        assert "HOKUSAI_CONFIG_NAME" not in mock_launch.call_args[1]["env_extra"]

    def test_start_workflow_step_mode_config_not_found(self):
        result = start_workflow_step_mode("https://notion.so/task", "unknown-config")
        assert result["success"] is False
        assert any("設定ファイルが見つかりません" in e for e in result["errors"])

    def test_continue_workflow_step_mode_success(self):
        bg_result = {"launched": True, "pid": 12346, "log_file": "/tmp/test.log"}
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = {"config_name": None}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result), \
             patch("scripts.dashboard._get_store", return_value=mock_store):
            result = continue_workflow_step_mode("wf-1")

        assert result["success"] is True
        assert result["workflow_id"] == "wf-1"
        assert result["pid"] == 12346

    def test_continue_workflow_step_mode_with_config(self):
        """state に config_name がある場合、continue に -c フラグが付く"""
        bg_result = {"launched": True, "pid": 12346, "log_file": "/tmp/test.log"}
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = {"config_name": "example-github-issue"}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result) as mock_launch, \
             patch("scripts.dashboard._get_store", return_value=mock_store):
            result = continue_workflow_step_mode("wf-1")

        assert result["success"] is True
        # コマンドに -c フラグが含まれること
        cmd = mock_launch.call_args[0][0]
        assert "-c" in cmd
        # env_extra に HOKUSAI_CONFIG_NAME が含まれること
        env_extra = mock_launch.call_args[1].get("env_extra") or {}
        assert env_extra.get("HOKUSAI_CONFIG_NAME") == "example-github-issue"

    def test_continue_workflow_step_mode_no_config(self):
        """state に config_name がない場合、-c フラグが付かない"""
        bg_result = {"launched": True, "pid": 12346, "log_file": "/tmp/test.log"}
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = {"config_name": None}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result) as mock_launch, \
             patch("scripts.dashboard._get_store", return_value=mock_store):
            result = continue_workflow_step_mode("wf-1")

        assert result["success"] is True
        cmd = mock_launch.call_args[0][0]
        assert "-c" not in cmd

    def test_start_workflow_auto_mode_success(self):
        bg_result = {"launched": True, "pid": 12347, "log_file": "/tmp/test.log"}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result) as mock_launch:
            result = start_workflow_auto_mode("https://notion.so/task", "example-github-issue")

        assert result["success"] is True
        assert result["pid"] == 12347
        assert result["workflow_id"].startswith("wf-")
        # identifier が start:{workflow_id} 形式であること
        call_args = mock_launch.call_args
        assert call_args[0][1] == f"start:{result['workflow_id']}"
        assert call_args[1]["env_extra"]["HOKUSAI_WORKFLOW_ID"] == result["workflow_id"]
        # HOKUSAI_CONFIG_NAME が env_extra に含まれること
        assert call_args[1]["env_extra"]["HOKUSAI_CONFIG_NAME"] == "example-github-issue"

    def test_continue_workflow_auto_mode_success(self):
        bg_result = {"launched": True, "pid": 12348, "log_file": "/tmp/test.log"}
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = {"config_name": None}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result), \
             patch("scripts.dashboard._get_store", return_value=mock_store):
            result = continue_workflow_auto_mode("wf-2")

        assert result["success"] is True
        assert result["workflow_id"] == "wf-2"
        assert result["pid"] == 12348

    def test_continue_workflow_auto_mode_with_config(self):
        """auto mode の continue も state から config_name を読み取る"""
        bg_result = {"launched": True, "pid": 12348, "log_file": "/tmp/test.log"}
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = {"config_name": "example-github-issue"}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result) as mock_launch, \
             patch("scripts.dashboard._get_store", return_value=mock_store):
            result = continue_workflow_auto_mode("wf-2")

        assert result["success"] is True
        cmd = mock_launch.call_args[0][0]
        assert "-c" in cmd
        env_extra = mock_launch.call_args[1].get("env_extra") or {}
        assert env_extra.get("HOKUSAI_CONFIG_NAME") == "example-github-issue"

    def test_start_workflow_step_mode_already_running(self):
        bg_result = {"launched": False, "error": "このワークフローは既に実行中です (PID: 999)"}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result):
            result = start_workflow_step_mode("https://notion.so/task")

        assert result["success"] is False
        assert any("既に実行中" in e for e in result["errors"])

    def test_continue_workflow_step_mode_already_running(self):
        bg_result = {"launched": False, "error": "このワークフローは既に実行中です (PID: 999)"}
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = {"config_name": None}
        with patch("scripts.dashboard._launch_hokusai_background", return_value=bg_result), \
             patch("scripts.dashboard._get_store", return_value=mock_store):
            result = continue_workflow_step_mode("wf-1")

        assert result["success"] is False
        assert any("既に実行中" in e for e in result["errors"])


class TestResolveHokusaiArgs:
    """_resolve_hokusai_args ヘルパーのテスト"""

    def test_no_fallback_when_hokusai_available(self):
        with patch("scripts.dashboard.shutil.which", return_value="/usr/local/bin/hokusai"):
            result = _resolve_hokusai_args(["hokusai", "start", "url"])
        assert result == ["hokusai", "start", "url"]

    def test_fallback_to_uv(self):
        with patch("scripts.dashboard.shutil.which") as mock_which:
            mock_which.side_effect = lambda name: None if name == "hokusai" else "/opt/homebrew/bin/uv"
            result = _resolve_hokusai_args(["hokusai", "start", "url"])
        assert result == ["uv", "run", "hokusai", "start", "url"]

    def test_no_fallback_for_non_hokusai_command(self):
        result = _resolve_hokusai_args(["python", "-m", "hokusai"])
        assert result == ["python", "-m", "hokusai"]


class TestLaunchHokusaiBackground:
    """_launch_hokusai_background のテスト"""

    def test_launches_process_successfully(self):
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None  # running
        mock_proc.wait.return_value = 0

        with patch("scripts.dashboard.subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("scripts.dashboard.shutil.which", return_value="/usr/local/bin/hokusai"), \
             patch("scripts.dashboard._bg_processes", {}), \
             patch("builtins.open", MagicMock()):
            result = _launch_hokusai_background(["hokusai", "start", "url"], "test-id")

        assert result["launched"] is True
        assert result["pid"] == 42
        assert "log_file" in result
        # Popen に stdin=DEVNULL と start_new_session=True が渡されている
        popen_kwargs = mock_popen.call_args[1]
        assert popen_kwargs["stdin"] == subprocess.DEVNULL
        assert popen_kwargs["start_new_session"] is True

    def test_prevents_duplicate_launch(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.pid = 999

        with patch("scripts.dashboard._bg_processes", {"dup-id": mock_proc}):
            result = _launch_hokusai_background(["hokusai", "start", "url"], "dup-id")

        assert result["launched"] is False
        assert "既に実行中" in result["error"]

    def test_allows_relaunch_after_process_completed(self):
        finished_proc = MagicMock()
        finished_proc.poll.return_value = 0  # completed

        new_proc = MagicMock()
        new_proc.pid = 100
        new_proc.wait.return_value = 0

        with patch("scripts.dashboard._bg_processes", {"done-id": finished_proc}), \
             patch("scripts.dashboard.subprocess.Popen", return_value=new_proc), \
             patch("scripts.dashboard.shutil.which", return_value="/usr/local/bin/hokusai"), \
             patch("builtins.open", MagicMock()):
            result = _launch_hokusai_background(["hokusai", "start", "url"], "done-id")

        assert result["launched"] is True
        assert result["pid"] == 100

    def test_handles_file_not_found(self):
        with patch("scripts.dashboard.subprocess.Popen", side_effect=FileNotFoundError), \
             patch("scripts.dashboard.shutil.which", return_value=None), \
             patch("scripts.dashboard._bg_processes", {}), \
             patch("builtins.open", MagicMock()):
            result = _launch_hokusai_background(["hokusai", "start", "url"], "fnf-id")

        assert result["launched"] is False
        assert "見つかりません" in result["error"]


class TestCrossReviewBadge:
    """クロスLLMレビューバッジ表示のテスト"""

    def test_badge_approve(self):
        """approve結果は緑バッジ"""
        results = {2: {"overall_assessment": "approve", "findings": []}}
        html = cross_review_badge(2, results)
        assert "approve" in html
        assert "#dcfce7" in html  # 緑背景

    def test_badge_request_changes_with_findings(self):
        """request_changes結果は黄色バッジ＋件数表示"""
        results = {4: {
            "overall_assessment": "request_changes",
            "findings": [
                {"severity": "critical", "title": "A"},
                {"severity": "warning", "title": "B"},
            ],
        }}
        html = cross_review_badge(4, results)
        assert "changes" in html
        assert "#fef3c7" in html  # 黄色背景
        assert "2件" in html
        assert "critical 1" in html

    def test_badge_needs_discussion(self):
        """needs_discussion結果は青バッジ"""
        results = {3: {"overall_assessment": "needs_discussion", "findings": []}}
        html = cross_review_badge(3, results)
        assert "discuss" in html
        assert "#dbeafe" in html  # 青背景

    def test_badge_no_result(self):
        """結果なしは「-」表示"""
        html = cross_review_badge(2, {})
        assert "-" in html
        assert "text-muted" in html

    def test_badge_string_key(self):
        """文字列キーでも取得できる"""
        results = {"2": {"overall_assessment": "approve", "findings": []}}
        html = cross_review_badge(2, results)
        assert "approve" in html

    def test_phase_table_shows_review_column_for_phase_2_3_4(self):
        """フェーズテーブルのPhase 2/3/4にLLMレビュー列が表示される"""
        phases = {str(i): {"status": "completed", "retry_count": 0} for i in range(1, 11)}
        state = {
            "current_phase": 5,
            "cross_review_results": {
                2: {"overall_assessment": "approve", "findings": []},
                4: {"overall_assessment": "request_changes", "findings": [
                    {"severity": "warning", "title": "X"},
                ]},
            },
        }
        html = render_phase_table(phases, state)
        # Phase 2 に approve バッジが表示される
        assert "approve" in html
        # Phase 4 に changes バッジが表示される
        assert "changes" in html
        assert "1件" in html

    def test_phase_table_no_review_for_other_phases(self):
        """Phase 1/5/7-9にはレビュー/検証列が空"""
        phases = {str(i): {"status": "completed", "retry_count": 0} for i in range(1, 11)}
        state = {"current_phase": 9, "cross_review_results": {}}
        html = render_phase_table(phases, state)
        # Phase 2,3,4 (cross-review なし) + Phase 6 (verification なし) で text-muted
        assert html.count("text-muted") >= 4

    def test_phase_table_shows_verification_in_phase6(self):
        """Phase 6 行に検証バッジが表示される"""
        phases = {str(i): {"status": "completed", "retry_count": 0} for i in range(1, 11)}
        state = {
            "current_phase": 9,
            "cross_review_results": {},
            "verification": {"build": "pass", "test": "pass", "lint": "pass"},
        }
        html = render_phase_table(phases, state)
        assert "build" in html
        assert "result-ok" in html


class TestVerificationBadge:
    """Phase 6 検証バッジのテスト"""

    def test_not_run(self):
        """未実行時は '-' を返す"""
        state = {"verification": {"build": "not_run", "test": "not_run", "lint": "not_run"}}
        assert verification_badge(state) == '<span class="text-muted">-</span>'

    def test_empty_verification(self):
        """verification が空の場合も '-' を返す"""
        assert verification_badge({"verification": {}}) == '<span class="text-muted">-</span>'
        assert verification_badge({}) == '<span class="text-muted">-</span>'

    def test_all_pass(self):
        """全項目 pass でバッジが表示される"""
        state = {"verification": {"build": "pass", "test": "pass", "lint": "pass"}}
        html = verification_badge(state)
        assert "result-ok" in html
        assert "build" in html
        assert "test" in html
        assert "lint" in html

    def test_with_failure(self):
        """失敗項目は result-ng クラスになる"""
        state = {"verification": {"build": "pass", "test": "fail", "lint": "pass"}}
        html = verification_badge(state)
        assert "result-ng" in html
        assert "result-ok" in html


class TestDetailPageVerificationRemoval:
    """詳細ページから検証結果カードが削除されていることのテスト"""

    def _make_state(self):
        return {
            "workflow_id": "wf-test",
            "task_title": "Test Task",
            "task_url": "https://notion.so/test",
            "branch_name": "feature/test",
            "base_branch": "main",
            "current_phase": 7,
            "phases": {i: {"status": "completed", "retry_count": 0} for i in range(1, 11)},
            "audit_log": [],
            "pull_requests": [],
            "final_review_rules": {},
            "final_review_by_repo": {},
            "verification": {"build": "pass", "test": "pass", "lint": "pass"},
            "cross_review_results": {},
            "waiting_for_human": False,
            "human_input_request": "",
            "phase_subpages": {},
            "phase_page_decision": {},
            "phase_page_recommended_action": {},
        }

    def test_no_verification_card(self):
        """詳細ページに検証結果カードが表示されない"""
        html = render_detail_page(self._make_state())
        assert "検証結果" not in html

    def test_no_detail_grid(self):
        """detail-grid ラッパーが存在しない"""
        html = render_detail_page(self._make_state())
        assert "detail-grid" not in html

    def test_header_review_verification(self):
        """テーブルヘッダーが 'レビュー / 検証' になっている"""
        html = render_detail_page(self._make_state())
        assert "レビュー / 検証" in html


class TestPhaseActionButtonVisibility:
    """フェーズ実行ボタンの表示条件テスト"""

    def test_shows_disabled_buttons_for_1_to_7_and_enabled_for_current(self):
        phases = {
            "1": {"status": "completed", "retry_count": 0},
            "2": {"status": "completed", "retry_count": 0},
            "3": {"status": "completed", "retry_count": 0},
            "4": {"status": "completed", "retry_count": 0},
            "5": {"status": "completed", "retry_count": 0},
            "6": {"status": "completed", "retry_count": 0},
            "7": {"status": "completed", "retry_count": 0},
            "8": {"status": "in_progress", "retry_count": 0},
            "9": {"status": "pending", "retry_count": 0},
            "10": {"status": "pending", "retry_count": 0},
        }
        state = {"current_phase": 8, "waiting_for_human": True}
        html = render_phase_table(phases, state, workflow_id="wf-123")
        # Phase 1〜9 は表示（10は非表示）
        assert html.count("このフェーズを実行") == 9
        # disabled 付きボタンは 1〜7 の7つ + Phase 9 の1つ = 8つ
        assert html.count("phase-run-btn\" disabled") == 8
        # 現在フェーズ(8)は有効
        assert "continueWorkflowStep('wf-123')" in html
        # Phase 10 は current_phase=10 まで非表示
        assert html.count("phase-run-btn") == 9

    def test_enables_next_phase_button_when_current_is_completed(self):
        """current_phase が completed の場合、次フェーズのボタンも有効になる"""
        phases = {
            "1": {"status": "completed", "retry_count": 0},
            "2": {"status": "pending", "retry_count": 0},
            "3": {"status": "pending", "retry_count": 0},
        }
        state = {"current_phase": 1}
        html = render_phase_table(phases, state, workflow_id="wf-next")
        # 9ボタン中8つがdisabled（Phase 1=completed, Phase 3-9=pending but not next）
        # Phase 2 のみが enabled（next_phase かつ pending）
        assert html.count('phase-run-btn" disabled') == 8
        # Phase 2 のボタンが enabled（disabled なしの phase-run-btn）
        assert 'phase-run-btn"  onclick' in html


class TestExecutionControlUI:
    """実行コントロールUIの表示テスト"""

    def test_dashboard_shows_execution_mode_selector(self):
        html = render_step_controls(["example-github-issue"])
        assert "executionMode" in html
        assert "ステップ実行（hokusai --step）" in html
        assert "通常自動実行（hokusai）" in html
        assert "workflow_id（継続用）" not in html
        assert "continueWorkflowFromControl" not in html

    def test_dashboard_has_auto_api_endpoints_in_js(self):
        html = render_html("<div>test</div>")
        assert "/api/workflow/start-auto" in html
        assert "/api/workflow/continue-auto" in html

    def test_dashboard_has_background_execution_alert(self):
        html = render_html("<div>test</div>")
        assert "__workflowActionPending" in html
        assert "バックグラウンドで" in html
        assert "window.alert(" in html

    def test_dashboard_escapes_newline_in_js_string_literals(self):
        html = render_html("<div>test</div>")
        assert "result.errors.join('\\n')" in html


class TestWorkflowListActions:
    """一覧行アクションボタンの表示テスト"""

    def test_render_workflow_list_shows_continue_buttons(self):
        workflows = [{
            "workflow_id": "wf-abc12345",
            "task_title": "Task A",
            "current_phase": 4,
            "updated_at": "2026-03-01T10:00:00",
            "repos": ["org/repo"],
            "waiting_status": "waiting",
            "run_mode": "step",
            "current_phase_status": "in_progress",
        }]
        html = render_workflow_list(workflows)
        assert "継続" in html
        assert "STEP mode" in html
        assert "continueWorkflowByMode" in html
        assert "disabled" not in html

    def test_render_workflow_list_disables_buttons_for_completed_workflow(self):
        workflows = [{
            "workflow_id": "wf-finished",
            "task_title": "Done",
            "current_phase": 9,
            "updated_at": "2026-03-01T10:00:00",
            "repos": [],
            "waiting_status": None,
            "run_mode": "auto",
            "current_phase_status": "completed",
        }]
        html = render_workflow_list(workflows)
        assert "継続" in html
        assert "AUTO mode" in html
        assert html.count("disabled") >= 1


class TestDeleteWorkflow:
    """ワークフロー削除機能のテスト"""

    def test_detail_page_has_delete_button(self):
        """詳細ページに削除ボタンが表示される"""
        state = {
            "workflow_id": "wf-del-test",
            "task_url": "https://notion.so/test",
            "current_phase": 3,
            "waiting_for_human": False,
            "phases": {},
            "audit_log": [],
            "pull_requests": [],
            "cross_review_results": {},
        }
        html = render_detail_page(state)
        assert "deleteWorkflow" in html
        assert "delete-btn" in html
        assert "このワークフローを削除" in html

    def test_delete_button_contains_workflow_id(self):
        """削除ボタンのonclickに正しいworkflow_idが含まれる"""
        state = {
            "workflow_id": "wf-abc999",
            "task_url": "https://notion.so/test",
            "current_phase": 1,
            "waiting_for_human": False,
            "phases": {},
            "audit_log": [],
            "pull_requests": [],
            "cross_review_results": {},
        }
        html = render_detail_page(state)
        assert "deleteWorkflow('wf-abc999')" in html

    def test_delete_button_disabled_while_bg_running(self):
        """バックグラウンド実行中は削除ボタンが disabled になる"""
        state = {
            "workflow_id": "wf-running",
            "task_url": "https://notion.so/test",
            "current_phase": 4,
            "waiting_for_human": False,
            "phases": {},
            "audit_log": [],
            "pull_requests": [],
            "cross_review_results": {},
        }
        html = render_detail_page(state, bg_running=True)
        assert 'class="delete-btn" disabled' in html

    def test_delete_js_function_in_html(self):
        """削除用のJS関数がHTMLに含まれる"""
        html = render_html("<div>test</div>")
        assert "async function deleteWorkflow" in html
        assert "/api/workflow/delete" in html

    def test_render_html_includes_banner_restore_script(self):
        """実行開始バナーの sessionStorage 復元ロジックが含まれる"""
        html = render_html("<div>test</div>")
        assert "sessionStorage.setItem('_hokusai_banner'" in html
        assert "sessionStorage.getItem('_hokusai_banner')" in html
        assert "sessionStorage.removeItem('_hokusai_banner')" in html
        assert "data-bg-running" in html


class TestPhasePageDashboard:
    """Phase 2-4 ページリンクと human decision UI のテスト"""

    def test_render_phase_page_links_shows_subpage_and_action_buttons(self):
        state = {
            "workflow_id": "wf-phase-pages",
            "current_phase": 3,
            "waiting_for_human": True,
            "human_input_request": "Codexクロスレビューでcritical指摘があります",
            "phase_subpages": {2: "https://notion.so/p2", 3: "https://notion.so/p3"},
            "phase_page_decision": {3: "request_changes"},
            "phase_page_recommended_action": {2: "approve_and_move_next", 3: "request_changes"},
            "phase_page_last_human_note_at": {},
            "cross_review_results": {
                3: {"overall_assessment": "request_changes", "findings": [{"severity": "critical"}]},
            },
            "audit_log": [
                {"phase": 3, "action": "cross_review_blocked", "timestamp": "2026-03-09T10:00:00+09:00"},
            ],
            "phases": {
                2: {"status": "completed"},
                3: {"status": "failed", "error_message": "cross_review_blocked"},
                4: {"status": "pending"},
            },
        }

        html = render_phase_page_links(state, "wf-phase-pages")
        assert "https://notion.so/p2" in html
        assert "https://notion.so/p3" in html
        assert "applyPhaseDecision('wf-phase-pages', 3, 'request_changes')" in html
        assert "applyPhaseDecision('wf-phase-pages', 3, 'approve_and_move_next')" in html

    @patch("hokusai.utils.notion_helpers.sync_phase_page_from_state", return_value=True)
    @patch("scripts.dashboard.is_workflow_running", return_value=False)
    @patch("scripts.dashboard._get_store")
    def test_submit_phase_decision_approve_updates_state(
        self, mock_store_fn, mock_running, mock_sync,
    ):
        state = {
            "workflow_id": "wf-phase-pages",
            "current_phase": 3,
            "waiting_for_human": True,
            "human_input_request": "cross_review_blocked",
            "phase_page_decision": {},
            "phase_page_last_human_note_at": {},
            "phase_page_recommended_action": {},
            "phase_subpages": {3: "https://notion.so/p3"},
            "phases": {3: {"status": "failed", "error_message": "blocked", "retry_count": 0}},
            "audit_log": [],
            "updated_at": "2026-03-09T10:00:00+09:00",
        }
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = state
        mock_store_fn.return_value = mock_store

        result = submit_phase_decision("wf-phase-pages", 3, "approve_and_move_next")

        assert result["success"] is True
        saved_state = mock_store.save_workflow.call_args[0][1]
        assert saved_state["phase_page_decision"][3] == "approve_and_move_next"
        assert saved_state["waiting_for_human"] is False
        assert saved_state["phases"][3]["status"] == "completed"
        mock_sync.assert_called_once()

    @patch("hokusai.utils.notion_helpers.sync_phase_page_from_state", return_value=True)
    @patch("scripts.dashboard.is_workflow_running", return_value=False)
    @patch("scripts.dashboard._get_store")
    def test_submit_phase_decision_request_changes_keeps_waiting_state(
        self, mock_store_fn, mock_running, mock_sync,
    ):
        state = {
            "workflow_id": "wf-phase-pages",
            "current_phase": 2,
            "waiting_for_human": True,
            "human_input_request": "cross_review_blocked",
            "phase_page_decision": {},
            "phase_page_last_human_note_at": {},
            "phase_page_recommended_action": {},
            "phase_subpages": {2: "https://notion.so/p2"},
            "phases": {2: {"status": "failed", "error_message": "blocked", "retry_count": 0}},
            "audit_log": [],
            "updated_at": "2026-03-09T10:00:00+09:00",
        }
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = state
        mock_store_fn.return_value = mock_store

        result = submit_phase_decision("wf-phase-pages", 2, "request_changes")

        assert result["success"] is True
        saved_state = mock_store.save_workflow.call_args[0][1]
        assert saved_state["phase_page_decision"][2] == "request_changes"
        assert saved_state["waiting_for_human"] is True
        assert saved_state["phases"][2]["status"] == "failed"
        mock_sync.assert_called_once()


class TestBackgroundRunningUI:
    """バックグラウンド実行中のUI表示テスト"""

    def test_phase_table_shows_running_status_when_pending(self):
        """status=pending でも実行中バッジが表示される"""
        phases = {"1": {"status": "pending", "retry_count": 0}}
        state = {"current_phase": 1}
        html = render_phase_table(phases, state, workflow_id="wf-run", bg_running=True)
        assert ">実行中<" in html

    def test_phase_table_shows_running_status_when_in_progress(self):
        """status=in_progress でも実行中バッジが表示される"""
        phases = {"1": {"status": "in_progress", "retry_count": 0}}
        state = {"current_phase": 1}
        html = render_phase_table(phases, state, workflow_id="wf-run", bg_running=True)
        assert ">実行中<" in html

    def test_phase_table_shows_running_status_when_failed(self):
        """status=failed でも実行中バッジが表示される（リトライ実行中）"""
        phases = {"1": {"status": "failed", "retry_count": 1}}
        state = {"current_phase": 1}
        html = render_phase_table(phases, state, workflow_id="wf-run", bg_running=True)
        assert ">実行中<" in html

    def test_phase_table_disables_all_buttons_when_running(self):
        """実行中は全フェーズのボタンが無効になる"""
        phases = {
            "1": {"status": "pending", "retry_count": 0},
            "2": {"status": "pending", "retry_count": 0},
        }
        state = {"current_phase": 1}
        html = render_phase_table(phases, state, workflow_id="wf-run", bg_running=True)
        # 全ボタンが disabled
        assert 'phase-run-btn"  onclick' not in html
        # 現在フェーズは「実行中...」ラベル
        assert "実行中..." in html

    def test_phase_table_no_running_when_bg_false(self):
        """bg_running=False の場合は通常表示"""
        phases = {
            "1": {"status": "completed", "retry_count": 0},
            "2": {"status": "pending", "retry_count": 0},
        }
        state = {"current_phase": 1}
        html = render_phase_table(phases, state, workflow_id="wf-ok", bg_running=False)
        assert "実行中..." not in html
        assert ">実行中<" not in html

    def test_workflow_list_shows_running_badge(self):
        """一覧で実行中ワークフローに「実行中」バッジが表示される"""
        workflows = [{
            "workflow_id": "wf-running",
            "task_title": "Running Task",
            "task_url": "https://notion.so/running",
            "current_phase": 1,
            "updated_at": "2026-03-02T10:00:00",
            "repos": [],
            "waiting_status": None,
            "run_mode": "step",
            "current_phase_status": "pending",
        }]
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch("scripts.dashboard._bg_processes", {"start:wf-running": mock_proc}), \
             patch("scripts.dashboard._BG_META_DIR", Path("/nonexistent")):
            html = render_workflow_list(workflows)
        assert ">実行中<" in html
        assert "実行中..." in html
        assert "disabled" in html

    def test_workflow_list_no_running_badge_when_idle(self):
        """実行中でないワークフローには「実行中」バッジなし"""
        workflows = [{
            "workflow_id": "wf-idle",
            "task_title": "Idle Task",
            "task_url": "https://notion.so/idle",
            "current_phase": 3,
            "updated_at": "2026-03-02T10:00:00",
            "repos": [],
            "waiting_status": "waiting",
            "run_mode": "step",
            "current_phase_status": "in_progress",
        }]
        with patch("scripts.dashboard._bg_processes", {}), \
             patch("scripts.dashboard._BG_META_DIR", Path("/nonexistent")):
            html = render_workflow_list(workflows)
        assert ">実行中<" not in html
        assert "実行中..." not in html

    def test_is_workflow_running_checks_continue(self):
        """continue: プレフィクスで実行中判定"""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch("scripts.dashboard._bg_processes", {"continue:wf-1": mock_proc}), \
             patch("scripts.dashboard._BG_META_DIR", Path("/nonexistent")):
            assert is_workflow_running("wf-1") is True
            assert is_workflow_running("wf-2") is False

    def test_is_workflow_running_checks_start_by_workflow_id(self):
        """start: workflow_id で実行中判定"""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch("scripts.dashboard._bg_processes", {"start:wf-1": mock_proc}), \
             patch("scripts.dashboard._BG_META_DIR", Path("/nonexistent")):
            assert is_workflow_running("wf-1") is True
            assert is_workflow_running("wf-2") is False

    def test_is_workflow_running_no_task_url_fallback(self):
        """task_url ベース identifier では実行中判定しない"""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch("scripts.dashboard._bg_processes", {"start:https://notion.so/x": mock_proc}), \
             patch("scripts.dashboard._BG_META_DIR", Path("/nonexistent")):
            # task_url ベースの identifier は is_workflow_running では拾えない
            assert is_workflow_running("wf-1") is False


class TestRunningMetaPersistence:
    """永続メタデータの保存・復元・クリーンアップのテスト"""

    def test_save_and_restore_meta(self, tmp_path):
        """メタ保存→_get_running_identifiersで復元"""
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        pid = os.getpid()  # 自プロセスは必ず生存
        with patch("scripts.dashboard._BG_META_DIR", meta_dir), \
             patch("scripts.dashboard._bg_processes", {}), \
             patch("scripts.dashboard._verify_pid_is_hokusai", return_value=True):
            _save_running_meta("continue:wf-persist", pid, "/tmp/test.log")
            running = _get_running_identifiers()
        assert "continue:wf-persist" in running

    def test_save_meta_includes_cmdline(self, tmp_path):
        """メタにcmdlineが保存される"""
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        with patch("scripts.dashboard._BG_META_DIR", meta_dir):
            _save_running_meta("start:wf-cmd", 111, "/tmp/t.log", cmdline=["hokusai", "start", "url"])
            meta_file = meta_dir / "start_wf-cmd.json"
            meta = json.loads(meta_file.read_text())
        assert meta["cmdline"] == ["hokusai", "start", "url"]
        assert "started_at" in meta

    def test_dead_pid_cleaned_up(self, tmp_path):
        """非生存pidのメタはクリーンアップされる"""
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        dead_pid = 999999  # 存在しないPID
        meta_file = meta_dir / "continue_wf-dead.json"
        meta_file.write_text(json.dumps({
            "identifier": "continue:wf-dead",
            "pid": dead_pid,
            "log_file": "/tmp/test.log",
            "started_at": datetime.now().isoformat(),
        }))
        with patch("scripts.dashboard._BG_META_DIR", meta_dir), \
             patch("scripts.dashboard._bg_processes", {}):
            running = _get_running_identifiers()
        assert "continue:wf-dead" not in running
        assert not meta_file.exists()  # クリーンアップされた

    def test_stale_meta_cleaned_up_by_age(self, tmp_path):
        """started_atが最大経過時間を超えたメタはクリーンアップされる（pid再利用対策）"""
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        pid = os.getpid()  # 生存PIDだが古い
        from datetime import timedelta
        old_time = (datetime.now() - timedelta(seconds=_BG_MAX_AGE_SECONDS + 100)).isoformat()
        meta_file = meta_dir / "start_wf-stale.json"
        meta_file.write_text(json.dumps({
            "identifier": "start:wf-stale",
            "pid": pid,
            "log_file": "/tmp/test.log",
            "started_at": old_time,
        }))
        with patch("scripts.dashboard._BG_META_DIR", meta_dir), \
             patch("scripts.dashboard._bg_processes", {}):
            running = _get_running_identifiers()
        assert "start:wf-stale" not in running
        assert not meta_file.exists()

    def test_non_hokusai_pid_cleaned_up(self, tmp_path):
        """生存PIDでもhokusaiプロセスでなければクリーンアップされる"""
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        pid = os.getpid()
        meta_file = meta_dir / "start_wf-alien.json"
        meta_file.write_text(json.dumps({
            "identifier": "start:wf-alien",
            "pid": pid,
            "log_file": "/tmp/test.log",
            "started_at": datetime.now().isoformat(),
        }))
        with patch("scripts.dashboard._BG_META_DIR", meta_dir), \
             patch("scripts.dashboard._bg_processes", {}), \
             patch("scripts.dashboard._verify_pid_is_hokusai", return_value=False):
            running = _get_running_identifiers()
        assert "start:wf-alien" not in running
        assert not meta_file.exists()

    def test_corrupted_json_cleaned_up(self, tmp_path):
        """壊れたJSONのメタファイルはフェイルセーフでクリーンアップ"""
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        meta_file = meta_dir / "broken.json"
        meta_file.write_text("{invalid json")
        with patch("scripts.dashboard._BG_META_DIR", meta_dir), \
             patch("scripts.dashboard._bg_processes", {}):
            running = _get_running_identifiers()
        assert len(running) == 0
        assert not meta_file.exists()

    def test_remove_meta(self, tmp_path):
        """メタ削除が正常に動作する"""
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()
        with patch("scripts.dashboard._BG_META_DIR", meta_dir):
            _save_running_meta("start:wf-rm", 12345, "/tmp/test.log")
            safe_name = "start_wf-rm"
            assert (meta_dir / f"{safe_name}.json").exists()
            _remove_running_meta("start:wf-rm")
            assert not (meta_dir / f"{safe_name}.json").exists()


class TestRetryPhase:
    """フェーズリトライ機能のテスト"""

    def _make_state(self, current_phase=2):
        """テスト用のワークフロー状態を作成"""
        phases = {}
        for i in range(1, 11):
            if i < current_phase:
                phases[str(i)] = {"status": "completed", "started_at": "2026-01-01T00:00:00",
                                  "completed_at": "2026-01-01T01:00:00", "error_message": None, "retry_count": 0}
            else:
                phases[str(i)] = {"status": "pending", "started_at": None,
                                  "completed_at": None, "error_message": None, "retry_count": 0}
        return {
            "workflow_id": "wf-retry",
            "task_url": "https://notion.so/test",
            "task_title": "Test",
            "current_phase": current_phase,
            "phases": phases,
            "research_result": "some research",
            "schema_change_required": True,
            "work_plan": "some plan",
            "expected_changed_files": ["a.py"],
            "implementation_result": "done",
            "waiting_for_human": False,
            "human_input_request": None,
        }

    def test_retry_resets_phases_and_outputs(self, temp_db):
        """リトライで指定フェーズ以降がリセットされる"""
        state = self._make_state(current_phase=5)
        # Phase 1-4 completed, Phase 5-9 pending
        state["phases"]["5"]["status"] = "failed"
        state["phases"]["5"]["error_message"] = "err"

        store = SQLiteStore(temp_db)
        store.save_workflow("wf-retry", state)

        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._store", None), \
             patch("scripts.dashboard.CHECKPOINT_DB_PATH", temp_db):  # 同じDBでOK（テーブルなし=スキップ）
            result = retry_phase("wf-retry", 3)

        assert result["success"] is True
        assert result["reset_phases"] == [3, 4, 5, 6, 7, 8, 9, 10]

        # DB を再読み込みして検証（SQLiteStore は int キーで返す）
        reloaded = store.load_workflow("wf-retry")
        assert reloaded["current_phase"] == 3
        assert reloaded["phases"][2]["status"] == "completed"
        assert reloaded["phases"][3]["status"] == "pending"
        assert reloaded["phases"][5]["status"] == "pending"
        assert reloaded["research_result"] == "some research"  # Phase 2 は保持
        assert reloaded["work_plan"] is None  # Phase 4 出力はクリア

    def test_retry_from_phase2_clears_research(self, temp_db):
        """Phase 2 からリトライすると research_result がクリアされる"""
        state = self._make_state(current_phase=3)

        store = SQLiteStore(temp_db)
        store.save_workflow("wf-retry", state)

        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._store", None), \
             patch("scripts.dashboard.CHECKPOINT_DB_PATH", temp_db):
            result = retry_phase("wf-retry", 2)

        assert result["success"] is True
        reloaded = store.load_workflow("wf-retry")
        assert reloaded["current_phase"] == 2
        assert reloaded.get("research_result") is None
        assert reloaded.get("schema_change_required") is False

    def test_retry_nonexistent_workflow(self, temp_db):
        """存在しないワークフローのリトライはエラー"""
        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._store", None):
            result = retry_phase("wf-nonexistent", 2)
        assert result["success"] is False

    def test_retry_clears_verification_and_repositories(self, temp_db):
        """Phase 6 以前からリトライすると verification 関連がクリアされる"""
        state = self._make_state(current_phase=7)
        state["phases"]["6"]["retry_count"] = 2
        state["verification"] = {"build": "pass", "test": "pass", "lint": "fail"}
        state["verification_errors"] = [
            {"repository": "Backend", "command": "lint", "success": False,
             "error_output": "lint error"},
        ]
        state["repositories"] = [
            {"name": "Backend", "phase_status": {"6": "failed"}},
            {"name": "API", "phase_status": {"6": "failed"}},
        ]

        store = SQLiteStore(temp_db)
        store.save_workflow("wf-retry", state)

        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._store", None), \
             patch("scripts.dashboard.CHECKPOINT_DB_PATH", temp_db):
            result = retry_phase("wf-retry", 4)

        assert result["success"] is True
        reloaded = store.load_workflow("wf-retry")
        assert reloaded["phases"][6]["retry_count"] == 0
        assert reloaded.get("verification") == {}
        assert reloaded.get("verification_errors") == []
        for repo in reloaded.get("repositories", []):
            assert "6" not in repo.get("phase_status", {})
            assert 6 not in repo.get("phase_status", {})

    def test_retry_from_phase7_keeps_verification(self, temp_db):
        """Phase 7 からリトライしても Phase 6 の verification は保持される"""
        state = self._make_state(current_phase=8)
        state["verification"] = {"build": "pass", "test": "pass", "lint": "pass"}
        state["verification_errors"] = []
        state["repositories"] = [
            {"name": "Backend", "phase_status": {"6": "completed"}},
        ]

        store = SQLiteStore(temp_db)
        store.save_workflow("wf-retry", state)

        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._store", None), \
             patch("scripts.dashboard.CHECKPOINT_DB_PATH", temp_db):
            result = retry_phase("wf-retry", 7)

        assert result["success"] is True
        reloaded = store.load_workflow("wf-retry")
        assert reloaded.get("verification") == {"build": "pass", "test": "pass", "lint": "pass"}
        assert reloaded.get("repositories", [{}])[0].get("phase_status", {}).get("6") == "completed"

    def test_retry_invalid_phase(self, temp_db):
        """無効なフェーズ番号はエラー"""
        state = self._make_state()
        store = SQLiteStore(temp_db)
        store.save_workflow("wf-retry", state)

        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._store", None):
            result = retry_phase("wf-retry", 0)
        assert result["success"] is False

        with patch("scripts.dashboard.DB_PATH", temp_db), \
             patch("scripts.dashboard._store", None):
            result = retry_phase("wf-retry", 11)
        assert result["success"] is False

    def test_retry_clears_checkpoint_db(self, tmp_path):
        """リトライでチェックポイントDBもクリアされる"""
        # チェックポイントDB を作成
        cp_db = tmp_path / "checkpoint.db"
        with sqlite3.connect(str(cp_db)) as conn:
            conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB)")
            conn.execute("CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB)")
            conn.execute("INSERT INTO checkpoints VALUES ('wf-retry', '', 'cp-1', NULL, 'msgpack', X'00', X'00')")
            conn.execute("INSERT INTO writes VALUES ('wf-retry', '', 'cp-1', 'task-1', 0, 'ch', NULL, X'00')")

        # ワークフローDB
        wf_db = tmp_path / "workflow.db"
        store = SQLiteStore(wf_db)
        state = self._make_state(current_phase=3)
        store.save_workflow("wf-retry", state)

        with patch("scripts.dashboard.DB_PATH", wf_db), \
             patch("scripts.dashboard._store", None), \
             patch("scripts.dashboard.CHECKPOINT_DB_PATH", cp_db):
            result = retry_phase("wf-retry", 2)

        assert result["success"] is True

        # チェックポイントが削除されたことを確認
        with sqlite3.connect(str(cp_db)) as conn:
            cp_count = conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id='wf-retry'").fetchone()[0]
            wr_count = conn.execute("SELECT count(*) FROM writes WHERE thread_id='wf-retry'").fetchone()[0]
        assert cp_count == 0
        assert wr_count == 0

    def test_retry_button_in_phase_table(self):
        """完了/失敗フェーズにリトライボタンが表示される"""
        phases = {
            "1": {"status": "completed", "retry_count": 0},
            "2": {"status": "failed", "retry_count": 1},
            "3": {"status": "pending", "retry_count": 0},
        }
        state = {"current_phase": 2, "cross_review_results": {}}
        html = render_phase_table(phases, state, workflow_id="wf-btn")
        # Phase 1 (completed) にリトライボタンあり
        assert "retryFromPhase('wf-btn', 1)" in html
        # Phase 2 (failed) にリトライボタンあり
        assert "retryFromPhase('wf-btn', 2)" in html
        # Phase 3 (pending) にリトライボタンなし
        assert "retryFromPhase('wf-btn', 3)" not in html


# === Notion接続状態バッジ ===


class TestNotionStatusBadge:
    """ダッシュボードの Notion バッジ表示テスト"""

    def test_badge_connected(self):
        """notion_connected=True で Connected バッジ"""
        html = _notion_status_badge({"notion_connected": True})
        assert "Connected" in html
        assert "badge-ok" in html

    def test_badge_disconnected(self):
        """notion_connected=False で Disconnected バッジ"""
        html = _notion_status_badge({"notion_connected": False})
        assert "Disconnected (Skipped)" in html
        assert "badge-warn" in html

    def test_badge_unknown(self):
        """notion_connected 未設定で - バッジ"""
        html = _notion_status_badge({})
        assert "badge-muted" in html

    def test_warning_banner_shown(self):
        """Disconnected 時に警告バナーが表示される"""
        html = _notion_warning_banner({"notion_connected": False})
        assert "notion-warning" in html
        assert "Notion に反映されていません" in html

    def test_warning_banner_hidden_when_connected(self):
        """Connected 時に警告バナーが出ない"""
        html = _notion_warning_banner({"notion_connected": True})
        assert html == ""

    def test_detail_page_shows_notion_badge(self):
        """詳細ページに Notion バッジが含まれる"""
        state = {
            "workflow_id": "wf-test",
            "task_title": "Test",
            "task_url": "https://notion.so/page",
            "branch_name": "feat/test",
            "base_branch": "main",
            "current_phase": 1,
            "verification": {},
            "phases": {},
            "audit_log": [],
            "pull_requests": [],
            "final_review_rules": {},
            "final_review_by_repo": {},
            "notion_connected": False,
        }
        html = render_detail_page(state)
        assert "Disconnected (Skipped)" in html
        assert "Notion に反映されていません" in html


class TestHygieneActionBanner:
    """ブランチ衛生アクションバナーのテスト"""

    def test_waiting_status_branch_hygiene(self):
        """branch_hygiene 待機状態が正しく判定される"""
        state = {
            "waiting_for_human": True,
            "human_input_request": "branch_hygiene",
        }
        assert get_waiting_status(state) == "branch_hygiene"

    def test_waiting_status_label_branch_hygiene(self):
        """branch_hygiene のラベルが正しい"""
        assert waiting_status_label("branch_hygiene") == "ブランチ整理待ち"

    def test_banner_shown_when_branch_hygiene(self):
        """branch_hygiene 待機時にバナーが表示される"""
        state = {
            "waiting_for_human": True,
            "human_input_request": "branch_hygiene",
            "branch_hygiene_issues": [
                {
                    "type": "already_merged_commits",
                    "severity": "warning",
                    "message": "マージ済みコミットが3件",
                    "recommendation": "rebase で除外を推奨",
                }
            ],
        }
        html = _hygiene_action_banner(state, "wf-test")
        assert "hygiene-action-banner" in html
        assert "マージ済みコミットが3件" in html
        assert "Rebase" in html
        assert "Cherry-pick" in html
        assert "Merge" in html

    def test_banner_hidden_when_not_hygiene(self):
        """branch_hygiene 以外の待機時にはバナーが出ない"""
        state = {
            "waiting_for_human": True,
            "human_input_request": "implementation",
        }
        html = _hygiene_action_banner(state, "wf-test")
        assert html == ""

    def test_banner_hidden_when_not_waiting(self):
        """待機中でない場合はバナーが出ない"""
        state = {"waiting_for_human": False}
        html = _hygiene_action_banner(state, "wf-test")
        assert html == ""

    def test_detail_page_shows_hygiene_banner(self):
        """詳細ページに衛生アクションバナーが表示される"""
        state = {
            "workflow_id": "wf-hygiene",
            "task_title": "Test",
            "task_url": "https://notion.so/page",
            "branch_name": "feat/test",
            "base_branch": "beta",
            "current_phase": 7,
            "verification": {},
            "phases": {},
            "audit_log": [],
            "pull_requests": [],
            "final_review_rules": {},
            "final_review_by_repo": {},
            "waiting_for_human": True,
            "human_input_request": "branch_hygiene",
            "branch_hygiene_issues": [
                {"type": "already_merged_commits", "severity": "warning", "message": "テスト問題"},
            ],
        }
        html = render_detail_page(state)
        assert "hygiene-action-banner" in html
        assert "executeHygieneAction" in html

    @patch("scripts.dashboard._launch_hokusai_background")
    @patch("scripts.dashboard._get_store")
    def test_continue_step_passes_action(self, mock_store, mock_launch):
        """continue_workflow_step_mode が action を CLI に渡す"""
        mock_store.return_value.load_workflow.return_value = {"config_name": None}
        mock_launch.return_value = {"launched": True, "pid": 123, "log_file": "/tmp/test.log"}

        continue_workflow_step_mode("wf-test", action="rebase")

        cmd = mock_launch.call_args[0][0]
        assert "--action" in cmd
        assert "rebase" in cmd

    @patch("scripts.dashboard._launch_hokusai_background")
    @patch("scripts.dashboard._get_store")
    def test_continue_auto_passes_action(self, mock_store, mock_launch):
        """continue_workflow_auto_mode が action を CLI に渡す"""
        mock_store.return_value.load_workflow.return_value = {"config_name": None}
        mock_launch.return_value = {"launched": True, "pid": 123, "log_file": "/tmp/test.log"}

        continue_workflow_auto_mode("wf-test", action="cherry-pick")

        cmd = mock_launch.call_args[0][0]
        assert "--action" in cmd
        assert "cherry-pick" in cmd

    @patch("scripts.dashboard._launch_hokusai_background")
    @patch("scripts.dashboard._get_store")
    def test_continue_without_action(self, mock_store, mock_launch):
        """action なしの場合は --action が付かない"""
        mock_store.return_value.load_workflow.return_value = {"config_name": None}
        mock_launch.return_value = {"launched": True, "pid": 123, "log_file": "/tmp/test.log"}

        continue_workflow_step_mode("wf-test")

        cmd = mock_launch.call_args[0][0]
        assert "--action" not in cmd


class TestCrossReviewBlocked:
    """cross-review blocked 関連テスト"""

    def _make_state_with_critical(self, phase=2):
        """critical findings 付きの state を生成"""
        return {
            "workflow_id": "wf-cr-test",
            "config_name": "my-project",
            "task_url": "https://notion.so/test",
            "task_title": "CR Test",
            "branch_name": "feature/cr",
            "current_phase": phase,
            "waiting_for_human": True,
            "human_input_request": "",
            "phases": {
                phase: {"status": "failed", "error_message": "cross_review_blocked"},
            },
            "cross_review_results": {
                phase: {
                    "overall_assessment": "reject",
                    "summary": "Critical issues found",
                    "confidence_score": 0.85,
                    "findings": [
                        {
                            "severity": "critical",
                            "title": "Missing error handling",
                            "description": "No error handling for API calls",
                            "suggestion": "Add try-catch blocks",
                        },
                        {
                            "severity": "minor",
                            "title": "Typo in variable name",
                            "description": "Typo found",
                            "suggestion": "Fix the typo",
                        },
                    ],
                }
            },
            "research_result": "## Research\nSome research content",
            "design_result": "## Design\nSome design content",
            "work_plan": "## Plan\nSome plan content",
            "audit_log": [
                {"phase": phase, "action": "cross_review_blocked",
                 "timestamp": "2026-03-06T00:00:00"},
            ],
            "updated_at": "2026-03-06T00:00:00",
            "phase_subpages": {},
        }

    def test_get_waiting_status_cross_review_blocked(self):
        """critical findings ありで cross_review_blocked を返す"""
        state = self._make_state_with_critical(phase=2)
        assert get_waiting_status(state) == "cross_review_blocked"

    def test_get_waiting_status_cross_review_no_critical(self):
        """critical なしで cross_review_blocked にならない"""
        state = self._make_state_with_critical(phase=2)
        # findings から critical を除去
        state["cross_review_results"][2]["findings"] = [
            {"severity": "minor", "title": "Typo", "description": "Minor issue"},
        ]
        assert get_waiting_status(state) != "cross_review_blocked"

    def test_cross_review_blocked_uses_latest_phase(self):
        """Phase 2 と 3 に critical findings がある場合、audit_log 最新のフェーズを返す"""
        state = self._make_state_with_critical(phase=3)
        # Phase 2 は completed（解消済み）だが、cross_review_results に履歴が残っている
        state["phases"][2] = {"status": "completed", "error_message": None}
        state["cross_review_results"][2] = {
            "overall_assessment": "reject",
            "summary": "Old phase 2 issues",
            "confidence_score": 0.7,
            "findings": [
                {"severity": "critical", "title": "Old issue",
                 "description": "This is from phase 2"},
            ],
        }
        # audit_log は Phase 3 が最新
        state["audit_log"] = [
            {"phase": 2, "action": "cross_review_blocked",
             "timestamp": "2026-03-05T00:00:00"},
            {"phase": 3, "action": "cross_review_blocked",
             "timestamp": "2026-03-06T00:00:00"},
        ]
        assert get_waiting_status(state) == "cross_review_blocked"
        html = _cross_review_blocked_banner(state, "wf-cr-test")
        assert "Phase 3" in html
        assert "Phase 2" not in html

    def test_completed_phase_excluded_from_blocked(self):
        """completed フェーズは cross_review_results に critical が残っていても除外"""
        state = self._make_state_with_critical(phase=2)
        # Phase 2 を completed に修復
        state["phases"][2]["status"] = "completed"
        state["phases"][2]["error_message"] = None
        # cross_review_results と audit_log は残存
        assert _find_cross_review_blocked_phase(state) is None
        assert get_waiting_status(state) != "cross_review_blocked"

    def test_failed_cross_review_blocked_still_detected(self):
        """現在 failed + cross_review_blocked のフェーズは正しく検出される"""
        state = self._make_state_with_critical(phase=3)
        assert _find_cross_review_blocked_phase(state) == 3
        html = _cross_review_blocked_banner(state, "wf-cr-test")
        assert "Phase 3" in html

    def test_phase5_waiting_not_overridden_by_old_cross_review(self):
        """Phase 5 の別待機理由が、過去の cross review 履歴で上書きされない"""
        state = self._make_state_with_critical(phase=2)
        # Phase 2 は completed（解消済み）
        state["phases"][2]["status"] = "completed"
        state["phases"][2]["error_message"] = None
        # Phase 5 で別理由により waiting_for_human
        state["current_phase"] = 5
        state["phases"][5] = {"status": "in_progress", "error_message": None}
        state["human_input_request"] = "作業計画（work_plan）が取得できませんでした。"
        assert get_waiting_status(state) != "cross_review_blocked"
        html = _cross_review_blocked_banner(state, "wf-cr-test")
        assert html == ""

    def test_cross_review_blocked_banner_renders_findings(self):
        """バナー HTML に findings が含まれる"""
        state = self._make_state_with_critical(phase=3)
        html = _cross_review_blocked_banner(state, "wf-cr-test")
        assert "cross-review-banner" in html
        assert "Phase 3" in html
        assert "Missing error handling" in html
        assert "Add try-catch blocks" in html
        assert "applyCrossReviewFixes" in html
        assert "rerunCrossReview" in html
        assert "ignoreCrossReview" in html

    @patch("scripts.dashboard.is_workflow_running", return_value=False)
    @patch("scripts.dashboard._get_store")
    def test_apply_cross_review_fixes_updates_state(self, mock_store_fn, mock_running):
        """apply_cross_review_fixes が state 内文書を更新する"""
        state = self._make_state_with_critical(phase=2)
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = state
        mock_store_fn.return_value = mock_store

        with patch("hokusai.integrations.claude_code.ClaudeCodeClient") as mock_claude_cls:
            mock_claude = MagicMock()
            mock_claude.execute_prompt.return_value = "## Research\nFixed content"
            mock_claude_cls.return_value = mock_claude

            result = apply_cross_review_fixes("wf-cr-test", 2)

        assert result["success"] is True
        # save_workflow が呼ばれ、research_result が更新されている
        saved_state = mock_store.save_workflow.call_args[0][1]
        assert saved_state["research_result"] == "## Research\nFixed content"

    @patch("scripts.dashboard.is_workflow_running", return_value=False)
    @patch("scripts.dashboard._get_store")
    def test_rerun_cross_review_clears_waiting(self, mock_store_fn, mock_running):
        """critical 解消で waiting_for_human=False になる"""
        state = self._make_state_with_critical(phase=4)
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = state
        mock_store_fn.return_value = mock_store

        def fake_cross_review(st, doc, phase):
            # critical なしの結果に置換
            st["cross_review_results"][phase] = {
                "overall_assessment": "approve",
                "findings": [{"severity": "minor", "title": "ok", "description": "ok"}],
            }
            return st

        from hokusai.config.models import WorkflowConfig, CrossReviewConfig
        fake_config = WorkflowConfig(cross_review=CrossReviewConfig(enabled=True, phases=[2, 3, 4]))
        with patch("hokusai.utils.cross_review.execute_cross_review", side_effect=fake_cross_review), \
             patch("scripts.dashboard._resolve_config_path", return_value=Path("/tmp/fake.yaml")), \
             patch("hokusai.config.create_config_from_env_and_file", return_value=fake_config):
            result = rerun_cross_review_for_phase("wf-cr-test", 4)

        assert result["success"] is True
        assert result["critical_count"] == 0
        saved_state = mock_store.save_workflow.call_args[0][1]
        assert saved_state["waiting_for_human"] is False
        # フェーズステータスが completed に復元されている
        assert saved_state["phases"][4]["status"] == "completed"
        assert saved_state["phases"][4]["error_message"] is None
        assert saved_state["phases"][4]["completed_at"] is not None
        # current_phase が次のフェーズに進んでいる
        assert saved_state["current_phase"] == 5

    @patch("scripts.dashboard._get_store")
    def test_continue_ignoring_cross_review_audit_log(self, mock_store_fn):
        """無視時に監査ログが記録される"""
        state = self._make_state_with_critical(phase=3)
        mock_store = MagicMock()
        mock_store.load_workflow.return_value = state
        mock_store_fn.return_value = mock_store

        result = continue_ignoring_cross_review("wf-cr-test", 3)

        assert result["success"] is True
        saved_state = mock_store.save_workflow.call_args[0][1]
        assert saved_state["waiting_for_human"] is False
        # current_phase が次のフェーズに進んでいる
        assert saved_state["current_phase"] == 4
        assert saved_state["phases"][3]["completed_at"] is not None
        # 監査ログに cross_review_ignored が記録されている
        log_actions = [entry["action"] for entry in saved_state["audit_log"]]
        assert "cross_review_ignored" in log_actions


# ---------------------------------------------------------------------------
# Phase 6 失敗分類・パネル・設定リンク・コマンド検証
# ---------------------------------------------------------------------------


class TestClassifyVerificationError:
    """classify_verification_error のテスト"""

    def test_config_error_unexpected_eof(self):
        """Case 1: シェル構文エラーは config_error"""
        err = {
            "repository": "Backend",
            "command": "lint",
            "success": False,
            "error_output": "sh: -c: line 0: unexpected EOF while looking for matching `'`",
        }
        result = classify_verification_error(err)
        assert result["category"] == "config_error"
        assert result["repository"] == "Backend"
        assert result["command"] == "lint"

    def test_code_error_pxx_ng(self):
        """Case 2: Pxx NG はコード違反 → code_error"""
        err = {
            "repository": "API",
            "command": "lint",
            "success": False,
            "error_output": "[P01] NG: src/my_api.yml - インライン oneOf",
        }
        result = classify_verification_error(err)
        assert result["category"] == "code_error"

    def test_code_error_pxx_header_only(self):
        """[Pxx] セクションヘッダーのみでも code_error"""
        err = {
            "repository": "API",
            "command": "lint",
            "success": False,
            "error_output": "=== API Repository Lint Checks ===\n--- [P01] OpenAPI インライン定義チェック ---\n[P01] 対象: src/my_api.yml",
        }
        result = classify_verification_error(err)
        assert result["category"] == "code_error"

    def test_code_error_pxx_multiple(self):
        """複数 Pxx NG も code_error"""
        err = {
            "repository": "Backend",
            "command": "lint",
            "success": False,
            "error_output": "[P06] NG: src/pages/user.tsx - Form.Select + register()\n[P07] NG: src/pages/user.tsx - string 使用",
        }
        result = classify_verification_error(err)
        assert result["category"] == "code_error"

    def test_environment_error(self):
        """Case 3: ポート競合は environment_error"""
        err = {
            "repository": "Backend",
            "command": "test",
            "success": False,
            "error_output": "Error: EADDRINUSE: port 3000 already in use",
        }
        result = classify_verification_error(err)
        assert result["category"] == "environment_error"

    def test_code_error_typescript(self):
        """Case 4: TypeScript エラーは code_error"""
        err = {
            "repository": "Backend",
            "command": "build",
            "success": False,
            "error_output": "src/main.ts(10,5): error TS2345: Argument of type ...",
        }
        result = classify_verification_error(err)
        assert result["category"] == "code_error"

    def test_unknown_error(self):
        """分類不能は unknown"""
        err = {
            "repository": "Backend",
            "command": "test",
            "success": False,
            "error_output": "some obscure error message",
        }
        result = classify_verification_error(err)
        assert result["category"] == "unknown"

    def test_success_entry_returns_unknown(self):
        """成功エントリでも呼べる（分類は unknown）"""
        err = {
            "repository": "Backend",
            "command": "build",
            "success": True,
            "error_output": None,
        }
        result = classify_verification_error(err)
        assert result["category"] == "unknown"


class TestPhase6FailureSummary:
    def test_filters_only_failed_entries(self):
        state = {
            "verification_errors": [
                {"repository": "Backend", "command": "build", "success": True, "error_output": None},
                {"repository": "API", "command": "lint", "success": False, "error_output": "--- [P01] OpenAPI check ---\n[P01] 対象: src/api.yml"},
            ],
        }
        result = phase6_failure_summary(state)
        assert len(result) == 1
        assert result[0]["repository"] == "API"
        assert result[0]["category"] == "code_error"

    def test_empty_errors(self):
        state = {"verification_errors": []}
        assert phase6_failure_summary(state) == []


class TestRenderPhase6FailurePanel:
    """Case 5: Phase 6 失敗パネル表示"""

    def test_renders_panel_with_failure(self):
        state = {
            "verification": {"build": "pass", "test": "pass", "lint": "fail"},
            "verification_errors": [
                {"repository": "Backend", "command": "build", "success": True, "error_output": None},
                {"repository": "API", "command": "lint", "success": False,
                 "error_output": "[P01] NG: src/my_api.yml - インライン\nline2\nline3"},
            ],
            "config_name": "my-project",
        }
        html = render_phase6_failure_panel(state, "wf-test")
        assert "Phase 6 失敗詳細" in html
        assert "[API] lint 失敗" in html
        assert "推定原因: コード問題" in html
        assert "Phase 5 からやり直す" in html
        assert "Phase 6 をリセット" in html
        assert "[P01] NG" in html

    def test_no_panel_when_no_failures(self):
        state = {
            "verification": {"build": "pass", "test": "pass", "lint": "pass"},
            "verification_errors": [],
        }
        html = render_phase6_failure_panel(state, "wf-test")
        assert html == ""

    def test_no_panel_when_empty_verification(self):
        state = {"verification": {}, "verification_errors": []}
        assert render_phase6_failure_panel(state, "wf-test") == ""


class TestPhase6SettingsLink:
    """Case 6: 設定リンク生成"""

    def test_generates_deep_link(self):
        link = _phase6_settings_link("my-project", "API", "lint")
        assert link == "/settings?config=my-project&repo=API&field=lint_command"

    def test_generates_deep_link_with_workflow_id(self):
        link = _phase6_settings_link("my-project", "API", "lint", "wf-123")
        assert link == "/settings?config=my-project&repo=API&field=lint_command&workflow_id=wf-123"

    def test_url_encodes_special_characters(self):
        link = _phase6_settings_link("my config", "My Repo", "lint")
        assert "my%20config" in link
        assert "My%20Repo" in link

    def test_no_config_name(self):
        link = _phase6_settings_link("", "API", "lint")
        assert link == "/settings"


class TestValidateCommandFields:
    """設定保存時のコマンド検証"""

    def test_valid_simple_command(self):
        data = {"build_command": "pnpm build", "repositories": []}
        result = validate_command_fields(data)
        assert result["errors"] == []
        assert result["warnings"] == []

    def test_unmatched_single_quotes(self):
        data = {"lint_command": "bash -c 'echo hello", "repositories": []}
        result = validate_command_fields(data)
        assert any("クオート" in e or "閉じ" in e for e in result["errors"])

    def test_if_fi_mismatch(self):
        data = {"lint_command": "bash -c 'if true; then echo ok'", "repositories": []}
        result = validate_command_fields(data)
        assert any("if" in e and "fi" in e for e in result["errors"])

    def test_greedy_regex_warning(self):
        data = {
            "repositories": [
                {"name": "API", "lint_command": r"rg 'requestBody:[\s\S]*schema:' file.yml"}
            ]
        }
        result = validate_command_fields(data)
        assert any(r"[\s\S]*" in w for w in result["warnings"])

    def test_repo_for_done_mismatch(self):
        data = {
            "repositories": [
                {"name": "Backend", "lint_command": "bash -c 'for f in *.ts; do echo $f'"}
            ]
        }
        result = validate_command_fields(data)
        assert any("for" in e and "done" in e for e in result["errors"])


class TestReviewStatusWaiting:
    """review_status 待機状態のテスト"""

    def test_get_waiting_status_review_status(self):
        """review_status が正しく判定される"""
        state = {
            "waiting_for_human": True,
            "human_input_request": "review_status",
        }
        assert get_waiting_status(state) == "review_status"

    def test_waiting_status_label_review_status(self):
        """review_status のラベルが正しい"""
        assert waiting_status_label("review_status") == "レビュー対応中"


class TestRenderPrProgressPerPR:
    """per-PR ボタン付き render_pr_progress のテスト"""

    def _make_state(self, prs, waiting=True, human_input="review_status", current_phase=9):
        return {
            "waiting_for_human": waiting,
            "human_input_request": human_input,
            "current_phase": current_phase,
            "pull_requests": prs,
            "phases": {9: {"status": "in_progress"}},
        }

    def test_per_pr_buttons_shown_in_review_status(self):
        """review_status 待ちの場合、per-PRボタンが表示される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "https://github.com/test/backend/pull/123", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "prReviewAction" in html
        assert "'recheck'" in html
        assert "'mark_complete'" in html

    def test_confirmed_pr_shows_unmark_button(self):
        """confirmed PRは「対応完了を取消」ボタンが表示される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "approved", "human_review_confirmed": True},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "'unmark_complete'" in html
        assert "'mark_complete'" not in html

    def test_finish_button_disabled_when_not_all_confirmed(self):
        """全PR未完了時は「次へ進む」ボタンがdisabled"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
            {"repo_name": "API", "number": 456, "url": "", "status": "approved", "human_review_confirmed": True},
        ]
        state = self._make_state(prs)
        _, finish_btn = render_pr_progress(state, "wf-test001")
        assert "finish_review" in finish_btn
        assert "disabled" in finish_btn

    def test_finish_button_enabled_when_all_confirmed(self):
        """全PR完了時は「次へ進む」ボタンが有効"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "approved", "human_review_confirmed": True},
            {"repo_name": "API", "number": 456, "url": "", "status": "approved", "human_review_confirmed": True},
        ]
        state = self._make_state(prs)
        _, finish_btn = render_pr_progress(state, "wf-test001")
        assert "finish_review" in finish_btn
        assert "disabled" not in finish_btn

    def test_no_buttons_without_workflow_id(self):
        """workflow_id なしの場合、ボタンなし"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, None)
        assert "prReviewAction" not in html

    def test_no_buttons_when_phase9_not_in_progress(self):
        """Phase 9 が in_progress でない場合、ボタンなし"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs, waiting=False)
        state["phases"] = {9: {"status": "completed"}}
        html, _ = render_pr_progress(state, "wf-test001")
        assert "prReviewAction" not in html

    def test_buttons_shown_when_not_waiting_but_in_progress(self):
        """待機中でなくてもPhase 9 in_progressならボタン表示"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs, waiting=False)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "prReviewAction" in html

    def test_buttons_shown_with_legacy_human_review(self):
        """既存ワークフローの human_review でもper-PRボタンが表示される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs, human_input="human_review")
        html, _ = render_pr_progress(state, "wf-test001")
        assert "prReviewAction" in html

    # --- テーブル構造の新規テスト ---

    def test_table_structure(self):
        """テーブル構造（table/thead/tbody）が出力される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "<table" in html
        assert "<thead>" in html
        assert "<tbody>" in html
        assert "</table>" in html

    def test_table_column_headers(self):
        """列見出し PR, コンポーネント, 進捗, アクション が出力される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "<th>PR</th>" in html
        assert "<th>コンポーネント</th>" in html
        assert "<th>進捗</th>" in html
        assert "<th>アクション</th>" in html

    def test_reviewer_stats_display_order(self):
        """reviewer内訳が Copilot, Devin の順で表示される"""
        prs = [{
            "repo_name": "Backend", "number": 123, "url": "", "status": "reviewing",
            "copilot_comments": [
                {"id": 1, "body": "a", "author": "Copilot", "replied": True},
                {"id": 2, "body": "b", "author": "Copilot", "replied": False},
            ],
            "human_comments": [
                {"id": 3, "body": "c", "author": "devin-ai-integration[bot]", "replied": True},
            ],
        }]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        copilot_pos = html.find("Copilot")
        devin_pos = html.find("Devin")
        assert copilot_pos < devin_pos
        assert "Copilot: 1/2件" in html
        assert "Devin: 1/1件" in html
        assert "人間: 0/0件" in html

    def test_push_warning_with_table(self):
        """push_warning とテーブルが共存する"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        state["push_verification_failed"] = True
        html, _ = render_pr_progress(state, "wf-test001")
        assert "コード修正がプッシュされていません" in html
        assert "<table" in html

    def test_confirmed_pr_icon(self):
        """完了済みPRのアイコンが ✅ で表示される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "approved", "human_review_confirmed": True},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "✅" in html

    def test_unconfirmed_pr_icon(self):
        """未完了PRのアイコンが ▶ で表示される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "▶" in html

    def test_pr_without_url_no_link(self):
        """URLなしPRではリンクが出ない"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "<code>#123</code>" in html
        assert 'href=""' not in html

    # --- モーダル構造のテスト ---

    def test_modal_dialog_exists(self):
        """dialog要素がモーダルとして出力される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "<dialog" in html
        assert "phase9-modal" in html
        assert ".showModal()" in html

    def test_summary_shown_inline(self):
        """サマリーがPhase 9行内に表示される"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing",
             "copilot_comments": [{"id": 1, "body": "a", "author": "Copilot", "replied": True}],
             "human_comments": []},
            {"repo_name": "API", "number": 456, "url": "", "status": "reviewing",
             "copilot_comments": [], "human_comments": []},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "phase9-summary" in html
        assert "#123" in html
        assert "#456" in html
        assert "1/1" in html

    def test_detail_button_opens_modal(self):
        """「レビュー詳細を開く」ボタンが存在する"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, "wf-test001")
        assert "レビュー詳細を開く" in html
        assert "phase9-detail-btn" in html

    def test_no_detail_button_without_workflow_id(self):
        """workflow_idなしでは詳細ボタンが出ない"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, _ = render_pr_progress(state, None)
        assert "レビュー詳細を開く" not in html

    def test_modal_contains_table_and_actions(self):
        """モーダル内にテーブルとアクションボタンが含まれる"""
        prs = [
            {"repo_name": "Backend", "number": 123, "url": "", "status": "reviewing"},
        ]
        state = self._make_state(prs)
        html, finish_btn = render_pr_progress(state, "wf-test001")
        # テーブルは dialog 内にある
        dialog_start = html.find("<dialog")
        dialog_end = html.find("</dialog>")
        dialog_content = html[dialog_start:dialog_end]
        assert "<table" in dialog_content
        assert "prReviewAction" in dialog_content
        # finish_review はモーダル外（Phase 9 行のアクション列）に表示
        assert "finish_review" in finish_btn


class TestHandlePrReviewAction:
    """handle_pr_review_action のテスト"""

    @patch("scripts.dashboard._get_store")
    def test_mark_complete(self, mock_store_fn):
        """mark_complete でPRが対応完了になる"""
        store = MagicMock()
        mock_store_fn.return_value = store
        state = {
            "workflow_id": "wf-test001",
            "pull_requests": [
                {"repo_name": "Backend", "number": 123, "status": "reviewing"},
            ],
        }
        store.load_workflow.return_value = state

        result = handle_pr_review_action("wf-test001", 0, "mark_complete")

        assert result["success"] is True
        saved_state = store.save_workflow.call_args[0][1]
        assert saved_state["pull_requests"][0]["human_review_confirmed"] is True
        assert saved_state["pull_requests"][0]["status"] == "approved"

    @patch("scripts.dashboard._get_store")
    def test_unmark_complete(self, mock_store_fn):
        """unmark_complete でPRの対応完了が取消される"""
        store = MagicMock()
        mock_store_fn.return_value = store
        state = {
            "workflow_id": "wf-test001",
            "pull_requests": [
                {"repo_name": "Backend", "number": 123, "status": "approved", "human_review_confirmed": True},
            ],
        }
        store.load_workflow.return_value = state

        result = handle_pr_review_action("wf-test001", 0, "unmark_complete")

        assert result["success"] is True
        saved_state = store.save_workflow.call_args[0][1]
        assert saved_state["pull_requests"][0]["human_review_confirmed"] is False
        assert saved_state["pull_requests"][0]["status"] == "reviewing"

    @patch("scripts.dashboard._get_store")
    def test_invalid_pr_index(self, mock_store_fn):
        """無効なPRインデックスでエラー"""
        store = MagicMock()
        mock_store_fn.return_value = store
        state = {
            "workflow_id": "wf-test001",
            "pull_requests": [
                {"repo_name": "Backend", "number": 123, "status": "reviewing"},
            ],
        }
        store.load_workflow.return_value = state

        result = handle_pr_review_action("wf-test001", 5, "mark_complete")

        assert result["success"] is False

    @patch("scripts.dashboard._get_store")
    def test_workflow_not_found(self, mock_store_fn):
        """ワークフローが見つからない場合のエラー"""
        store = MagicMock()
        mock_store_fn.return_value = store
        store.load_workflow.return_value = None

        result = handle_pr_review_action("wf-nonexist", 0, "mark_complete")

        assert result["success"] is False


# =========================================================================
# Prompt API テスト
# =========================================================================


class TestPromptAPI:
    """ダッシュボードの Prompt API ハンドラのテスト"""

    def _make_handler(self):
        """テスト用の DashboardHandler モックを作成"""
        from io import BytesIO
        handler = MagicMock(spec=DashboardHandler)
        handler._send_json_response = DashboardHandler._send_json_response.__get__(handler)
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def _parse_response(self, handler) -> dict:
        """wfile に書かれたJSONレスポンスをパースする"""
        handler.wfile.seek(0)
        return json.loads(handler.wfile.read().decode("utf-8"))

    def test_prompts_list_api(self):
        """GET /api/prompts がプロンプト一覧を返す"""
        handler = self._make_handler()
        handler._handle_prompts_list = DashboardHandler._handle_prompts_list.__get__(handler)
        handler._handle_prompts_list()
        data = self._parse_response(handler)
        assert data["success"] is True
        assert isinstance(data["data"], list)
        assert len(data["data"]) >= 1
        ids = [p["id"] for p in data["data"]]
        assert "phase2.task_research" in ids

    def test_prompt_get_api(self):
        """GET /api/prompts/<id> がテンプレート内容を返す"""
        handler = self._make_handler()
        handler._handle_prompt_get = DashboardHandler._handle_prompt_get.__get__(handler)
        handler._handle_prompt_get("phase2.task_research")
        data = self._parse_response(handler)
        assert data["success"] is True
        assert "{task_url}" in data["data"]["content"]

    def test_prompt_get_api_not_found(self):
        """GET /api/prompts/<id> で未知IDが404を返す"""
        handler = self._make_handler()
        handler._handle_prompt_get = DashboardHandler._handle_prompt_get.__get__(handler)
        handler._handle_prompt_get("nonexistent.prompt")
        data = self._parse_response(handler)
        assert data["success"] is False

    def test_prompt_save_api(self, tmp_path, monkeypatch):
        """POST /api/prompts/<id> がファイルを保存する"""
        # テスト用の一時ファイルとレジストリを設定
        test_file = tmp_path / "test.md"
        test_file.write_text("{task_url} テスト", encoding="utf-8")

        import hokusai.prompts.loader as loader
        original_registry = loader._registry
        original_dir = loader._PROMPTS_DIR
        monkeypatch.setattr(loader, "_PROMPTS_DIR", tmp_path)
        monkeypatch.setattr(loader, "_registry", [
            {"id": "test.prompt", "file": "test.md", "variables": ["task_url"], "title": "Test", "kind": "prompt"},
        ])

        try:
            handler = self._make_handler()
            handler._handle_prompt_save = DashboardHandler._handle_prompt_save.__get__(handler)
            handler._read_json_body = MagicMock(return_value={"content": "新しい {task_url} テスト"})
            handler._handle_prompt_save("test.prompt")
            data = self._parse_response(handler)
            assert data["success"] is True
            assert test_file.read_text(encoding="utf-8") == "新しい {task_url} テスト"
        finally:
            loader._registry = original_registry
            loader._PROMPTS_DIR = original_dir

    def test_prompt_save_api_validation_error(self, tmp_path, monkeypatch):
        """POST /api/prompts/<id> でバリデーションエラー時に400を返す"""
        test_file = tmp_path / "test.md"
        test_file.write_text("{task_url} テスト", encoding="utf-8")

        import hokusai.prompts.loader as loader
        original_registry = loader._registry
        original_dir = loader._PROMPTS_DIR
        monkeypatch.setattr(loader, "_PROMPTS_DIR", tmp_path)
        monkeypatch.setattr(loader, "_registry", [
            {"id": "test.prompt", "file": "test.md", "variables": ["task_url"], "title": "Test", "kind": "prompt"},
        ])

        try:
            handler = self._make_handler()
            handler._handle_prompt_save = DashboardHandler._handle_prompt_save.__get__(handler)
            handler._read_json_body = MagicMock(return_value={"content": "変数なしのテスト"})
            handler._handle_prompt_save("test.prompt")
            data = self._parse_response(handler)
            assert data["success"] is False
        finally:
            loader._registry = original_registry
            loader._PROMPTS_DIR = original_dir


class TestRenderPromptsPage:
    """render_prompts_page のテスト"""

    def test_returns_html_with_prompt_elements(self):
        """LLM指示文ページのHTMLが正しく生成される"""
        html = render_prompts_page()
        assert "LLM指示文" in html
        assert "promptList" in html
        assert "promptEditor" in html
        assert "savePrompt" in html
        assert "/api/prompts" in html
