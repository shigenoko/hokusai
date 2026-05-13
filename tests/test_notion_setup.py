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
    WORKFLOWS_DB_TITLE,
    setup_notion_workspace,
)


class _RecordingClient:
    """NotionAPIClient の最小モック。create_database を記録"""

    def __init__(
        self,
        *,
        workflows_id: str = "wf-db-id",
        pr_id: str = "pr-db-id",
        fail_on: str | None = None,
    ):
        self.calls: list[tuple[str, dict]] = []
        self._workflows_id = workflows_id
        self._pr_id = pr_id
        self._fail_on = fail_on

    def create_database(self, payload: dict) -> dict:
        self.calls.append(("create_database", payload))
        title = (payload.get("title") or [{}])[0].get("text", {}).get("content", "")
        if self._fail_on == "workflows" and "Workflows" in title:
            raise RuntimeError("workflows db creation failed")
        if self._fail_on == "pull_requests" and "Pull Requests" in title:
            raise RuntimeError("pr db creation failed")
        if "Workflows" in title:
            return {"id": self._workflows_id}
        return {"id": self._pr_id}


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
# 正常系: 2 つのリソース作成
# ---------------------------------------------------------------------------


def test_setup_creates_two_resources_in_order():
    client = _RecordingClient()
    result = setup_notion_workspace(
        "token", "parent-page-id", api_client=client
    )

    actions = [c[0] for c in client.calls]
    # Workflows DB → Pull Requests DB の順
    assert actions == ["create_database", "create_database"]
    assert result["workflows_db_id"] == "wf-db-id"
    assert result["pull_requests_db_id"] == "pr-db-id"


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


def test_cli_handler_prints_export_lines_on_success(capsys, monkeypatch):
    """成功時に export コマンド例を出力する"""
    from hokusai import cli_main
    from hokusai.integrations import notion_dashboard as nd

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN", "secret")

    def _fake_setup(api_token, parent_page_id):
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

    def _fake_setup(api_token, parent_page_id):
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

    def _fake_setup(api_token, parent_page_id):
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

    def _fake_setup(api_token, parent_page_id):
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

    def _fake_setup(api_token, parent_page_id):
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

    def _fake_setup(api_token, parent_page_id):
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

    def _fake_setup(api_token, parent_page_id):
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


def test_cli_handler_persist_uses_profile_marker_and_custom_env_names(
    capsys, monkeypatch, tmp_path
):
    """--persist + profile 指定で、rc に profile マーカー + カスタム env 名が書かれる"""
    from hokusai import cli_main

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN_4HOKUSAI", "secret-4hokusai")

    def _fake_setup(api_token, parent_page_id):
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
