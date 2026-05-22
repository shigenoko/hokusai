"""`hokusai prime` CLI ハンドラの統合テスト（Workgraph Phase 6 / Issue #48）

argparse 経由の引数解析 + workflow state 解決 + Notion fetch を含めた
end-to-end 動作を fake Notion API でカバーする。
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.cli_main import _handle_prime
from hokusai.config.models import (
    NotionDashboardConfig,
    NotionSyncRateLimitConfig,
    WorkflowConfig,
)
from hokusai.persistence.sqlite_store import SQLiteStore


class _Args:
    """argparse の Namespace 模倣（getattr 経由で取り出される値だけ持つ）"""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_config(
    tmp_path: Path, *, enabled: bool = True
) -> WorkflowConfig:
    db_path = tmp_path / "wf.db"
    cfg = WorkflowConfig(
        data_dir=tmp_path,
        database_path=db_path,
        checkpoint_db_path=tmp_path / "cp.db",
        notion_dashboard=NotionDashboardConfig(
            enabled=enabled,
            api_token_env="TEST_API_TOKEN",
            project_memory_db_id_env="TEST_MEMORY_DB",
            rate_limit=NotionSyncRateLimitConfig(
                requests_per_second=100, debounce_ms=0
            ),
        ),
    )
    return cfg


def _seed_workflow(config: WorkflowConfig, **overrides):
    """テスト用 workflow を SQLite に保存する"""
    store = SQLiteStore(config.database_path)
    # current_phase は SQLite 上で INTEGER として保存される（1..10）
    # ことに合わせて int で seed する。prime CLI 側で `phase{n}` に
    # 正規化されることをテストで検証する。
    state = {
        "workflow_id": overrides.get("workflow_id", "wf-1"),
        "profile_name": overrides.get("profile_name", "acme"),
        "current_phase": overrides.get("current_phase", 5),
    }
    store.save_workflow(state["workflow_id"], state)


@pytest.fixture
def captured():
    out = io.StringIO()
    err = io.StringIO()
    return out, err


def test_prime_returns_1_when_workflow_not_found(tmp_path, captured):
    out, err = captured
    cfg = _make_config(tmp_path)
    # workflow を seed せず存在しない状態
    args = _Args(workflow_id="missing", phase=None, memory_types=None, output="markdown")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 1
    assert "見つかりません" in err.getvalue()


def test_prime_returns_0_with_empty_memory_when_db_id_missing(
    tmp_path, monkeypatch, captured
):
    """Project Memory DB ID 未設定でも 0 件 prime で正常終了（後方互換）"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "token-xyz")
    monkeypatch.delenv("TEST_MEMORY_DB", raising=False)
    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="markdown")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    body = out.getvalue()
    assert "# HOKUSAI Prime Context — workflow `wf-1`" in body
    assert "_active Project Memory はありません_" in body


def test_prime_resolves_profile_and_phase_from_state(
    tmp_path, monkeypatch, captured
):
    """--profile / --phase 未指定なら workflow state を解決源にする"""
    out, err = captured
    cfg = _make_config(tmp_path)
    # int で seed → 実装側で `phase4` 文字列に正規化される（Copilot 指摘）
    _seed_workflow(cfg, profile_name="acme", current_phase=4)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    captured_calls: list[dict] = []

    class _FakeAPI:
        def __init__(self, *a, **kw):
            pass

        def query_database(self, *args, **kwargs):
            captured_calls.append(kwargs)
            return {"results": [], "has_more": False}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["workflow_id"] == "wf-1"
    assert payload["profile"] == "acme"
    assert payload["current_phase"] == "phase4"


def test_prime_normalizes_int_current_phase_to_phase_string(
    tmp_path, monkeypatch, captured
):
    """state.current_phase の int を `phase{n}` に正規化して出力する（Copilot 指摘）"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg, current_phase=7)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    captured_phase: list = []

    class _FakeClient:
        def __init__(self, *, api, database_id):
            pass

        def list_active_memories(self, *, profile, phase, types, **kwargs):
            captured_phase.append(phase)
            return []

    class _FakeAPI:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeClient,
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    # client には phase7 で渡る
    assert captured_phase == ["phase7"]
    # 出力にも phase7 が乗る
    payload = json.loads(out.getvalue())
    assert payload["current_phase"] == "phase7"


def test_prime_cli_phase_overrides_state(tmp_path, monkeypatch, captured):
    """--phase が指定されたら state の current_phase より優先される"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg, current_phase=4)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    class _FakeAPI:
        def __init__(self, *a, **kw):
            pass

        def query_database(self, *args, **kwargs):
            return {"results": [], "has_more": False}

    monkeypatch.setattr("hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI)
    args = _Args(
        workflow_id="wf-1",
        phase="phase6",
        memory_types=None,
        output="json",
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["current_phase"] == "phase6"


def test_prime_swallows_notion_failure_and_returns_0(
    tmp_path, monkeypatch, captured
):
    """Notion 障害時は memory 0 件で続行（stderr warning + exit 0）"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    class _BrokenAPI:
        def __init__(self, *a, **kw):
            raise RuntimeError("Notion is down")

    monkeypatch.setattr("hokusai.integrations.notion_dashboard.client.NotionAPIClient", _BrokenAPI)
    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="markdown")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    assert "Project Memory 取得に失敗" in err.getvalue()
    assert "# HOKUSAI Prime Context" in out.getvalue()


def test_prime_reloads_config_when_state_profile_differs(
    tmp_path, monkeypatch, captured
):
    """--profile 未指定で state.profile_name が config と異なれば
    create_config_from_env_and_file が state 側の profile で再呼び出しされる
    （Copilot 指摘: 別 profile の env を引かない）"""
    out, err = captured
    cfg = _make_config(tmp_path)
    # config 側 profile はあえて None。state の profile_name と不一致状態
    _seed_workflow(cfg, profile_name="acme")
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    reload_calls: list[dict] = []

    def _fake_create_config(config_path, *, profile_name=None):
        reload_calls.append({"profile_name": profile_name})
        # 再ロード後 config も同じ構成を返す（テスト目的: 呼ばれたことを検証）
        return _make_config(tmp_path)

    monkeypatch.setattr(
        "hokusai.config.create_config_from_env_and_file", _fake_create_config
    )

    class _FakeAPI:
        def __init__(self, *a, **kw):
            pass

        def query_database(self, *args, **kwargs):
            return {"results": [], "has_more": False}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    assert reload_calls == [{"profile_name": "acme"}]


def test_prime_does_not_reload_config_when_profile_arg_present(
    tmp_path, monkeypatch, captured
):
    """--profile 明示時は state.profile_name と無関係に config 再ロードしない"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg, profile_name="acme")
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    def _fake_create_config(*args, **kwargs):
        raise AssertionError("create_config_from_env_and_file should not be called")

    monkeypatch.setattr(
        "hokusai.config.create_config_from_env_and_file", _fake_create_config
    )

    class _FakeAPI:
        def __init__(self, *a, **kw):
            pass

        def query_database(self, *args, **kwargs):
            return {"results": [], "has_more": False}

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json", profile="acme")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0


def test_prime_passes_types_filter_to_client(tmp_path, monkeypatch, captured):
    """--type で指定された types が list_active_memories に伝わる"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "memdb")

    captured_types: list = []

    class _FakeClient:
        def __init__(self, *, api, database_id):
            self.api = api
            self.db = database_id

        def list_active_memories(self, *, profile, phase, types, **kwargs):
            captured_types.append(types)
            return []

    class _FakeAPI:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr("hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI)
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeClient,
    )

    args = _Args(
        workflow_id="wf-1",
        phase=None,
        memory_types=["project_rule", "avoidance"],
        output="json",
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    assert captured_types == [["project_rule", "avoidance"]]
