"""
render_settings_page の HTML 契約テスト

「サービス接続状態」セクションが想定どおりに含まれていること、JS が
`/api/connections` および `refresh=1` を呼び出すコードを含むこと、
status / severity / category の各列挙値が JS にマップされていることを検証する。

レイアウトの細部ではなく、UI ↔ API 契約として崩したくない要素を固定する目的。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.dashboard import render_settings_page  # noqa: E402

from hokusai.integrations import connection_status as cs  # noqa: E402


@pytest.fixture
def html() -> str:
    return render_settings_page(["example-github-issue", "example-gitlab"])


# ---------------------------------------------------------------------------
# DOM contract
# ---------------------------------------------------------------------------


def test_section_card_present(html: str):
    assert 'id="connectionStatusCard"' in html
    assert "サービス接続状態" in html


def test_recheck_button_present(html: str):
    assert 'id="recheckConnectionsBtn"' in html
    assert "再チェック" in html


def test_status_list_target_present(html: str):
    assert 'id="connectionStatusList"' in html
    assert 'id="connectionStatusMeta"' in html


def test_section_appears_before_existing_settings(html: str):
    """新規セクションが既存の「ダッシュボード設定」より上にあること"""
    new_section = html.index('id="connectionStatusCard"')
    existing_section = html.index("ダッシュボード設定")
    assert new_section < existing_section


# ---------------------------------------------------------------------------
# JS contract: API endpoints
# ---------------------------------------------------------------------------


def test_js_calls_collection_endpoint(html: str):
    assert "'/api/connections'" in html


def test_js_calls_refresh_endpoint(html: str):
    assert "/api/connections?refresh=1" in html


def test_js_does_not_expose_deep_mode_to_ui(html: str):
    """mode=deep は API のみで保持し、UI には出さない"""
    assert "mode=deep" not in html
    assert "?mode=deep" not in html


# ---------------------------------------------------------------------------
# JS contract: status / severity / category enum coverage
# ---------------------------------------------------------------------------


def test_js_maps_all_status_values(html: str):
    """connection_status モジュールの status 列挙が JS の STATUS_LABEL に揃っているか"""
    expected = {
        cs.STATUS_CONNECTED,
        cs.STATUS_NOT_INSTALLED,
        cs.STATUS_NOT_AUTHENTICATED,
        cs.STATUS_TIMEOUT,
        cs.STATUS_UNSUPPORTED,
        cs.STATUS_DISABLED,
        cs.STATUS_UNKNOWN,
    }
    for status in expected:
        assert f"{status}:" in html, f"STATUS_LABEL に {status} が含まれていません"


def test_js_maps_all_severity_values(html: str):
    """SEVERITY_BADGE が ok / warn / error / info をすべて扱う"""
    for severity in ("ok", "warn", "error", "info"):
        assert f"{severity}:" in html, f"SEVERITY_BADGE に {severity} が含まれていません"


def test_js_maps_all_categories(html: str):
    for category in (
        cs.CategoryLLMAgent,
        cs.CategoryGitHosting,
        cs.CategoryTaskBackend,
        cs.CategoryMCP,
    ):
        assert f"{category}:" in html, f"CATEGORY_LABEL に {category} が含まれていません"


def test_unsupported_label_distinct_from_not_authenticated(html: str):
    """『未対応』と『未認証』は別ラベルであり、UI で混同しない"""
    assert "'未対応'" in html
    assert "'未認証'" in html


# ---------------------------------------------------------------------------
# CSS classes used by JS-rendered markup
# ---------------------------------------------------------------------------


def test_new_severity_badge_styles_defined(html: str):
    """設定ページで新規追加した error / info バッジのスタイルがある。

    badge-ok / badge-warn は render_html 側のグローバル CSS で既に定義済みのため
    対象外。
    """
    for cls in ("badge-error", "badge-info"):
        assert f".{cls}" in html, f"CSS に .{cls} 定義が見当たりません"


def test_command_copy_button_class_present(html: str):
    """『コピー』ボタンの class（JS のクリックハンドラが参照する）が markup に含まれる"""
    assert "connection-copy-btn" in html
