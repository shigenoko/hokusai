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

from hokusai.cli_main import _build_prime_diagnostics, _handle_prime
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
            workflows_db_id_env="TEST_WORKFLOWS_DB",
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
    assert "_active な workgraph context はありません_" in body


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
            # no-op fake constructor: テスト fixture は state を持たない
            return

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
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_active_memories(self, *, profile, phase, types, **kwargs):
            captured_phase.append(phase)
            return []

    class _FakeAPI:
        def __init__(self, *a, **kw):
            # no-op fake constructor: テスト fixture は state を持たない
            return

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
            # no-op fake constructor: テスト fixture は state を持たない
            return

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
    assert "prime context（Notion）取得で失敗" in err.getvalue()
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
            # no-op fake constructor: テスト fixture は state を持たない
            return

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
            # no-op fake constructor: テスト fixture は state を持たない
            return

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
            # no-op fake constructor: テスト fixture は state を持たない
            return

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


# ---------------------------------------------------------------------------
# handover_note 世代遡及（Workgraph Phase 7 / Issue #52 / 要件 §8.4）
# ---------------------------------------------------------------------------


def test_prime_injects_handover_notes_via_supersedes_chain(
    tmp_path, monkeypatch, captured
):
    """Supersedes を辿って旧 workflow の active handover_note を memories に append"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg, profile_name="acme")
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "pm-db")
    monkeypatch.setenv("TEST_WORKFLOWS_DB", "wf-db")

    class _FakeWFClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def find_workflow_page_id(self, workflow_id):
            return "wf-page-current"

        def get_supersedes(self, page_id):
            # 1 世代: current → prior
            if page_id == "wf-page-current":
                return ["wf-page-prior"]
            return []

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_active_memories(self, *, profile, phase, types, **kwargs):
            return [
                {"id": "m-base", "properties": {"Name": {"title": [{"text": {"content": "Base"}}]}, "Type": {"select": {"name": "project_rule"}}}},
            ]

        def find_handover_notes_for_workflow(self, page_id, *, profile=None, **kwargs):
            assert page_id == "wf-page-prior"
            assert profile == "acme"
            return [
                {"id": "m-handover", "properties": {"Name": {"title": [{"text": {"content": "Handover"}}]}, "Type": {"select": {"name": "handover_note"}}}},
            ]

    class _FakeAPI:
        def __init__(self, *a, **kw):
            # no-op fake constructor: テスト fixture は state を持たない
            return

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.workflows_db.WorkflowsDBClient",
        _FakeWFClient,
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    payload = json.loads(out.getvalue())
    ids = [m["id"] for m in payload["memories"]]
    assert ids == ["m-base", "m-handover"]


def test_prime_traverses_multiple_supersedes_generations(
    tmp_path, monkeypatch, captured
):
    """A → A' → B のような多世代 chain で深さ 3 まで辿る"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "pm-db")
    monkeypatch.setenv("TEST_WORKFLOWS_DB", "wf-db")

    chain_map = {
        "wf-page-current": ["wf-page-gen2"],
        "wf-page-gen2": ["wf-page-gen3"],
        "wf-page-gen3": [],
    }
    visited_handover: list[str] = []

    class _FakeWFClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def find_workflow_page_id(self, workflow_id):
            return "wf-page-current"

        def get_supersedes(self, page_id):
            return chain_map.get(page_id, [])

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_active_memories(self, **kwargs):
            return []

        def find_handover_notes_for_workflow(self, page_id, **kwargs):
            visited_handover.append(page_id)
            return [
                {"id": f"m-{page_id}", "properties": {"Type": {"select": {"name": "handover_note"}}}},
            ]

    class _FakeAPI:
        def __init__(self, *a, **kw):
            # no-op fake constructor: テスト fixture は state を持たない
            return

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.workflows_db.WorkflowsDBClient",
        _FakeWFClient,
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    # gen2 / gen3 を順番に訪問（current 自身は対象外）
    assert visited_handover == ["wf-page-gen2", "wf-page-gen3"]


def test_prime_skips_handover_lookup_when_workflows_db_id_unset(
    tmp_path, monkeypatch, captured
):
    """Workflows DB ID 未設定なら handover_note 経路を skip（既存 prime 動作維持）"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "pm-db")
    monkeypatch.delenv("TEST_WORKFLOWS_DB", raising=False)

    handover_calls: list = []

    class _FakeWFClient:
        def __init__(self, *, api, database_id):
            handover_calls.append("wf_client_created")

        def find_workflow_page_id(self, workflow_id):
            handover_calls.append("find_page")
            return None

        def get_supersedes(self, page_id):
            return []

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_active_memories(self, **kwargs):
            return []

    class _FakeAPI:
        def __init__(self, *a, **kw):
            # no-op fake constructor: テスト fixture は state を持たない
            return

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.workflows_db.WorkflowsDBClient",
        _FakeWFClient,
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    # WorkflowsDBClient は生成すらされない
    assert handover_calls == []


def test_prime_handover_lookup_avoids_cycles(
    tmp_path, monkeypatch, captured
):
    """Supersedes が環状（A → B → A）でも無限ループしない"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "pm-db")
    monkeypatch.setenv("TEST_WORKFLOWS_DB", "wf-db")

    visited: list[str] = []

    class _FakeWFClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def find_workflow_page_id(self, workflow_id):
            return "wf-page-A"

        def get_supersedes(self, page_id):
            # A → B → A の環状参照
            if page_id == "wf-page-A":
                return ["wf-page-B"]
            return ["wf-page-A"]  # B → A（環状）

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_active_memories(self, **kwargs):
            return []

        def find_handover_notes_for_workflow(self, page_id, **kwargs):
            visited.append(page_id)
            return []

    class _FakeAPI:
        def __init__(self, *a, **kw):
            # no-op fake constructor: テスト fixture は state を持たない
            return

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.workflows_db.WorkflowsDBClient",
        _FakeWFClient,
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    # A は visited（current 自身）には含まれないが、chain では B のみ訪問して環状で停止
    assert visited == ["wf-page-B"]


def test_prime_dedupes_overlapping_memories():
    """_merge_memories_dedup が id 重複を排除する（page id ベース）"""
    from hokusai.cli_main import _merge_memories_dedup

    base = [
        {"id": "m1", "properties": {}},
        {"id": "m2", "properties": {}},
    ]
    extra = [
        {"id": "m2", "properties": {}},  # 重複
        {"id": "m3", "properties": {}},
    ]
    result = _merge_memories_dedup(base, extra)
    assert [m["id"] for m in result] == ["m1", "m2", "m3"]


def test_prime_skips_handover_when_type_filter_excludes_it(
    tmp_path, monkeypatch, captured
):
    """--type で handover_note を含めない場合は世代遡及自体を skip"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "pm-db")
    monkeypatch.setenv("TEST_WORKFLOWS_DB", "wf-db")

    supersedes_called: list = []
    handover_lookup_called: list = []

    class _FakeWFClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def find_workflow_page_id(self, workflow_id):
            return "wf-page"

        def get_supersedes(self, page_id):
            # handover 経路に入ったら呼ばれる。Issue #54 で workgraph context
            # 統合のため WorkflowsDBClient 自体は --type 指定時も生成される
            # が、_collect_handover_notes は呼ばれない（= get_supersedes に
            # 来ない）ことで検証する。
            supersedes_called.append("get_supersedes")
            return []

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_active_memories(self, **kwargs):
            return []

        def find_handover_notes_for_workflow(self, page_id, **kwargs):
            handover_lookup_called.append(page_id)
            return []

    class _FakeAPI:
        def __init__(self, *a, **kw):
            # no-op fake constructor: テスト fixture は state を持たない
            return

    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", _FakeAPI
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        _FakeMemClient,
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.workflows_db.WorkflowsDBClient",
        _FakeWFClient,
    )

    args = _Args(
        workflow_id="wf-1",
        phase=None,
        memory_types=["project_rule"],  # handover_note を除外
        output="json",
    )
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    # handover note 取得経路は走らない（get_supersedes も find_handover も呼ばれない）
    assert supersedes_called == []
    assert handover_lookup_called == []


# ---------------------------------------------------------------------------
# workgraph context 統合（Issue #54 / 要件 §8.4 完成）
# ---------------------------------------------------------------------------


def _make_baseline_workgraph_fakes():
    """test_prime_injects_workgraph_context_when_all_db_ids_set と
    test_prime_skips_workgraph_context_when_db_ids_unset で共通する
    Workflows / ProjectMemory / NotionAPI の fake class セット（SonarCloud
    duplication 対策で共通化）。返り値は (FakeAPI, FakeWFClient, FakeMemClient)
    の 3 つ組。"""

    class _FakeWFClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def find_workflow_page_id(self, workflow_id):
            return "wf-page-1"

        def get_supersedes(self, page_id):
            return []

    class _FakeMemClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_active_memories(self, **kwargs):
            return []

        def find_handover_notes_for_workflow(self, page_id, **kwargs):
            return []

    class _FakeAPI:
        def __init__(self, *a, **kw):
            # no-op fake constructor: テスト fixture は state を持たない
            return

    return _FakeAPI, _FakeWFClient, _FakeMemClient


def _apply_baseline_workgraph_patches(monkeypatch, fake_api, fake_wf, fake_mem):
    """fake_api / fake_wf / fake_mem を Notion 各モジュールへ monkeypatch する
    共通 helper（SonarCloud duplication 対策で共通化）。"""
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.client.NotionAPIClient", fake_api
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.project_memory_db.ProjectMemoryDBClient",
        fake_mem,
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.workflows_db.WorkflowsDBClient",
        fake_wf,
    )


def test_prime_injects_workgraph_context_when_all_db_ids_set(
    tmp_path, monkeypatch, captured
):
    """全 DB ID 設定済みなら work_items / review_issues / gates も prime に統合"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "pm-db")
    monkeypatch.setenv("TEST_WORKFLOWS_DB", "wf-db")
    monkeypatch.setenv("TEST_WORK_ITEMS_DB", "wi-db")
    monkeypatch.setenv("TEST_REVIEW_ISSUES_DB", "ri-db")
    monkeypatch.setenv("TEST_WORKFLOW_GATES_DB", "wg-db")

    # cfg を拡張: 追加 env を解決可能に
    cfg.notion_dashboard.work_items_db_id_env = "TEST_WORK_ITEMS_DB"
    cfg.notion_dashboard.review_issues_db_id_env = "TEST_REVIEW_ISSUES_DB"
    cfg.notion_dashboard.workflow_gates_db_id_env = "TEST_WORKFLOW_GATES_DB"

    fake_api, fake_wf_client, fake_mem_client = _make_baseline_workgraph_fakes()

    class _FakeWIClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_ready_work_items_for_workflow(self, page_id, **kwargs):
            return [{"id": "wi-1", "properties": {"Title": {"title": [{"text": {"content": "Login"}}]}, "Status": {"select": {"name": "ready"}}}}]

    class _FakeRIClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_open_review_issues_for_workflow(self, page_id, **kwargs):
            return [{"id": "ri-1", "properties": {"Title": {"title": [{"text": {"content": "Bug"}}]}, "Severity": {"select": {"name": "high"}}}}]

    class _FakeWGClient:
        def __init__(self, *, api, database_id):
            # no-op fake constructor: テスト fixture は state を持たない
            return

        def list_pending_gates_for_workflow(self, page_id, **kwargs):
            return [{"id": "g-1", "properties": {"Name": {"title": [{"text": {"content": "Sec"}}]}, "Status": {"select": {"name": "pending"}}}}]

    _apply_baseline_workgraph_patches(monkeypatch, fake_api, fake_wf_client, fake_mem_client)
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.work_items_db.WorkItemsDBClient", _FakeWIClient,
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.review_issues_db.ReviewIssuesDBClient", _FakeRIClient,
    )
    monkeypatch.setattr(
        "hokusai.integrations.notion_dashboard.workflow_gates_db.WorkflowGatesDBClient", _FakeWGClient,
    )

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert [w["id"] for w in payload["work_items"]] == ["wi-1"]
    assert [r["id"] for r in payload["review_issues"]] == ["ri-1"]
    assert [g["id"] for g in payload["gates"]] == ["g-1"]


def test_prime_skips_workgraph_context_when_db_ids_unset(
    tmp_path, monkeypatch, captured
):
    """各 DB ID 未設定なら該当 section は null（未取得 / fetch 試行せず）。
    取得済み 0 件 (= []) と区別するため null になることを検証。"""
    out, err = captured
    cfg = _make_config(tmp_path)
    _seed_workflow(cfg)
    monkeypatch.setenv("TEST_API_TOKEN", "t")
    monkeypatch.setenv("TEST_MEMORY_DB", "pm-db")
    monkeypatch.setenv("TEST_WORKFLOWS_DB", "wf-db")
    monkeypatch.delenv("TEST_WORK_ITEMS_DB", raising=False)
    monkeypatch.delenv("TEST_REVIEW_ISSUES_DB", raising=False)
    monkeypatch.delenv("TEST_WORKFLOW_GATES_DB", raising=False)
    cfg.notion_dashboard.work_items_db_id_env = "TEST_WORK_ITEMS_DB"
    cfg.notion_dashboard.review_issues_db_id_env = "TEST_REVIEW_ISSUES_DB"
    cfg.notion_dashboard.workflow_gates_db_id_env = "TEST_WORKFLOW_GATES_DB"

    fake_api, fake_wf_client, fake_mem_client = _make_baseline_workgraph_fakes()
    # 各 sub client は monkeypatch しない（呼ばれたらテスト fail）
    _apply_baseline_workgraph_patches(monkeypatch, fake_api, fake_wf_client, fake_mem_client)

    args = _Args(workflow_id="wf-1", phase=None, memory_types=None, output="json")
    with redirect_stdout(out), redirect_stderr(err):
        rc = _handle_prime(args, cfg)
    assert rc == 0
    payload = json.loads(out.getvalue())
    # DB ID 未設定の場合は「未取得」を意味する null（Copilot 指摘で取得 0 件と区別）
    assert payload["work_items"] is None
    assert payload["review_issues"] is None
    assert payload["gates"] is None


# ---------------------------------------------------------------------------
# M2.4 (#92): _build_prime_diagnostics 単体テスト
# 構成要素ごとの「設定有無 / 取得結果」を診断行に組み立てる純粋関数。
# ---------------------------------------------------------------------------


class _NotionCfgStub:
    """notion_dashboard config の最小スタブ。`getattr(cfg, "...", default)`
    経由で参照されるフィールドのみ保持する。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        api_token_env: str = "X_API_TOKEN",
        project_memory_db_id_env: str = "X_PM_DB",
        workflows_db_id_env: str = "X_WF_DB",
        work_items_db_id_env: str = "X_WI_DB",
        review_issues_db_id_env: str = "X_RI_DB",
        workflow_gates_db_id_env: str = "X_WG_DB",
    ):
        self.enabled = enabled
        self.api_token_env = api_token_env
        self.project_memory_db_id_env = project_memory_db_id_env
        self.workflows_db_id_env = workflows_db_id_env
        self.work_items_db_id_env = work_items_db_id_env
        self.review_issues_db_id_env = review_issues_db_id_env
        self.workflow_gates_db_id_env = workflow_gates_db_id_env


def _empty_kwargs(notion_cfg):
    """すべて「未設定 / 未取得」相当の baseline kwargs を返す helper."""
    return dict(
        notion_cfg=notion_cfg,
        api_token="",
        memories_db_id="",
        memories=[],
        workflows_db_id="",
        work_items_db_id="",
        work_items=None,
        review_issues_db_id="",
        review_issues=None,
        workflow_gates_db_id="",
        gates=None,
    )


def test_diagnostics_when_notion_dashboard_disabled():
    diag = _build_prime_diagnostics(
        **_empty_kwargs(_NotionCfgStub(enabled=False))
    )
    # 連携無効なら 1 行のみで早期 return
    assert diag == ["Notion 連携: 無効 (notion_dashboard.enabled=false)"]


def test_diagnostics_when_notion_cfg_none():
    diag = _build_prime_diagnostics(**_empty_kwargs(None))
    assert diag == ["Notion 連携: 無効 (notion_dashboard.enabled=false)"]


def test_diagnostics_all_unset_lists_each_source():
    cfg = _NotionCfgStub()
    diag = _build_prime_diagnostics(**_empty_kwargs(cfg))
    # API Token 未設定
    assert any("Notion API Token: 未設定 (env X_API_TOKEN)" in d for d in diag)
    # Project Memory DB 未設定
    assert any(
        "Project Memory DB: 未設定 (env X_PM_DB)" in d for d in diag
    )
    # Workflows DB 未設定（連鎖して 3 カテゴリも skip される旨）
    assert any("Workflows DB: 未設定 (env X_WF_DB)" in d for d in diag)
    # 各カテゴリ DB も「未設定」
    assert any("Work Items DB: 未設定 (env X_WI_DB)" in d for d in diag)
    assert any(
        "Review Issues DB: 未設定 (env X_RI_DB)" in d for d in diag
    )
    assert any(
        "Workflow Gates DB: 未設定 (env X_WG_DB)" in d for d in diag
    )


def test_diagnostics_project_memory_fetched_empty_when_token_and_db_set():
    """API token + DB ID が揃って fetch を試みたが 0 件、というケース."""
    kwargs = _empty_kwargs(_NotionCfgStub())
    kwargs["api_token"] = "tok"
    kwargs["memories_db_id"] = "pmdb"
    kwargs["memories"] = []
    diag = _build_prime_diagnostics(**kwargs)
    assert any("Project Memory DB: 取得済 0 件" in d for d in diag)
    # token があるので「Notion API Token: 未設定」行は出ない
    assert not any("Notion API Token: 未設定" in d for d in diag)


def test_diagnostics_workgraph_section_distinguishes_unset_vs_unfetched_vs_empty():
    """list[dict] = 取得済 0 件、None = 未取得（DB ID 未設定 or workflow_page_id
    解決失敗）、DB ID 自体無い = 未設定、を 3 通り区別する."""
    cfg = _NotionCfgStub()
    # work_items_db_id は設定済だが workflow_page_id 解決失敗等で fetch 未試行
    kwargs = _empty_kwargs(cfg)
    kwargs["work_items_db_id"] = "wdb"
    kwargs["work_items"] = None  # 未取得
    kwargs["review_issues_db_id"] = "rdb"
    kwargs["review_issues"] = []  # 取得済 0 件
    # gates 系は db_id 未設定のまま → 未設定
    diag = _build_prime_diagnostics(**kwargs)

    work_lines = [d for d in diag if d.startswith("Work Items DB:")]
    assert len(work_lines) == 1
    assert "未取得" in work_lines[0]

    review_lines = [d for d in diag if d.startswith("Review Issues DB:")]
    assert len(review_lines) == 1
    assert review_lines[0] == "Review Issues DB: 取得済 0 件"

    gate_lines = [d for d in diag if d.startswith("Workflow Gates DB:")]
    assert len(gate_lines) == 1
    assert "未設定" in gate_lines[0]


def test_diagnostics_omits_section_when_data_present():
    """list 非空のセクションは診断行に含めない（出力ノイズ防止）."""
    cfg = _NotionCfgStub()
    kwargs = _empty_kwargs(cfg)
    kwargs["api_token"] = "tok"
    kwargs["memories_db_id"] = "pmdb"
    kwargs["memories"] = [{"id": "p1"}]  # 取得済 1 件
    kwargs["workflows_db_id"] = "wfdb"
    kwargs["work_items_db_id"] = "widb"
    kwargs["work_items"] = [{"id": "w1"}]  # 取得済 1 件
    diag = _build_prime_diagnostics(**kwargs)
    # Project Memory / Work Items は出ない
    assert not any(d.startswith("Project Memory DB:") for d in diag)
    assert not any(d.startswith("Work Items DB:") for d in diag)
    # 未設定の Review Issues / Gates は出る
    assert any(d.startswith("Review Issues DB:") for d in diag)
    assert any(d.startswith("Workflow Gates DB:") for d in diag)
