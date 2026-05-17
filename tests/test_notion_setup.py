"""Notion 初期セットアップ（hokusai notion-setup）のテスト

setup.py の setup_notion_workspace() が、Workflows DB / Pull Requests DB を
正しい payload で作成するかを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.integrations.notion_dashboard.setup import (
    NotionSetupError,
    PULL_REQUESTS_DB_TITLE,
    REVIEW_ISSUES_DB_TITLE,
    WORKFLOWS_DB_TITLE,
    scaffold_notion_workspace,
    setup_notion_workspace,
)


class _RecordingClient:
    """NotionAPIClient の最小モック。create_database を記録"""

    def __init__(
        self,
        *,
        workflows_id: str = "wf-db-id",
        pr_id: str = "pr-db-id",
        review_issues_id: str = "ri-db-id",
        fail_on: str | None = None,
    ):
        self.calls: list[tuple[str, dict]] = []
        self._workflows_id = workflows_id
        self._pr_id = pr_id
        self._review_issues_id = review_issues_id
        self._fail_on = fail_on

    def create_database(self, payload: dict) -> dict:
        self.calls.append(("create_database", payload))
        title = (payload.get("title") or [{}])[0].get("text", {}).get("content", "")
        if self._fail_on == "workflows" and "Workflows" in title:
            raise RuntimeError("workflows db creation failed")
        if self._fail_on == "pull_requests" and "Pull Requests" in title:
            raise RuntimeError("pr db creation failed")
        if self._fail_on == "review_issues" and "Review Issues" in title:
            raise RuntimeError("review issues db creation failed")
        if "Workflows" in title:
            return {"id": self._workflows_id}
        if "Pull Requests" in title:
            return {"id": self._pr_id}
        return {"id": self._review_issues_id}


# ---------------------------------------------------------------------------
# 入力検証
# ---------------------------------------------------------------------------


def test_setup_rejects_empty_token():
    with pytest.raises(NotionSetupError):
        setup_notion_workspace("", "page-id")


def test_setup_rejects_empty_parent_page_id():
    with pytest.raises(NotionSetupError):
        setup_notion_workspace("token", "")


# ---------------------------------------------------------------------------
# 正常系: 3 つのリソース作成
# ---------------------------------------------------------------------------


def test_setup_creates_three_resources_in_order():
    client = _RecordingClient()
    result = setup_notion_workspace(
        "token", "parent-page-id", api_client=client
    )

    actions = [c[0] for c in client.calls]
    # Workflows DB → Pull Requests DB → Review Issues DB の順
    assert actions == ["create_database", "create_database", "create_database"]
    assert result["workflows_db_id"] == "wf-db-id"
    assert result["pull_requests_db_id"] == "pr-db-id"
    assert result["review_issues_db_id"] == "ri-db-id"


def test_setup_workflows_db_payload_includes_description_warning():
    """Workflows DB 作成時に description（手動編集抑止の警告文）が含まれる"""
    client = _RecordingClient()
    setup_notion_workspace("token", "parent", api_client=client)

    wf_payload = client.calls[0][1]
    assert "description" in wf_payload
    text = wf_payload["description"][0]["text"]["content"]
    assert "HOKUSAI が自動管理する DB" in text
    assert "Business Owner" in text  # 人間入力可プロパティの言及


def test_setup_pr_db_payload_includes_description_warning():
    """Pull Requests DB 作成時に description が含まれる"""
    client = _RecordingClient()
    setup_notion_workspace("token", "parent", api_client=client)

    pr_payload = client.calls[1][1]
    assert "description" in pr_payload
    text = pr_payload["description"][0]["text"]["content"]
    assert "HOKUSAI が自動管理する DB" in text
    assert "Phase 8a" in text  # PR 作成タイミングの言及


def test_setup_workflows_db_payload_has_required_properties():
    client = _RecordingClient()
    setup_notion_workspace("token", "parent", api_client=client)

    wf_payload = client.calls[0][1]
    title = wf_payload["title"][0]["text"]["content"]
    assert title == WORKFLOWS_DB_TITLE
    assert wf_payload["parent"] == {
        "type": "page_id",
        "page_id": "parent",
    }
    props = wf_payload["properties"]

    # 主要プロパティの存在確認
    for required in [
        "Name",
        "Workflow ID",
        "Status",
        "Current Phase",
        "Current Phase Name",
        "Waiting Reason",
        "Next Action",
        "Business Owner",
        "Tech Lead",
        "GitLab MR",
        "Research Page",
        "Design Page",
        "Plan Page",
        "Started At",
        "Completed At",
        "Last Updated",
        "Last Sync",
        "Sync Errors",
        "Error Summary",
        "Operator",  # Issue #21 / v0.4.8
    ]:
        assert required in props, f"missing property: {required}"


def test_setup_workflows_status_select_has_six_options():
    """Status の Select options が 6 件揃っている"""
    client = _RecordingClient()
    setup_notion_workspace("token", "parent", api_client=client)

    wf_payload = client.calls[0][1]
    options = wf_payload["properties"]["Status"]["select"]["options"]
    names = [o["name"] for o in options]
    assert set(names) == {
        "Ready", "Running", "Waiting for Human", "Failed", "Done", "Canceled"
    }


def test_setup_workflows_waiting_reason_select_has_eight_options():
    client = _RecordingClient()
    setup_notion_workspace("token", "parent", api_client=client)

    wf_payload = client.calls[0][1]
    options = wf_payload["properties"]["Waiting Reason"]["select"]["options"]
    names = [o["name"] for o in options]
    assert "branch_hygiene" in names
    assert "cross_review_blocked" in names
    assert len(names) == 8


def test_setup_pr_db_payload_includes_workflow_relation():
    """PR DB の Workflow プロパティに Workflows DB の relation が含まれる"""
    client = _RecordingClient()
    setup_notion_workspace("token", "parent", api_client=client)

    pr_payload = client.calls[1][1]
    title = pr_payload["title"][0]["text"]["content"]
    assert title == PULL_REQUESTS_DB_TITLE
    relation = pr_payload["properties"]["Workflow"]["relation"]
    # Workflows DB の ID（client が返した値）が指定されている
    assert relation["database_id"] == "wf-db-id"
    assert "single_property" in relation


def test_setup_pr_db_has_required_properties():
    client = _RecordingClient()
    setup_notion_workspace("token", "parent", api_client=client)

    pr_payload = client.calls[1][1]
    props = pr_payload["properties"]
    for required in [
        "PR Number",
        "URL",
        "Repository",
        "Status",
        "Workflow",
        "Reviewer",
        "Created At",
        "Last Updated",
    ]:
        assert required in props, f"missing PR DB property: {required}"


def test_setup_pr_db_status_select_has_five_options():
    client = _RecordingClient()
    setup_notion_workspace("token", "parent", api_client=client)
    pr_payload = client.calls[1][1]
    names = [
        o["name"]
        for o in pr_payload["properties"]["Status"]["select"]["options"]
    ]
    assert set(names) == {"Draft", "Open", "Approved", "Merged", "Closed"}


# ---------------------------------------------------------------------------
# 失敗系
# ---------------------------------------------------------------------------


def test_setup_raises_when_workflows_db_fails():
    client = _RecordingClient(fail_on="workflows")
    with pytest.raises(NotionSetupError, match="Workflows DB"):
        setup_notion_workspace("token", "parent", api_client=client)
    # 後続は呼ばれない
    actions = [c[0] for c in client.calls]
    assert actions == ["create_database"]


def test_setup_raises_when_pr_db_fails():
    client = _RecordingClient(fail_on="pull_requests")
    with pytest.raises(NotionSetupError, match="Pull Requests DB"):
        setup_notion_workspace("token", "parent", api_client=client)


def test_setup_raises_when_response_missing_id(monkeypatch):
    """API レスポンスに id が無いケース"""

    class _BadClient:
        def create_database(self, payload):
            return {}  # id なし

    with pytest.raises(NotionSetupError, match="id が含まれません"):
        setup_notion_workspace("token", "parent", api_client=_BadClient())


# ---------------------------------------------------------------------------
# CLI ハンドラ
# ---------------------------------------------------------------------------


def test_cli_handler_returns_one_when_token_missing(capsys, monkeypatch):
    from hokusai.cli_main import _handle_notion_setup

    monkeypatch.delenv("HOKUSAI_NOTION_API_TOKEN", raising=False)

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "p"

    rc = _handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "設定されていません" in out


# ---------------------------------------------------------------------------
# notion-migrate-schema ハンドラ（Issue #21 / v0.4.8、Copilot レビュー対応）
# ---------------------------------------------------------------------------


class _MigrateArgs:
    """notion-migrate-schema 用の最小 args オブジェクト（テスト用）。"""

    def __init__(
        self,
        *,
        workflows_db_id=None,
        api_token_env=None,
        dry_run=False,
    ):
        self.workflows_db_id = workflows_db_id
        self.api_token_env = api_token_env
        self.dry_run = dry_run


def test_migrate_handler_dry_run_skips_api_call(capsys, monkeypatch):
    """--dry-run は API を呼ばずに計画のみ表示し、token 未設定でも成功する。"""
    from hokusai import cli_main

    monkeypatch.delenv("HOKUSAI_NOTION_API_TOKEN", raising=False)
    monkeypatch.setenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", "wf-db-123")
    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(dry_run=True), None,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "wf-db-123" in out
    assert "Operator" in out
    assert "dry-run" in out


def test_migrate_handler_dry_run_uses_explicit_db_id(capsys, monkeypatch):
    """--workflows-db-id 明示時はその ID を使う（env 不要）。"""
    from hokusai import cli_main

    monkeypatch.delenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", raising=False)
    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(workflows_db_id="explicit-db", dry_run=True), None,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "explicit-db" in out


def test_migrate_handler_resolves_default_workflows_db_env(capsys, monkeypatch):
    """profile config 未指定でも HOKUSAI_NOTION_WORKFLOWS_DB_ID 既定 env を確認する。

    Copilot レビュー 1 回目 #4 対応: profile 無し利用者でも既定 env を fallback
    として使えるようにする。
    """
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", "from-default-env")
    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(dry_run=True), None,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "from-default-env" in out


def test_migrate_handler_fails_when_no_db_id_resolved(capsys, monkeypatch):
    """workflows_db_id が CLI / env どちらでも解決できない場合は 1 を返す。"""
    from hokusai import cli_main

    monkeypatch.delenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", raising=False)
    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(dry_run=True), None,
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "Workflows DB ID が解決できません" in out


def test_migrate_handler_calls_update_database_on_success(capsys, monkeypatch):
    """非 dry-run で update_database に Operator プロパティ payload が渡る。

    Copilot レビュー 1 回目 #7 対応: handler の正常系パスを直接検証する。
    """
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")
    monkeypatch.setenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", "wf-db-target")

    captured: dict = {}

    class _FakeAPI:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def update_database(self, database_id, payload):
            captured["database_id"] = database_id
            captured["payload"] = payload
            return {"id": database_id, "object": "database"}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI,
    )
    rc = cli_main._handle_notion_migrate_schema(_MigrateArgs(), None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "更新しました" in out
    assert captured["database_id"] == "wf-db-target"
    assert "Operator" in captured["payload"]["properties"]


def test_migrate_handler_fails_when_token_missing_non_dry_run(capsys, monkeypatch):
    """非 dry-run で token 未設定は 1 を返す（dry-run は token 不要、こちらは必要）。"""
    from hokusai import cli_main

    monkeypatch.delenv("HOKUSAI_NOTION_API_TOKEN", raising=False)
    monkeypatch.setenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", "wf-db-target")
    rc = cli_main._handle_notion_migrate_schema(_MigrateArgs(), None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "設定されていません" in out


def test_migrate_handler_uses_profile_config_token_env_and_db_id_env(
    capsys, monkeypatch,
):
    """profile config が指す env 変数名で token / DB ID を解決する。"""
    from hokusai import cli_main

    monkeypatch.setenv("COMPANY_A_NOTION_API_TOKEN", "company-a-secret")
    monkeypatch.setenv("COMPANY_A_NOTION_WORKFLOWS_DB_ID", "company-a-wf-db")

    class _NDConfig:
        api_token_env = "COMPANY_A_NOTION_API_TOKEN"
        workflows_db_id_env = "COMPANY_A_NOTION_WORKFLOWS_DB_ID"

    class _Config:
        notion_dashboard = _NDConfig()

    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(dry_run=True), _Config(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "company-a-wf-db" in out


def test_migrate_handler_rejects_invalid_cli_api_token_env(capsys, monkeypatch):
    """--api-token-env に不正な env 変数名（空白 / `;` 等）が来たら即 1 を返す。

    Copilot レビュー 3 回目 #3 対応: notion-setup と同等の検証を行う。
    """
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", "wf-db-target")
    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(api_token_env="BAD NAME;rm -rf /", dry_run=True),
        None,
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "不正な env 変数名" in out


class TestNotionMigrateSchemaParser:
    """notion-migrate-schema の parser-level test。

    Copilot レビュー 5 回目 #2 対応:
    サブパーサの `--dry-run` に `default=argparse.SUPPRESS` を付けて
    トップレベル値を温存している。この挙動が将来 default 変更で
    silent regression しないよう、3 パターンを parser レベルで検証する。
    """

    def test_global_dry_run_before_subcommand(self):
        """`hokusai --dry-run notion-migrate-schema` で args.dry_run=True。"""
        from hokusai.cli_main import _build_parser

        parser, _, _ = _build_parser()
        args = parser.parse_args(["--dry-run", "notion-migrate-schema"])
        assert getattr(args, "dry_run", False) is True

    def test_subcommand_dry_run_after_subcommand(self):
        """`hokusai notion-migrate-schema --dry-run` で args.dry_run=True。"""
        from hokusai.cli_main import _build_parser

        parser, _, _ = _build_parser()
        args = parser.parse_args(["notion-migrate-schema", "--dry-run"])
        assert getattr(args, "dry_run", False) is True

    def test_no_dry_run_defaults_to_false(self):
        """`hokusai notion-migrate-schema` のみなら args.dry_run=False。"""
        from hokusai.cli_main import _build_parser

        parser, _, _ = _build_parser()
        args = parser.parse_args(["notion-migrate-schema"])
        assert getattr(args, "dry_run", False) is False


def test_migrate_handler_rejects_whitespace_workflows_db_id(capsys, monkeypatch):
    """env / CLI 由来の workflows_db_id が空白のみなら未設定として扱う。

    Copilot レビュー 4 回目 #4 対応: 単純な truthiness check では `   ` が
    通過してしまい `/databases/   ` を呼びかねないため、strip 後に再判定する。
    """
    from hokusai import cli_main

    # env 経由の空白のみ値
    monkeypatch.setenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", "   \t  ")
    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(dry_run=True), None,
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "解決できません" in out

    # CLI 明示の空白のみ値
    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(workflows_db_id="   ", dry_run=True), None,
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "解決できません" in out


def test_migrate_handler_rejects_whitespace_api_token(capsys, monkeypatch):
    """非 dry-run で token が空白のみなら未設定として扱う。

    Copilot レビュー 4 回目 #3 対応: API に invalid token を送って
    紛らわしい 401 を出す前にローカルで弾く。
    """
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "   \n")
    monkeypatch.setenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", "wf-db-target")
    rc = cli_main._handle_notion_migrate_schema(_MigrateArgs(), None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "設定されていません" in out


def test_migrate_handler_falls_back_when_profile_config_env_invalid(
    capsys, monkeypatch,
):
    """profile config の env 名が不正なら警告して既定にフォールバックし、続行する。

    Copilot レビュー 3 回目 #3 対応: notion-setup と同じ `_pick_env_name` 方針。
    """
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID", "fallback-wf-db")

    class _NDConfig:
        api_token_env = "BAD;TOKEN"  # 不正
        workflows_db_id_env = "BAD WF NAME"  # 不正

    class _Config:
        notion_dashboard = _NDConfig()

    rc = cli_main._handle_notion_migrate_schema(
        _MigrateArgs(dry_run=True), _Config(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    # 警告メッセージが出ていることと、既定 env で解決された ID が使われていること
    assert "不正な env 変数名" in out
    assert "fallback-wf-db" in out


def test_cli_handler_prints_export_lines_on_success(capsys, monkeypatch):
    """成功時に export コマンド例を出力する"""
    from hokusai import cli_main
    from hokusai.integrations import notion_dashboard as nd

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {
            "workflows_db_id": "wf123",
            "pull_requests_db_id": "pr456",
        }

    monkeypatch.setattr(nd, "setup_notion_workspace", _fake_setup)
    # cli_main は from . import で取り込んでいるため、そちらも差し替える
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "parent"

    rc = cli_main._handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "wf123" in out
    assert "pr456" in out
    assert "HOKUSAI_NOTION_WORKFLOWS_DB_ID" in out
    assert "HOKUSAI_NOTION_PR_DB_ID" in out


def test_cli_handler_returns_one_on_setup_error(capsys, monkeypatch):
    from hokusai import cli_main
    from hokusai.integrations.notion_dashboard import NotionSetupError

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        raise NotionSetupError("integration not connected")

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "parent"

    rc = cli_main._handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "セットアップ失敗" in out


# ---------------------------------------------------------------------------
# detect_shell_rc
# ---------------------------------------------------------------------------


def test_detect_shell_rc_zsh(monkeypatch):
    from hokusai.integrations.notion_dashboard.setup import detect_shell_rc

    monkeypatch.setenv("SHELL", "/bin/zsh")
    rc = detect_shell_rc()
    assert rc.name == ".zshrc"


def test_detect_shell_rc_bash(monkeypatch):
    from hokusai.integrations.notion_dashboard.setup import detect_shell_rc

    monkeypatch.setenv("SHELL", "/usr/local/bin/bash")
    rc = detect_shell_rc()
    assert rc.name == ".bashrc"


def test_detect_shell_rc_unknown_falls_back_to_profile(monkeypatch):
    from hokusai.integrations.notion_dashboard.setup import detect_shell_rc

    monkeypatch.setenv("SHELL", "/bin/fish")
    rc = detect_shell_rc()
    assert rc.name == ".profile"


def test_detect_shell_rc_missing_shell_env(monkeypatch):
    from hokusai.integrations.notion_dashboard.setup import detect_shell_rc

    monkeypatch.delenv("SHELL", raising=False)
    rc = detect_shell_rc()
    assert rc.name == ".profile"


# ---------------------------------------------------------------------------
# persist_env_vars
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_ids():
    return {
        "workflows_db_id": "wf-id-12345",
        "pull_requests_db_id": "pr-id-67890",
    }


def test_persist_env_vars_appends_when_no_marker(tmp_path, sample_ids):
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    rc.write_text("# existing user content\nexport FOO=bar\n")

    result = persist_env_vars(rc, sample_ids)

    content = rc.read_text()
    assert result["action"] == "appended"
    # 既存内容が残っている
    assert "# existing user content" in content
    assert "export FOO=bar" in content
    # ブロックが追記されている
    assert "HOKUSAI Notion Dashboard" in content
    assert "wf-id-12345" in content
    assert "pr-id-67890" in content


def test_persist_env_vars_creates_backup(tmp_path, sample_ids):
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    original = "original content\n"
    rc.write_text(original)

    result = persist_env_vars(rc, sample_ids)
    backup_path = Path(result["backup_path"])

    assert backup_path.exists()
    assert backup_path.read_text() == original


def test_persist_env_vars_skips_backup_when_disabled(tmp_path, sample_ids):
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    rc.write_text("original\n")

    result = persist_env_vars(rc, sample_ids, backup=False)
    assert result["backup_path"] is None


def test_persist_env_vars_replaces_existing_block(tmp_path, sample_ids):
    """再実行時は既存マーカーブロックを置き換える（idempotent）"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    # 一度書き込み
    persist_env_vars(rc, sample_ids)

    # 別の値で再実行
    new_ids = {
        "workflows_db_id": "wf-NEW",
        "pull_requests_db_id": "pr-NEW",
    }
    result = persist_env_vars(rc, new_ids)

    assert result["action"] == "replaced"
    content = rc.read_text()
    # 古い ID は消えている
    assert "wf-id-12345" not in content
    assert "pr-id-67890" not in content
    # 新しい ID に書き換わっている
    assert "wf-NEW" in content
    assert "pr-NEW" in content
    # マーカーブロックは 1 つだけ
    assert content.count("HOKUSAI Notion Dashboard (managed by") == 1


def test_persist_env_vars_creates_file_when_missing(tmp_path, sample_ids):
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "subdir" / "new.zshrc"
    assert not rc.exists()

    result = persist_env_vars(rc, sample_ids)

    assert rc.exists()
    assert "wf-id-12345" in rc.read_text()
    assert result["action"] == "appended"
    assert result["backup_path"] is None  # 元ファイルが無いのでバックアップ無し


def test_persist_env_vars_preserves_existing_content_around_block(tmp_path, sample_ids):
    """既存の前後コンテンツを破壊しない"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    rc.write_text(
        "# top comment\nexport BEFORE=1\n"
        "# === HOKUSAI Notion Dashboard (managed by `hokusai notion-setup`) ===\n"
        "# Last updated: 2026-01-01\n"
        'export HOKUSAI_NOTION_WORKFLOWS_DB_ID="old-1"\n'
        'export HOKUSAI_NOTION_PR_DB_ID="old-2"\n'
        "# === END HOKUSAI Notion Dashboard ===\n"
        "export AFTER=2\n"
    )

    persist_env_vars(rc, sample_ids)
    content = rc.read_text()

    # 前後のユーザーコンテンツが残っている
    assert "export BEFORE=1" in content
    assert "export AFTER=2" in content
    assert "# top comment" in content
    # 古い値は消え、新しい値に
    assert "old-1" not in content
    assert "wf-id-12345" in content


# ---------------------------------------------------------------------------
# CLI ハンドラ: --persist
# ---------------------------------------------------------------------------


def test_cli_handler_persist_writes_to_rc(capsys, monkeypatch, tmp_path):
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {
            "workflows_db_id": "wfNEW",
            "pull_requests_db_id": "prNEW",
        }

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    rc = tmp_path / "rc"

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "parent"
        persist = True
        shell_rc = str(rc)
        no_backup = True

    rc_code = cli_main._handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert rc_code == 0
    assert "追記しました" in out or "更新しました" in out
    # rc ファイルに ID が書き込まれている
    assert rc.exists()
    content = rc.read_text()
    assert "wfNEW" in content
    assert "prNEW" in content


def test_cli_handler_persist_disabled_shows_hint(capsys, monkeypatch):
    """--persist 無しの場合、ヒントが表示される"""
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {
            "workflows_db_id": "x",
            "pull_requests_db_id": "y",
        }

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "parent"
        persist = False
        shell_rc = None
        no_backup = False

    cli_main._handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert "--persist" in out


# ---------------------------------------------------------------------------
# persist_env_vars: profile-aware (v0.4.1)
# ---------------------------------------------------------------------------


def test_persist_env_vars_uses_profile_marker(tmp_path, sample_ids):
    """profile_name 指定時は profile マーカー / カスタム env 名が使われる"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    result = persist_env_vars(
        rc,
        sample_ids,
        workflows_env_name="HOKUSAI_NOTION_WORKFLOWS_DB_ID_FOO",
        pull_requests_env_name="HOKUSAI_NOTION_PR_DB_ID_FOO",
        profile_name="foo",
    )

    assert result["action"] == "appended"
    content = rc.read_text()
    # profile マーカー
    assert "profile=foo" in content
    # カスタム env 名
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID_FOO="wf-id-12345"' in content
    assert 'export HOKUSAI_NOTION_PR_DB_ID_FOO="pr-id-67890"' in content
    # 既定 env 名は書かれない
    assert "HOKUSAI_NOTION_WORKFLOWS_DB_ID=" not in content
    assert "HOKUSAI_NOTION_PR_DB_ID=" not in content


def test_persist_env_vars_keeps_separate_profile_blocks(tmp_path, sample_ids):
    """同じ rc に複数 profile の env を並列で書ける"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"

    # profile=foo を書き込み
    persist_env_vars(
        rc,
        sample_ids,
        workflows_env_name="WF_FOO",
        pull_requests_env_name="PR_FOO",
        profile_name="foo",
    )
    # profile=bar を書き込み（既存ブロックは置換されない）
    persist_env_vars(
        rc,
        {"workflows_db_id": "wf-bar", "pull_requests_db_id": "pr-bar"},
        workflows_env_name="WF_BAR",
        pull_requests_env_name="PR_BAR",
        profile_name="bar",
    )

    content = rc.read_text()
    # 両方のブロックが並存
    assert "profile=foo" in content
    assert "profile=bar" in content
    # 両方の env が残る
    assert 'export WF_FOO="wf-id-12345"' in content
    assert 'export WF_BAR="wf-bar"' in content
    # マーカーは別ブロック（合計 2）
    assert content.count("HOKUSAI Notion Dashboard (managed by") == 2


def test_persist_env_vars_replaces_same_profile_block(tmp_path, sample_ids):
    """同じ profile 名で再実行すると同じブロックを置換する"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    persist_env_vars(
        rc,
        sample_ids,
        workflows_env_name="WF_FOO",
        pull_requests_env_name="PR_FOO",
        profile_name="foo",
    )
    result = persist_env_vars(
        rc,
        {"workflows_db_id": "wf-NEW", "pull_requests_db_id": "pr-NEW"},
        workflows_env_name="WF_FOO",
        pull_requests_env_name="PR_FOO",
        profile_name="foo",
    )

    assert result["action"] == "replaced"
    content = rc.read_text()
    assert "wf-id-12345" not in content
    assert "wf-NEW" in content
    # profile=foo ブロックは 1 つだけ
    assert content.count("profile=foo") == 2  # begin + end marker


def test_persist_env_vars_none_profile_keeps_legacy_marker(tmp_path, sample_ids):
    """profile_name=None なら従来マーカー / 既定 env 名（後方互換）"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    persist_env_vars(rc, sample_ids, profile_name=None)

    content = rc.read_text()
    # 従来マーカー（profile= 文字列を含まない）
    assert "HOKUSAI Notion Dashboard (managed by `hokusai notion-setup`) ===" in content
    assert "profile=" not in content
    # 既定 env 名
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID="wf-id-12345"' in content
    assert 'export HOKUSAI_NOTION_PR_DB_ID="pr-id-67890"' in content


def test_persist_env_vars_profile_block_coexists_with_legacy(tmp_path, sample_ids):
    """既存の legacy ブロック（profile 名なし）と profile ブロックが共存できる"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    # 先に legacy ブロックを書き込み
    persist_env_vars(rc, sample_ids, profile_name=None)
    # profile ブロックを追加
    persist_env_vars(
        rc,
        {"workflows_db_id": "wf-foo", "pull_requests_db_id": "pr-foo"},
        workflows_env_name="WF_FOO",
        pull_requests_env_name="PR_FOO",
        profile_name="foo",
    )

    content = rc.read_text()
    # legacy と profile の 2 ブロックが並存
    assert content.count("HOKUSAI Notion Dashboard (managed by") == 2
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID="wf-id-12345"' in content  # legacy
    assert 'export WF_FOO="wf-foo"' in content  # profile


# ---------------------------------------------------------------------------
# CLI ハンドラ: env 名解決ロジック (Issue #17 / v0.4.1)
# ---------------------------------------------------------------------------


def _make_notion_dashboard_config(
    *,
    api_token_env: str | None = None,
    workflows_db_id_env: str | None = None,
    pull_requests_db_id_env: str | None = None,
):
    """テスト用に notion_dashboard config を持つダミー config オブジェクト"""
    from types import SimpleNamespace

    nd = SimpleNamespace()
    if api_token_env is not None:
        nd.api_token_env = api_token_env
    if workflows_db_id_env is not None:
        nd.workflows_db_id_env = workflows_db_id_env
    if pull_requests_db_id_env is not None:
        nd.pull_requests_db_id_env = pull_requests_db_id_env
    return SimpleNamespace(notion_dashboard=nd)


def test_cli_handler_uses_default_env_when_no_profile_no_explicit(
    capsys, monkeypatch
):
    """profile 未指定 + --api-token-env 未指定 → 既定 HOKUSAI_NOTION_API_TOKEN を読む"""
    from hokusai import cli_main

    monkeypatch.delenv("HOKUSAI_NOTION_API_TOKEN", raising=False)

    class _Args:
        api_token_env = None  # CLI 未指定（v0.4.1 default が None）
        parent_page_id = "p"

    rc = cli_main._handle_notion_setup(_Args(), config=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "HOKUSAI_NOTION_API_TOKEN が設定されていません" in out


def test_cli_handler_uses_profile_config_api_token_env(
    capsys, monkeypatch, tmp_path
):
    """profile config に api_token_env がある → その env を読む"""
    from hokusai import cli_main

    # profile config 由来の env のみ設定、既定 env は意図的に未設定
    monkeypatch.delenv("HOKUSAI_NOTION_API_TOKEN", raising=False)
    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN_FOO", "secret-foo")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {"workflows_db_id": "wf", "pull_requests_db_id": "pr"}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    config = _make_notion_dashboard_config(
        api_token_env="HOKUSAI_NOTION_API_TOKEN_FOO",
        workflows_db_id_env="HOKUSAI_NOTION_WORKFLOWS_DB_ID_FOO",
        pull_requests_db_id_env="HOKUSAI_NOTION_PR_DB_ID_FOO",
    )

    class _Args:
        api_token_env = None
        parent_page_id = "p"
        profile = "foo"
        persist = False
        shell_rc = None
        no_backup = False

    rc = cli_main._handle_notion_setup(_Args(), config=config)
    out = capsys.readouterr().out
    assert rc == 0
    # config の env 名で読み込み成功 → export 出力もカスタム env 名
    assert "HOKUSAI_NOTION_API_TOKEN_FOO" in out
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID_FOO="wf"' in out
    assert 'export HOKUSAI_NOTION_PR_DB_ID_FOO="pr"' in out
    # 既定 env 名は使われない
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID="' not in out
    assert 'export HOKUSAI_NOTION_PR_DB_ID="' not in out


def test_cli_handler_explicit_api_token_env_wins_over_profile_config(
    capsys, monkeypatch
):
    """--api-token-env 明示指定が profile config よりも優先される"""
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN_EXPLICIT", "secret-explicit")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {"workflows_db_id": "wf", "pull_requests_db_id": "pr"}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    config = _make_notion_dashboard_config(
        api_token_env="HOKUSAI_NOTION_API_TOKEN_FROM_CONFIG",
    )

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN_EXPLICIT"  # 明示指定
        parent_page_id = "p"
        profile = "foo"
        persist = False
        shell_rc = None
        no_backup = False

    rc = cli_main._handle_notion_setup(_Args(), config=config)
    out = capsys.readouterr().out
    assert rc == 0
    assert "HOKUSAI_NOTION_API_TOKEN_EXPLICIT" in out
    # config 側 env 名は採用されない
    assert "HOKUSAI_NOTION_API_TOKEN_FROM_CONFIG" not in out


def test_cli_handler_falls_back_to_default_when_profile_config_missing_field(
    capsys, monkeypatch
):
    """profile 指定 + config.notion_dashboard.api_token_env 未定義 → 既定値"""
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret-default")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {"workflows_db_id": "wf", "pull_requests_db_id": "pr"}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    # api_token_env は config に無いが、workflows_db_id_env はある
    config = _make_notion_dashboard_config(
        workflows_db_id_env="HOKUSAI_NOTION_WORKFLOWS_DB_ID_BAR",
    )

    class _Args:
        api_token_env = None
        parent_page_id = "p"
        profile = "bar"
        persist = False
        shell_rc = None
        no_backup = False

    rc = cli_main._handle_notion_setup(_Args(), config=config)
    out = capsys.readouterr().out
    assert rc == 0
    # token は既定 env で読まれる
    assert "HOKUSAI_NOTION_API_TOKEN" in out
    # workflows env は config 側
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID_BAR="wf"' in out
    # PR env は未指定なので既定値
    assert 'export HOKUSAI_NOTION_PR_DB_ID="pr"' in out


def test_persist_env_vars_rejects_invalid_profile_name(tmp_path, sample_ids):
    """profile_name に改行・空白・制御文字を含む値を渡すと ValueError"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"

    # 改行（注入リスク）
    with pytest.raises(ValueError, match="invalid profile_name"):
        persist_env_vars(
            rc, sample_ids,
            profile_name="foo\nexport EVIL=1",
        )
    # 空白
    with pytest.raises(ValueError, match="invalid profile_name"):
        persist_env_vars(rc, sample_ids, profile_name="bad name")
    # 大文字（registry 規則と一致）
    with pytest.raises(ValueError, match="invalid profile_name"):
        persist_env_vars(rc, sample_ids, profile_name="BAD_CASE")
    # 数字始まり
    with pytest.raises(ValueError, match="invalid profile_name"):
        persist_env_vars(rc, sample_ids, profile_name="1foo")
    # 空文字
    with pytest.raises(ValueError, match="invalid profile_name"):
        persist_env_vars(rc, sample_ids, profile_name="")
    # マーカー行を閉じる文字を含む
    with pytest.raises(ValueError, match="invalid profile_name"):
        persist_env_vars(rc, sample_ids, profile_name="foo)===")


def test_persist_env_vars_accepts_valid_profile_names(tmp_path, sample_ids):
    """profile_name として妥当な値は受け入れられる"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"
    # 標準的な命名（英小文字 + 数字 + ハイフン / アンダースコア）
    persist_env_vars(rc, sample_ids, profile_name="company-a")
    persist_env_vars(rc, sample_ids, profile_name="hokusai")
    persist_env_vars(rc, sample_ids, profile_name="proj_01")
    # 例外が出なければ OK


def test_persist_env_vars_rejects_invalid_env_name(tmp_path, sample_ids):
    """シェル変数名として不正な workflows_env_name を渡すと ValueError"""
    from hokusai.integrations.notion_dashboard.setup import persist_env_vars

    rc = tmp_path / "test.zshrc"

    # 空白を含む不正値
    with pytest.raises(ValueError, match="invalid env variable name"):
        persist_env_vars(
            rc, sample_ids,
            workflows_env_name="BAD NAME",
        )
    # 改行を含む不正値（コマンド注入リスク）
    with pytest.raises(ValueError, match="invalid env variable name"):
        persist_env_vars(
            rc, sample_ids,
            pull_requests_env_name="OK_NAME\nexport EVIL=1",
        )
    # 数字始まり
    with pytest.raises(ValueError, match="invalid env variable name"):
        persist_env_vars(
            rc, sample_ids,
            workflows_env_name="1BAD",
        )
    # 空文字
    with pytest.raises(ValueError, match="invalid env variable name"):
        persist_env_vars(
            rc, sample_ids,
            workflows_env_name="",
        )


def test_is_valid_env_var_name():
    """is_valid_env_var_name の真偽パターン"""
    from hokusai.integrations.notion_dashboard import is_valid_env_var_name

    # OK
    assert is_valid_env_var_name("HOKUSAI_NOTION_API_TOKEN")
    assert is_valid_env_var_name("_PRIVATE")
    assert is_valid_env_var_name("a")
    assert is_valid_env_var_name("VAR123")

    # NG
    assert not is_valid_env_var_name("")
    assert not is_valid_env_var_name("1VAR")
    assert not is_valid_env_var_name("BAD NAME")
    assert not is_valid_env_var_name("BAD;NAME")
    assert not is_valid_env_var_name("BAD\nNAME")
    assert not is_valid_env_var_name(None)
    assert not is_valid_env_var_name(123)


def test_cli_handler_rejects_invalid_explicit_api_token_env(capsys):
    """--api-token-env に不正値が来たら 1 を返して中断する"""
    from hokusai import cli_main

    class _Args:
        api_token_env = "BAD NAME"
        parent_page_id = "p"
        profile = None

    rc = cli_main._handle_notion_setup(_Args(), config=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "不正な env 変数名" in out


def test_cli_handler_falls_back_when_config_env_name_invalid(
    capsys, monkeypatch, tmp_path
):
    """profile config の env 名が不正なら警告 + 既定値にフォールバック"""
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret-default")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {"workflows_db_id": "wf", "pull_requests_db_id": "pr"}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    # 不正な env 名（コマンド注入を試みた形）
    config = _make_notion_dashboard_config(
        api_token_env="BAD NAME",  # 空白
        workflows_db_id_env="WF\nexport EVIL=1",  # 改行
        pull_requests_db_id_env="1BAD",  # 数字始まり
    )

    class _Args:
        api_token_env = None
        parent_page_id = "p"
        profile = "bad"
        persist = False
        shell_rc = None
        no_backup = False

    return_code = cli_main._handle_notion_setup(_Args(), config=config)
    out = capsys.readouterr().out
    assert return_code == 0
    # 不正な env 名は警告として出力（3 件すべて）
    assert out.count("不正な env 変数名") == 3
    # 既定値にフォールバック（export 行が正規の env 名で出る）
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID="wf"' in out
    assert 'export HOKUSAI_NOTION_PR_DB_ID="pr"' in out
    # 不正値で実際の export 行（"<NAME>="<VALUE>"" フォーマット）が作られていない。
    # 警告メッセージ内に repr 表現として `'WF\nexport EVIL=1'` が現れるのは可
    # （改行はリテラル `\n` 2 文字としてエスケープされて表示される）
    assert 'export "1BAD"' not in out and "export 1BAD=" not in out
    # 注入された "export EVIL=1" が独立した行として出ない
    export_lines = [l for l in out.splitlines() if l.lstrip().startswith("export ")]
    assert all("EVIL" not in line for line in export_lines)


def test_cli_handler_persist_uses_profile_marker_and_custom_env_names(
    capsys, monkeypatch, tmp_path
):
    """--persist + profile 指定で、rc に profile マーカー + カスタム env 名が書かれる"""
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN_4HOKUSAI", "secret-4hokusai")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {"workflows_db_id": "wf-4hokusai", "pull_requests_db_id": "pr-4hokusai"}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    config = _make_notion_dashboard_config(
        api_token_env="HOKUSAI_NOTION_API_TOKEN_4HOKUSAI",
        workflows_db_id_env="HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI",
        pull_requests_db_id_env="HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI",
    )

    rc = tmp_path / "test.zshrc"

    class _Args:
        api_token_env = None
        parent_page_id = "p"
        profile = "hokusai"
        persist = True
        shell_rc = str(rc)
        no_backup = True

    return_code = cli_main._handle_notion_setup(_Args(), config=config)
    assert return_code == 0
    content = rc.read_text()
    # profile マーカーが書かれる
    assert "profile=hokusai" in content
    # カスタム env 名で書かれる
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI="wf-4hokusai"' in content
    assert 'export HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI="pr-4hokusai"' in content
    # 既定 env 名は書かれない
    assert 'export HOKUSAI_NOTION_WORKFLOWS_DB_ID="' not in content
    assert 'export HOKUSAI_NOTION_PR_DB_ID="' not in content


# ---------------------------------------------------------------------------
# scaffold_notion_workspace (Issue #25 / v0.4.3)
# ---------------------------------------------------------------------------


class _ScaffoldRecordingClient:
    """scaffold ロジック検証用の最小モック。

    list_block_children: 親ページの既存子要素を返す
    create_page: page を「作成」し、id を返却
    """

    def __init__(
        self,
        *,
        existing_pages: list[tuple[str, str]] | None = None,
        fail_on_titles: set[str] | None = None,
    ):
        """existing_pages: [(parent_id, title), ...] の既存ページ集合"""
        self.created_pages: list[dict] = []
        self.list_children_calls: list[str] = []
        # parent_id -> [(title, id), ...]
        self._existing: dict[str, list[tuple[str, str]]] = {}
        for parent_id, title in (existing_pages or []):
            self._existing.setdefault(parent_id, []).append((title, f"existing-{title}"))
        self._fail_on_titles = fail_on_titles or set()
        self._next_id = 0

    def list_block_children(
        self, block_id: str, *, start_cursor: str | None = None
    ) -> dict:
        # 単純化のため pagination は使わず 1 ページで返す
        self.list_children_calls.append(block_id)
        results = []
        for title, pid in self._existing.get(block_id, []):
            results.append({
                "type": "child_page",
                "id": pid,
                "child_page": {"title": title},
            })
        return {"results": results, "has_more": False, "next_cursor": None}

    def create_page(self, payload: dict) -> dict:
        self.created_pages.append(payload)
        title = payload["properties"]["title"][0]["text"]["content"]
        if title in self._fail_on_titles:
            raise RuntimeError(f"create_page failure for {title}")
        self._next_id += 1
        new_id = f"page-{self._next_id}"
        # 作成後は既存リストにも追加（同セッションで再作成しないため）
        parent_id = payload["parent"]["page_id"]
        self._existing.setdefault(parent_id, []).append((title, new_id))
        return {"id": new_id}


def test_scaffold_creates_full_tree_when_empty():
    """空の親ページに対して、4 ページ（ハブ + サブ 3）すべてを作成する"""
    client = _ScaffoldRecordingClient()
    result = scaffold_notion_workspace("token", "parent-page-id", api_client=client)

    created_titles = [c["title"] for c in result["created"]]
    assert "Documentation" in created_titles
    assert "議論" in created_titles
    assert "運用ガイド" in created_titles
    assert "要件定義" in created_titles
    assert len(result["created"]) == 4
    assert result["skipped"] == []


def test_scaffold_includes_icon_and_placeholder():
    """各作成ページに icon emoji と placeholder paragraph が含まれる"""
    client = _ScaffoldRecordingClient()
    scaffold_notion_workspace("token", "parent", api_client=client)

    # 全ページに icon があり、children に少なくとも 1 個の paragraph がある
    for payload in client.created_pages:
        assert payload["icon"]["type"] == "emoji"
        assert payload["icon"]["emoji"] in ("📚", "💬", "📖", "📋")
        children = payload["children"]
        assert len(children) >= 1
        assert children[0]["type"] == "paragraph"


def test_scaffold_payload_title_has_no_emoji_prefix():
    """Issue #27: 新仕様で create_page payload のタイトルから絵文字 prefix が消える。

    icon は Notion Create Page API のトップレベル `icon` フィールド
    （`{"type": "emoji", "emoji": "..."}`）に設定される。`properties` 配下では
    なくページ自体のメタデータ。Notion UI で title と icon が二重表示されない
    （v0.4.4 〜）。
    """
    client = _ScaffoldRecordingClient()
    scaffold_notion_workspace("token", "parent", api_client=client)

    # 全ページの title から絵文字 prefix が外れている
    for payload in client.created_pages:
        title = payload["properties"]["title"][0]["text"]["content"]
        assert not title.startswith(("📚", "💬", "📖", "📋")), (
            f"v0.4.4 では title から絵文字 prefix を外すべき: {title!r}"
        )
    # icon 側は引き続き絵文字
    icons = [p["icon"]["emoji"] for p in client.created_pages]
    assert "📚" in icons
    assert "💬" in icons
    assert "📖" in icons
    assert "📋" in icons


def test_scaffold_payload_uses_page_parent_title_shape():
    """create_page payload は page_id parent 仕様（properties.title が rich-text array 直接）。

    Notion Create Page API では page_id parent の場合
        properties.title = [rich_text, ...]
    でなければならない。DB 行用の properties.title = {"title": [...]} 形式を
    送ると実 API が 400 を返す。
    """
    client = _ScaffoldRecordingClient()
    scaffold_notion_workspace("token", "parent", api_client=client)

    assert client.created_pages, "ページが作成されているはず"
    for payload in client.created_pages:
        # parent は page_id 形式
        assert payload["parent"]["type"] == "page_id"
        # properties.title は rich-text array でなければならない（dict 不可）
        title_value = payload["properties"]["title"]
        assert isinstance(title_value, list), (
            "page_id parent では properties.title は rich-text array 必須"
        )
        assert title_value
        assert title_value[0]["type"] == "text"
        assert "content" in title_value[0]["text"]


def test_scaffold_skips_v0_4_3_legacy_hub_page():
    """v0.4.3 以前の旧タイトル（絵文字 prefix `📚 HOKUSAI Documentation`）の
    ハブが既存なら skip 検出する（v0.4.5 でも 2 世代後方互換維持）。
    """
    client = _ScaffoldRecordingClient(
        existing_pages=[("parent-id", "📚 HOKUSAI Documentation")]
    )
    result = scaffold_notion_workspace("token", "parent-id", api_client=client)

    skipped_titles = [s["title"] for s in result["skipped"]]
    assert "Documentation" in skipped_titles  # canonical で記録
    # サブ 3 ページは既存ハブ（legacy id）の下に新 canonical 名で作成される
    created_titles = [c["title"] for c in result["created"]]
    assert "議論" in created_titles
    assert "運用ガイド" in created_titles
    assert "要件定義" in created_titles
    # 既存ハブの id（v0.4.3 legacy title 由来）でサブを作る
    for payload in client.created_pages:
        assert payload["parent"]["page_id"] == "existing-📚 HOKUSAI Documentation"


def test_scaffold_skips_v0_4_4_legacy_hub_page():
    """v0.4.4 の旧タイトル（HOKUSAI prefix 付き `HOKUSAI Documentation`）の
    ハブが既存なら skip 検出する（v0.4.5 で新規追加された legacy 世代）。
    """
    client = _ScaffoldRecordingClient(
        existing_pages=[("parent-id", "HOKUSAI Documentation")]
    )
    result = scaffold_notion_workspace("token", "parent-id", api_client=client)

    skipped_titles = [s["title"] for s in result["skipped"]]
    assert "Documentation" in skipped_titles
    created_titles = [c["title"] for c in result["created"]]
    assert "議論" in created_titles
    assert "運用ガイド" in created_titles
    assert "要件定義" in created_titles
    for payload in client.created_pages:
        assert payload["parent"]["page_id"] == "existing-HOKUSAI Documentation"


def test_scaffold_skips_v0_4_4_legacy_subpages():
    """v0.4.4 の英語サブページタイトル（Discussions / Operation Guides /
    Requirements）が既存なら skip 検出する（2 世代後方互換）。
    """
    # ハブは新規作成、サブは全て v0.4.4 英語名で既存
    client = _ScaffoldRecordingClient()
    # 1 回目: ハブを作って、その配下に英語名サブを追加（v0.4.4 状態を再現）
    hub_response = client.create_page({
        "parent": {"type": "page_id", "page_id": "parent"},
        "icon": {"type": "emoji", "emoji": "📚"},
        "properties": {"title": [{"type": "text", "text": {"content": "Documentation"}}]},
        "children": [],
    })
    hub_id = hub_response["id"]
    for legacy_sub in ("Discussions", "Operation Guides", "Requirements"):
        client.create_page({
            "parent": {"type": "page_id", "page_id": hub_id},
            "icon": {"type": "emoji", "emoji": "💬"},
            "properties": {"title": [{"type": "text", "text": {"content": legacy_sub}}]},
            "children": [],
        })
    # 2 回目: v0.4.5 として再実行 → ハブも v0.4.4 サブも全て skip 検出
    initial_create_count = len(client.created_pages)
    result = scaffold_notion_workspace("token", "parent", api_client=client)

    # 2 回目では新規 create は発生しない
    assert len(client.created_pages) == initial_create_count
    skipped_titles = [s["title"] for s in result["skipped"]]
    assert "Documentation" in skipped_titles
    assert "議論" in skipped_titles
    assert "運用ガイド" in skipped_titles
    assert "要件定義" in skipped_titles


def test_scaffold_prefers_canonical_over_legacy_when_both_exist():
    """親ページに新旧両タイトルのハブが共存する場合、canonical 側を優先する。

    Copilot レビュー 2 回目（setup.py:376）対応: 旧実装は set 化された
    candidates の中で最初に見つかったページを返していたため、Notion API が
    legacy ページを先に返すと legacy hub 配下にサブが作られてしまっていた。
    新実装は走査中 canonical 完全一致を即返し、legacy は fallback として扱う。
    """

    class _ThreeGenerationsClient:
        """親直下に 3 世代（v0.4.3 / v0.4.4 / v0.4.5 canonical）が共存するモック"""

        def __init__(self):
            self.created_pages: list[dict] = []

        def list_block_children(self, block_id, *, start_cursor=None):
            if block_id == "parent":
                # v0.4.3, v0.4.4, v0.4.5(canonical) を順に返す
                return {
                    "results": [
                        {
                            "type": "child_page",
                            "id": "v043-hub-id",
                            "child_page": {"title": "📚 HOKUSAI Documentation"},
                        },
                        {
                            "type": "child_page",
                            "id": "v044-hub-id",
                            "child_page": {"title": "HOKUSAI Documentation"},
                        },
                        {
                            "type": "child_page",
                            "id": "canonical-hub-id",
                            "child_page": {"title": "Documentation"},
                        },
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            return {"results": [], "has_more": False, "next_cursor": None}

        def create_page(self, payload):
            self.created_pages.append(payload)
            title = payload["properties"]["title"][0]["text"]["content"]
            return {"id": f"new-{title}"}

    client = _ThreeGenerationsClient()
    result = scaffold_notion_workspace("token", "parent", api_client=client)

    # canonical 側 (canonical-hub-id) がハブとして選ばれる
    skipped_titles = [s["title"] for s in result["skipped"]]
    assert "Documentation" in skipped_titles
    skipped_hub = [s for s in result["skipped"] if s["title"] == "Documentation"]
    assert skipped_hub[0]["id"] == "canonical-hub-id"
    # サブは canonical-hub-id の下に作られる
    sub_payloads = [
        p for p in client.created_pages
        if p["properties"]["title"][0]["text"]["content"]
        in ("議論", "運用ガイド", "要件定義")
    ]
    assert len(sub_payloads) == 3
    for p in sub_payloads:
        assert p["parent"]["page_id"] == "canonical-hub-id", (
            f"サブの parent は canonical hub であるべき: {p}"
        )


def test_scaffold_rerun_is_idempotent_and_skips_all_four_pages():
    """同じクライアントで再実行すると 4 ページ全てが skip される（end-to-end idempotency）。

    1 回目: ハブ + 3 サブの計 4 ページを新規作成
    2 回目: 全 4 ページが既存検出されて skip
    """
    client = _ScaffoldRecordingClient()
    scaffold_notion_workspace("token", "parent", api_client=client)
    result = scaffold_notion_workspace("token", "parent", api_client=client)

    assert result["created"] == []
    assert len(result["skipped"]) == 4


def test_scaffold_skips_only_existing_subpage_via_legacy_titles():
    """ハブとサブ 1 つが v0.4.3 旧タイトル（絵文字 prefix 付き）で既存、
    残りのサブは新規作成（部分既存パターン、2 世代 legacy 検出）。
    """
    client = _ScaffoldRecordingClient(
        existing_pages=[
            ("parent", "📚 HOKUSAI Documentation"),
            ("existing-📚 HOKUSAI Documentation", "💬 Discussions"),
        ]
    )
    result = scaffold_notion_workspace("token", "parent", api_client=client)

    skipped_titles = [s["title"] for s in result["skipped"]]
    created_titles = [c["title"] for c in result["created"]]
    # ハブと 議論 (旧 Discussions) は legacy 検出で skip（canonical 名で記録）
    assert "Documentation" in skipped_titles
    assert "議論" in skipped_titles
    # 残りの 2 サブは新規作成（新 canonical 名）
    assert "運用ガイド" in created_titles
    assert "要件定義" in created_titles


def test_scaffold_partial_failure_does_not_raise():
    """サブページ作成のいずれかが失敗しても残りは作成（partial success）"""
    client = _ScaffoldRecordingClient(fail_on_titles={"運用ガイド"})
    result = scaffold_notion_workspace("token", "parent", api_client=client)

    created_titles = [c["title"] for c in result["created"]]
    # 失敗したサブを除く 3 つは作成される
    assert "Documentation" in created_titles
    assert "議論" in created_titles
    assert "要件定義" in created_titles
    assert "運用ガイド" not in created_titles
    # 失敗したサブは "failed" に記録される
    failed_titles = [f["title"] for f in result.get("failed", [])]
    assert "運用ガイド" in failed_titles
    assert result["failed"][0].get("error")


def test_scaffold_lookup_failure_avoids_duplicates_via_error_dict():
    """list_block_children が失敗したら create_page を呼ばず error 付き dict を返す。

    fail-open で None を返すと、Notion API の一過性失敗時に重複ページを作って
    しまう。idempotent チェックが完了できない場合は致命扱いとして error フィールド
    に記録し、create_page は呼ばない（Copilot レビュー #1 + #4 対応 / partial state 保持）。
    """

    class _ListFailClient:
        def list_block_children(self, block_id, *, start_cursor=None):
            raise RuntimeError("network down")

        def create_page(self, payload):  # pragma: no cover
            raise AssertionError("should not be called when lookup fails")

    result = scaffold_notion_workspace("token", "parent", api_client=_ListFailClient())
    assert "error" in result
    assert "親ページの子要素取得に失敗" in result["error"]
    assert result["created"] == []
    assert result["skipped"] == []
    assert result["failed"] == []


def test_scaffold_walks_pagination_to_find_existing_page():
    """子要素が複数ページに分割されていても、後方ページの既存ページを発見できる。

    （Copilot レビュー #1 / setup.py:340 pagination 不足の対応）
    """

    class _PaginatedClient:
        def __init__(self):
            self.calls: list[str | None] = []
            self.created_pages: list[dict] = []

        def list_block_children(self, block_id, *, start_cursor=None):
            self.calls.append(start_cursor)
            if start_cursor is None:
                return {
                    "results": [
                        {
                            "type": "child_page",
                            "id": "noise-1",
                            "child_page": {"title": "other page"},
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "cursor-1",
                }
            assert start_cursor == "cursor-1"
            return {
                "results": [
                    {
                        "type": "child_page",
                        "id": "hub-on-page-2",
                        "child_page": {"title": "📚 HOKUSAI Documentation"},
                    }
                ],
                "has_more": False,
                "next_cursor": None,
            }

        def create_page(self, payload):
            self.created_pages.append(payload)
            title = payload["properties"]["title"][0]["text"]["content"]
            return {"id": f"new-{title}"}

    client = _PaginatedClient()
    result = scaffold_notion_workspace("token", "parent", api_client=client)
    # ハブはページ 2 で見つかった v0.4.3 legacy title を legacy_aliases で skip 扱い
    skipped_titles = [s["title"] for s in result["skipped"]]
    assert "Documentation" in skipped_titles  # canonical で記録
    # ハブ検出時に pagination が走ったことを確認（最初の 2 件が None → cursor-1）
    assert client.calls[:2] == [None, "cursor-1"]
    # ハブが skip されたので create_page でハブを作っていない（canonical も legacy も）
    hub_creates = [
        p for p in client.created_pages
        if p["properties"]["title"][0]["text"]["content"]
        in ("Documentation", "HOKUSAI Documentation", "📚 HOKUSAI Documentation")
    ]
    assert hub_creates == []


def test_scaffold_hub_creation_failure_returns_error_dict():
    """ハブページ作成失敗時は raise せず error フィールド付き dict を返す（partial state 保持）。

    Copilot レビュー 4 回目対応: 旧実装は呼び出し側の try/except で partial
    state を空 dict で上書きしていたため、ハブが既に skip 検出されていたケース
    でも CLI 出力から情報が消えていた。新実装は scaffold_notion_workspace 内で
    例外を捕捉して結果 dict にそのまま記録する。
    """
    client = _ScaffoldRecordingClient(
        fail_on_titles={"Documentation"}
    )
    result = scaffold_notion_workspace("token", "parent", api_client=client)
    assert "error" in result
    assert "ハブページ" in result["error"]
    assert result["created"] == []
    assert result["skipped"] == []
    assert result["failed"] == []


def test_scaffold_subpage_lookup_failure_preserves_hub_creation():
    """サブページ idempotent チェック失敗時もハブ作成済み partial state を維持。

    Copilot レビュー 4 回目（setup.py:282）対応の回帰防止。
    """

    class _LookupFailsAfterHub:
        def __init__(self):
            self.children_calls = 0

        def list_block_children(self, block_id, *, start_cursor=None):
            self.children_calls += 1
            if self.children_calls == 1:
                return {"results": [], "has_more": False, "next_cursor": None}
            raise RuntimeError("notion API 5xx during subpage lookup")

        def create_page(self, payload):
            title = payload["properties"]["title"][0]["text"]["content"]
            return {"id": f"new-{title}"}

    client = _LookupFailsAfterHub()
    result = scaffold_notion_workspace("token", "parent", api_client=client)
    created_titles = [c["title"] for c in result["created"]]
    assert "Documentation" in created_titles
    failed_titles = [f["title"] for f in result["failed"]]
    assert "議論" in failed_titles
    assert "運用ガイド" in failed_titles
    assert "要件定義" in failed_titles
    assert "error" not in result


def test_scaffold_rejects_empty_token():
    with pytest.raises(NotionSetupError):
        scaffold_notion_workspace("", "parent")


def test_scaffold_rejects_empty_parent():
    with pytest.raises(NotionSetupError):
        scaffold_notion_workspace("token", "")


# ---------------------------------------------------------------------------
# setup_notion_workspace(scaffold=...) との統合
# ---------------------------------------------------------------------------


class _DBPlusScaffoldClient:
    """DB 作成と scaffold 両方をモックするクライアント"""

    def __init__(self):
        self.create_database_calls = 0
        self.create_page_calls: list[dict] = []
        self.list_children_calls = 0

    def create_database(self, payload):
        self.create_database_calls += 1
        title = payload["title"][0]["text"]["content"]
        return {"id": f"db-{title}"}

    def create_page(self, payload):
        self.create_page_calls.append(payload)
        title = payload["properties"]["title"][0]["text"]["content"]
        return {"id": f"page-{title}"}

    def list_block_children(self, block_id, *, start_cursor=None):
        self.list_children_calls += 1
        return {"results": [], "has_more": False, "next_cursor": None}


def test_setup_workspace_without_scaffold_does_not_create_pages():
    """scaffold=False（既定）なら DB のみ作成、ページは作らない"""
    client = _DBPlusScaffoldClient()
    result = setup_notion_workspace("token", "parent", api_client=client)
    assert client.create_database_calls == 3  # Workflows + PR + Review Issues
    assert client.create_page_calls == []
    assert "scaffold" not in result


def test_setup_workspace_with_scaffold_creates_both():
    """scaffold=True なら DB + ドキュメントツリーを作成"""
    client = _DBPlusScaffoldClient()
    result = setup_notion_workspace(
        "token", "parent", scaffold=True, api_client=client
    )
    assert client.create_database_calls == 3
    assert len(client.create_page_calls) == 4  # ハブ + サブ 3
    assert "scaffold" in result
    assert len(result["scaffold"]["created"]) == 4


def test_setup_workspace_scaffold_failure_does_not_fail_db_creation():
    """scaffold が完全失敗しても DB は成功扱い（partial success）"""

    class _DBOkScaffoldFail:
        def __init__(self):
            self.db_calls = 0

        def create_database(self, payload):
            self.db_calls += 1
            return {"id": f"db-{self.db_calls}"}

        def list_block_children(self, block_id, *, start_cursor=None):
            return {"results": [], "has_more": False, "next_cursor": None}

        def create_page(self, payload):
            # scaffold のハブ作成で必ず失敗
            raise RuntimeError("scaffold network down")

    client = _DBOkScaffoldFail()
    result = setup_notion_workspace(
        "token", "parent", scaffold=True, api_client=client
    )
    # DB は作成される
    assert result["workflows_db_id"] == "db-1"
    assert result["pull_requests_db_id"] == "db-2"
    # scaffold は error を含む
    assert "scaffold" in result
    assert result["scaffold"].get("error") is not None


# ---------------------------------------------------------------------------
# CLI ハンドラ: --scaffold フラグ
# ---------------------------------------------------------------------------


def test_cli_handler_scaffold_flag_routes_to_setup(capsys, monkeypatch):
    """--scaffold 指定時に setup_notion_workspace に scaffold=True が渡る"""
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    received: dict = {}

    def _fake_setup(api_token, parent_page_id, **kwargs):
        received.update(kwargs)
        return {
            "workflows_db_id": "wf",
            "pull_requests_db_id": "pr",
            "scaffold": {"created": [{"title": "Documentation", "id": "h1"}], "skipped": []},
        }

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "parent"
        scaffold = True
        persist = False
        shell_rc = None
        no_backup = False

    rc = cli_main._handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert received.get("scaffold") is True
    assert "📚 ドキュメントツリー" in out  # ツリーセクション header (icon 残し)
    assert "Documentation" in out


def test_cli_handler_no_scaffold_flag_keeps_default(capsys, monkeypatch):
    """--scaffold 未指定なら scaffold=False で呼ばれ、ツリー表示も出ない"""
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    received: dict = {}

    def _fake_setup(api_token, parent_page_id, **kwargs):
        received.update(kwargs)
        return {"workflows_db_id": "wf", "pull_requests_db_id": "pr"}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "parent"
        scaffold = False
        persist = False
        shell_rc = None
        no_backup = False

    rc = cli_main._handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert received.get("scaffold") is False
    assert "📚 ドキュメントツリー" not in out


def test_cli_handler_scaffold_error_branch_shows_error_first(capsys, monkeypatch):
    """scaffold が致命エラーで返した場合、CLI 出力は error を最初に表示し、
    「変更なし」は表示しない（Copilot レビュー 4 回目 / cli_main.py:745 対応）。
    """
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {
            "workflows_db_id": "wf", "pull_requests_db_id": "pr",
            "scaffold": {
                "created": [], "skipped": [], "failed": [],
                "error": "RuntimeError: notion 5xx",
            },
        }

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "parent"
        scaffold = True
        persist = False
        shell_rc = None
        no_backup = False

    rc = cli_main._handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert rc == 0  # DB は成功なので exit 0
    assert "⚠️ scaffold 中にエラー" in out
    assert "RuntimeError: notion 5xx" in out
    assert "（変更なし）" not in out
    # error は created / skipped より前に出ているはず
    error_idx = out.find("⚠️ scaffold 中にエラー")
    tree_section_idx = out.find("📚 ドキュメントツリー")
    assert 0 <= tree_section_idx < error_idx  # ヘッダ → error の順


def test_cli_handler_scaffold_failed_subpages_show_in_output(capsys, monkeypatch):
    """個別サブページの failed 配列が CLI 出力で ✗ 失敗行として表示される
    （Copilot レビュー 4 回目 / cli_main.py:745 対応）。
    """
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    def _fake_setup(api_token, parent_page_id, **kwargs):
        return {
            "workflows_db_id": "wf", "pull_requests_db_id": "pr",
            "scaffold": {
                "created": [{"title": "Documentation", "id": "h1"}],
                "skipped": [],
                "failed": [
                    {"title": "議論", "error": "RuntimeError: boom"},
                ],
            },
        }

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.setup_notion_workspace",
        _fake_setup,
    )

    class _Args:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
        parent_page_id = "parent"
        scaffold = True
        persist = False
        shell_rc = None
        no_backup = False

    rc = cli_main._handle_notion_setup(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "✓ 作成: Documentation" in out
    assert "✗ 失敗: 議論" in out
    assert "RuntimeError: boom" in out
    # failed があるので「変更なし」は出ない
    assert "（変更なし）" not in out
