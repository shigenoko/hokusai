"""Issue #19: Notion identification ヘルパのテスト。

mask_db_id / notion_db_url / get_bot_info（cached） / get_bot_display_name /
build_notion_identification の挙動を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.integrations.notion_dashboard.identification import (
    build_notion_identification,
    clear_bot_info_cache,
    get_bot_display_name,
    get_bot_info,
    mask_db_id,
    notion_db_url,
)


# ---------------------------------------------------------------------------
# mask_db_id
# ---------------------------------------------------------------------------


def test_mask_db_id_typical():
    assert mask_db_id("35f85495-565d-81c9-aea4-f4a137ed82ff") == "35f85495...82ff"


def test_mask_db_id_no_dashes():
    assert mask_db_id("35f85495565d81c9aea4f4a137ed82ff") == "35f85495...82ff"


def test_mask_db_id_short_value_returns_unknown():
    assert mask_db_id("abc") == "(unknown)"


def test_mask_db_id_none_or_empty():
    assert mask_db_id(None) == "(unknown)"
    assert mask_db_id("") == "(unknown)"


def test_mask_db_id_non_string():
    # 型が違う値（int 等）でも例外を出さず unknown 扱い
    assert mask_db_id(12345) == "(unknown)"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# notion_db_url
# ---------------------------------------------------------------------------


def test_notion_db_url_strips_dashes():
    url = notion_db_url("35f85495-565d-81c9-aea4-f4a137ed82ff")
    assert url == "https://www.notion.so/35f85495565d81c9aea4f4a137ed82ff"


def test_notion_db_url_no_dashes_passthrough():
    url = notion_db_url("35f85495565d81c9aea4f4a137ed82ff")
    assert url == "https://www.notion.so/35f85495565d81c9aea4f4a137ed82ff"


def test_notion_db_url_empty():
    assert notion_db_url("") == ""
    assert notion_db_url(None) == ""


# ---------------------------------------------------------------------------
# get_bot_display_name
# ---------------------------------------------------------------------------


def test_get_bot_display_name_with_bot_type():
    info = {"name": "HOKUSAI Integration", "type": "bot"}
    assert get_bot_display_name(info) == "HOKUSAI Integration (bot)"


def test_get_bot_display_name_non_bot_type():
    info = {"name": "Some User", "type": "person"}
    assert get_bot_display_name(info) == "Some User"


def test_get_bot_display_name_no_name():
    info = {"type": "bot"}
    assert get_bot_display_name(info) == "(no name)"


def test_get_bot_display_name_none_or_empty():
    assert get_bot_display_name(None) == "(unable to fetch)"
    assert get_bot_display_name({}) == "(no name)"


# ---------------------------------------------------------------------------
# get_bot_info (キャッシュつき)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """各テスト前にキャッシュをクリア"""
    clear_bot_info_cache()
    yield
    clear_bot_info_cache()


def test_get_bot_info_returns_none_for_empty_token():
    assert get_bot_info("") is None


def test_get_bot_info_calls_api_and_caches(monkeypatch):
    """初回は API 呼び出し、2 回目はキャッシュから返す"""
    from hokusai.integrations.notion_dashboard import identification as ident_mod

    mock_client = MagicMock()
    mock_client.get_bot_info.return_value = {
        "id": "bot-1", "name": "Test Bot", "type": "bot",
    }
    monkeypatch.setattr(
        ident_mod, "NotionAPIClient", lambda api_token: mock_client
    )

    r1 = get_bot_info("token-A")
    r2 = get_bot_info("token-A")

    assert r1 == r2 == {"id": "bot-1", "name": "Test Bot", "type": "bot"}
    # 同じ key（= token）で 2 回呼んでも API call は 1 回
    assert mock_client.get_bot_info.call_count == 1


def test_get_bot_info_different_cache_keys_separate(monkeypatch):
    from hokusai.integrations.notion_dashboard import identification as ident_mod

    mock_client = MagicMock()
    mock_client.get_bot_info.return_value = {"name": "Bot X", "type": "bot"}
    monkeypatch.setattr(
        ident_mod, "NotionAPIClient", lambda api_token: mock_client
    )

    get_bot_info("token-A", cache_key="profile-a")
    get_bot_info("token-B", cache_key="profile-b")
    # 別の cache_key なので両方 API 呼ばれる
    assert mock_client.get_bot_info.call_count == 2


def test_get_bot_info_ttl_expires(monkeypatch):
    """TTL を超えたら再取得する"""
    from hokusai.integrations.notion_dashboard import identification as ident_mod

    mock_client = MagicMock()
    mock_client.get_bot_info.return_value = {"name": "X", "type": "bot"}
    monkeypatch.setattr(
        ident_mod, "NotionAPIClient", lambda api_token: mock_client
    )

    # 時刻を mock
    current_time = [1000.0]
    monkeypatch.setattr(ident_mod, "_now", lambda: current_time[0])

    get_bot_info("token", ttl_seconds=60)
    current_time[0] = 1030.0  # TTL 内
    get_bot_info("token", ttl_seconds=60)
    assert mock_client.get_bot_info.call_count == 1

    current_time[0] = 1070.0  # TTL 超過
    get_bot_info("token", ttl_seconds=60)
    assert mock_client.get_bot_info.call_count == 2


def test_get_bot_info_returns_none_on_api_error(monkeypatch):
    """NotionAPIError は graceful degrade で None"""
    from hokusai.integrations.notion_dashboard import identification as ident_mod
    from hokusai.integrations.notion_dashboard.client import NotionAPIError

    mock_client = MagicMock()
    mock_client.get_bot_info.side_effect = NotionAPIError(401, "Unauthorized")
    monkeypatch.setattr(
        ident_mod, "NotionAPIClient", lambda api_token: mock_client
    )

    assert get_bot_info("bad-token") is None


def test_get_bot_info_returns_none_on_unexpected_exception(monkeypatch):
    """予期しない例外でも None で graceful degrade"""
    from hokusai.integrations.notion_dashboard import identification as ident_mod

    mock_client = MagicMock()
    mock_client.get_bot_info.side_effect = RuntimeError("network down")
    monkeypatch.setattr(
        ident_mod, "NotionAPIClient", lambda api_token: mock_client
    )

    assert get_bot_info("token") is None


# ---------------------------------------------------------------------------
# build_notion_identification
# ---------------------------------------------------------------------------


def test_build_notion_identification_full(monkeypatch):
    from hokusai.integrations.notion_dashboard import identification as ident_mod

    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN_4HOKUSAI", "secret-token")
    monkeypatch.setenv(
        "HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI",
        "35f85495-565d-81c9-aea4-f4a137ed82ff",
    )
    monkeypatch.setenv(
        "HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI",
        "35f85495-565d-8133-8ac5-ca4d2d73c0dc",
    )

    mock_client = MagicMock()
    mock_client.get_bot_info.return_value = {
        "id": "bot-1", "name": "HOKUSAI Integration", "type": "bot",
    }
    monkeypatch.setattr(
        ident_mod, "NotionAPIClient", lambda api_token: mock_client
    )

    ident = build_notion_identification(
        profile_name="hokusai",
        api_token_env="HOKUSAI_NOTION_API_TOKEN_4HOKUSAI",
        workflows_db_id_env="HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI",
        pull_requests_db_id_env="HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI",
    )

    assert ident["profile_name"] == "hokusai"
    assert ident["api_token_env"] == "HOKUSAI_NOTION_API_TOKEN_4HOKUSAI"
    assert ident["workflows_db_id_full"] == "35f85495-565d-81c9-aea4-f4a137ed82ff"
    assert ident["workflows_db_id_masked"] == "35f85495...82ff"
    assert ident["workflows_db_url"].endswith("35f85495565d81c9aea4f4a137ed82ff")
    assert ident["pull_requests_db_id_masked"] == "35f85495...c0dc"
    assert ident["bot_display_name"] == "HOKUSAI Integration (bot)"


def test_build_notion_identification_no_token(monkeypatch):
    """token env が未設定なら Bot は (unable to fetch)、他は unknown"""
    monkeypatch.delenv("HOKUSAI_NOTION_API_TOKEN_4HOKUSAI", raising=False)
    monkeypatch.delenv("HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI", raising=False)
    monkeypatch.delenv("HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI", raising=False)

    ident = build_notion_identification(
        profile_name=None,
        api_token_env="HOKUSAI_NOTION_API_TOKEN_4HOKUSAI",
        workflows_db_id_env="HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI",
        pull_requests_db_id_env="HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI",
    )
    assert ident["profile_name"] is None
    assert ident["workflows_db_id_masked"] == "(unknown)"
    assert ident["pull_requests_db_id_masked"] == "(unknown)"
    assert ident["bot_display_name"] == "(unable to fetch)"


# ---------------------------------------------------------------------------
# render_notion_dashboard_panel (smoke test)
# ---------------------------------------------------------------------------


def test_panel_includes_identification_section(monkeypatch):
    """panel HTML に identification セクションが含まれる"""
    monkeypatch.setenv("HOKUSAI_NOTION_API_TOKEN_4HOKUSAI", "secret-token")
    monkeypatch.setenv(
        "HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI",
        "35f85495-565d-81c9-aea4-f4a137ed82ff",
    )
    monkeypatch.setenv(
        "HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI",
        "35f85495-565d-8133-8ac5-ca4d2d73c0dc",
    )

    # NotionAPIClient を mock
    from hokusai.integrations.notion_dashboard import identification as ident_mod

    mock_client = MagicMock()
    mock_client.get_bot_info.return_value = {
        "name": "HOKUSAI Integration", "type": "bot",
    }
    monkeypatch.setattr(
        ident_mod, "NotionAPIClient", lambda api_token: mock_client
    )

    # dispatcher / config をテスト用に差し替え
    from types import SimpleNamespace
    nd_cfg = SimpleNamespace(
        enabled=True,
        api_token_env="HOKUSAI_NOTION_API_TOKEN_4HOKUSAI",
        workflows_db_id_env="HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI",
        pull_requests_db_id_env="HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI",
    )

    import scripts.dashboard as dashboard
    # _get_notion_dispatcher() の戻り値を mock
    mock_dispatcher = MagicMock()
    mock_dispatcher._config = nd_cfg
    mock_dispatcher.is_configured.return_value = True
    monkeypatch.setattr(dashboard, "_get_notion_dispatcher", lambda: mock_dispatcher)

    # _get_store() の戻り値も mock
    mock_store = MagicMock()
    mock_store.count_notion_sync_pending.return_value = 0
    mock_store.count_notion_sync_errors.return_value = 0
    monkeypatch.setattr(dashboard, "_get_store", lambda: mock_store)

    # get_config().notion_dashboard を mock
    cfg = SimpleNamespace(notion_dashboard=nd_cfg, profile_name="hokusai")
    monkeypatch.setattr(
        "hokusai.config.get_config", lambda: cfg
    )

    html = dashboard.render_notion_dashboard_panel()

    assert "接続先 Notion" in html
    assert "hokusai" in html  # profile name
    assert "HOKUSAI_NOTION_API_TOKEN_4HOKUSAI" in html
    assert "35f85495...82ff" in html  # workflows masked
    assert "35f85495...c0dc" in html  # PR masked
    assert "HOKUSAI Integration (bot)" in html
    # 完全 ID は title 属性のみに含まれる（直接 visible にはマスク版が出ている）
    assert 'title="35f85495-565d-81c9-aea4-f4a137ed82ff"' in html


def test_panel_section_not_rendered_when_disabled(monkeypatch):
    """notion_dashboard が disabled なら panel 全体が空"""
    import scripts.dashboard as dashboard
    from types import SimpleNamespace

    nd_cfg = SimpleNamespace(enabled=False)
    mock_dispatcher = MagicMock()
    mock_dispatcher._config = nd_cfg
    monkeypatch.setattr(dashboard, "_get_notion_dispatcher", lambda: mock_dispatcher)

    assert dashboard.render_notion_dashboard_panel() == ""
