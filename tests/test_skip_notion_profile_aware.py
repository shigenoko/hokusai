"""Issue #111 / C. SKIP_NOTION profile 化 helper の単体テスト

dogfooding-findings §1.3: HOKUSAI_SKIP_NOTION がプロセス全体に効く問題を解消し、
HOKUSAI_SKIP_NOTION_<SLUG> (profile 単位) + HOKUSAI_ACTIVE_PROFILE (context env)
で profile-aware な lookup を提供。

検証ポイント:
1. profile_skip_env_name の SLUG 化（特殊文字 / lowercase / 数字）
2. is_skip_notion の評価順: 明示引数 > context env > legacy global
3. set_active_profile の挙動（None/空文字は no-op、正常値で setenv）
4. core パス置換: state / workflow / connection_status / cli/services
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# isort: off
from hokusai.utils.skip_notion import (  # noqa: E402
    ACTIVE_PROFILE_ENV,
    LEGACY_GLOBAL_ENV,
    active_skip_env_name,
    is_skip_notion,
    profile_skip_env_name,
    set_active_profile,
)
# isort: on


# --- profile_skip_env_name の SLUG 化テスト ---


def test_profile_skip_env_name_uppercases_alphabetic():
    assert profile_skip_env_name("hokusai") == "HOKUSAI_SKIP_NOTION_HOKUSAI"


def test_profile_skip_env_name_preserves_digits():
    assert profile_skip_env_name("4hokusai") == "HOKUSAI_SKIP_NOTION_4HOKUSAI"


def test_profile_skip_env_name_replaces_special_chars():
    assert (
        profile_skip_env_name("my-project") == "HOKUSAI_SKIP_NOTION_MY_PROJECT"
    )
    assert (
        profile_skip_env_name("my.project.v2")
        == "HOKUSAI_SKIP_NOTION_MY_PROJECT_V2"
    )


def test_profile_skip_env_name_strips_trailing_underscores():
    """末尾の特殊文字は _ に変換されるが strip で取り除く"""
    assert profile_skip_env_name("foo.") == "HOKUSAI_SKIP_NOTION_FOO"
    assert profile_skip_env_name("__foo__") == "HOKUSAI_SKIP_NOTION_FOO"


# --- is_skip_notion の評価順テスト ---


def test_is_skip_notion_returns_false_when_no_env_set():
    """全 env 無し → False"""
    with patch.dict(os.environ, {}, clear=True):
        assert is_skip_notion() is False
        assert is_skip_notion("hokusai") is False


def test_is_skip_notion_legacy_global_only():
    """legacy HOKUSAI_SKIP_NOTION のみ True → True（後方互換）"""
    with patch.dict(os.environ, {LEGACY_GLOBAL_ENV: "1"}, clear=True):
        assert is_skip_notion() is True
        assert is_skip_notion("hokusai") is True


def test_is_skip_notion_profile_specific_via_explicit_arg():
    """profile suffix env のみ True かつ明示引数で指定 → True"""
    with patch.dict(
        os.environ, {"HOKUSAI_SKIP_NOTION_HOKUSAI": "1"}, clear=True
    ):
        assert is_skip_notion("hokusai") is True
        # 別 profile を指定すると False（profile isolation）
        assert is_skip_notion("other") is False
        # 引数なしで legacy global も無いので False
        assert is_skip_notion() is False


def test_is_skip_notion_profile_specific_via_context_env():
    """HOKUSAI_ACTIVE_PROFILE 経由で profile 自動解決される"""
    with patch.dict(
        os.environ,
        {
            ACTIVE_PROFILE_ENV: "hokusai",
            "HOKUSAI_SKIP_NOTION_HOKUSAI": "1",
        },
        clear=True,
    ):
        assert is_skip_notion() is True


def test_is_skip_notion_active_profile_isolates_other_profile_flag():
    """context env が profile-A なら profile-B の suffix env で skip しない"""
    with patch.dict(
        os.environ,
        {
            ACTIVE_PROFILE_ENV: "hokusai",
            "HOKUSAI_SKIP_NOTION_OTHER": "1",  # 別 profile 用
        },
        clear=True,
    ):
        # active profile は hokusai、HOKUSAI_SKIP_NOTION_HOKUSAI は無い、
        # HOKUSAI_SKIP_NOTION_OTHER は無視、legacy global も無いので False
        assert is_skip_notion() is False
        # 明示引数で "other" を指定すれば True
        assert is_skip_notion("other") is True


def test_is_skip_notion_value_must_be_exact_one():
    """'1' 以外（'true', 'yes', '0' 等）は skip と解釈しない"""
    for value in ["true", "yes", "TRUE", "0", "", "false"]:
        with patch.dict(os.environ, {LEGACY_GLOBAL_ENV: value}, clear=True):
            assert is_skip_notion() is False, f"value={value!r} should be False"


def test_is_skip_notion_legacy_fallback_when_profile_env_missing():
    """profile suffix env が未設定なら legacy global にフォールバック"""
    with patch.dict(
        os.environ,
        {ACTIVE_PROFILE_ENV: "hokusai", LEGACY_GLOBAL_ENV: "1"},
        clear=True,
    ):
        assert is_skip_notion() is True


# --- set_active_profile の挙動テスト ---


def test_set_active_profile_with_valid_name():
    with patch.dict(os.environ, {}, clear=True):
        set_active_profile("hokusai")
        assert os.environ.get(ACTIVE_PROFILE_ENV) == "hokusai"


def test_set_active_profile_strips_whitespace():
    with patch.dict(os.environ, {}, clear=True):
        set_active_profile("  hokusai  ")
        assert os.environ.get(ACTIVE_PROFILE_ENV) == "hokusai"


def test_set_active_profile_none_is_noop():
    """None 渡しは既存 env を消したり上書きしたりしない"""
    with patch.dict(
        os.environ, {ACTIVE_PROFILE_ENV: "existing"}, clear=True
    ):
        set_active_profile(None)
        assert os.environ.get(ACTIVE_PROFILE_ENV) == "existing"


def test_set_active_profile_preserves_existing_value():
    """Round 3 対応: 既に HOKUSAI_ACTIVE_PROFILE が set されていれば
    上書きせず既存値を尊重する（setdefault 動作）.

    親プロセスが意図的に export しているケースを尊重するため。
    """
    with patch.dict(
        os.environ, {ACTIVE_PROFILE_ENV: "from-parent"}, clear=True
    ):
        set_active_profile("from-cli-arg")
        assert os.environ.get(ACTIVE_PROFILE_ENV) == "from-parent"


def test_set_active_profile_empty_string_is_noop():
    """空文字 / 空白のみは no-op"""
    with patch.dict(
        os.environ, {ACTIVE_PROFILE_ENV: "existing"}, clear=True
    ):
        set_active_profile("")
        set_active_profile("   ")
        assert os.environ.get(ACTIVE_PROFILE_ENV) == "existing"


# --- core パス置換の統合テスト ---


# --- active_skip_env_name の評価テスト（Round 1 指摘） ---


def test_active_skip_env_name_returns_none_when_no_skip():
    """skip 状態でないなら None"""
    with patch.dict(os.environ, {}, clear=True):
        assert active_skip_env_name() is None


def test_active_skip_env_name_returns_profile_suffix_when_active():
    """context env で active profile が set されており suffix env が "1" なら suffix 名を返す"""
    with patch.dict(
        os.environ,
        {
            ACTIVE_PROFILE_ENV: "hokusai",
            "HOKUSAI_SKIP_NOTION_HOKUSAI": "1",
        },
        clear=True,
    ):
        assert active_skip_env_name() == "HOKUSAI_SKIP_NOTION_HOKUSAI"


def test_active_skip_env_name_returns_legacy_when_only_global_set():
    """profile suffix が無く legacy global のみなら legacy 名を返す"""
    with patch.dict(os.environ, {LEGACY_GLOBAL_ENV: "1"}, clear=True):
        assert active_skip_env_name() == LEGACY_GLOBAL_ENV


def test_active_skip_env_name_prefers_profile_suffix_over_legacy():
    """両方 set されていたら profile suffix が優先される"""
    with patch.dict(
        os.environ,
        {
            ACTIVE_PROFILE_ENV: "hokusai",
            "HOKUSAI_SKIP_NOTION_HOKUSAI": "1",
            LEGACY_GLOBAL_ENV: "1",
        },
        clear=True,
    ):
        assert active_skip_env_name() == "HOKUSAI_SKIP_NOTION_HOKUSAI"


# --- 文言動的化テスト（Round 1 指摘） ---


def test_check_environment_warning_shows_actual_env_name():
    """warning 文言に実際に効いている env 名が入る"""
    from hokusai.cli.services.environment import check_environment

    with patch.dict(
        os.environ,
        {
            ACTIVE_PROFILE_ENV: "hokusai",
            "HOKUSAI_SKIP_NOTION_HOKUSAI": "1",
        },
        clear=True,
    ):
        warnings = check_environment()
        # profile suffix env 名が含まれる
        assert any("HOKUSAI_SKIP_NOTION_HOKUSAI=1" in w for w in warnings)
        # legacy 名のみは含まれない（profile suffix が優先表示）
        assert not any(
            w.startswith("HOKUSAI_SKIP_NOTION=1") for w in warnings
        )


def test_check_environment_uses_profile_aware_helper():
    """cli/services/environment.py が active_skip_env_name() 経由で
    profile-aware lookup を提供している（is_skip_notion と同じ評価順）.
    """
    from hokusai.cli.services.environment import check_environment

    # legacy 経路: HOKUSAI_SKIP_NOTION=1 で warning が出る（後方互換）
    with patch.dict(os.environ, {LEGACY_GLOBAL_ENV: "1"}, clear=True):
        warnings = check_environment()
        assert any("HOKUSAI_SKIP_NOTION=1" in w for w in warnings)

    # 新経路: profile suffix env で warning（active profile via context env）
    # Round 1 対応: 文言は実際に効いている env 名を含むようになった
    with patch.dict(
        os.environ,
        {
            ACTIVE_PROFILE_ENV: "hokusai",
            "HOKUSAI_SKIP_NOTION_HOKUSAI": "1",
        },
        clear=True,
    ):
        warnings = check_environment()
        assert any("HOKUSAI_SKIP_NOTION_HOKUSAI=1" in w for w in warnings)

    # どちらも無ければ warning 出ない
    with patch.dict(os.environ, {}, clear=True):
        warnings = check_environment()
        assert not any("HOKUSAI_SKIP_NOTION" in w for w in warnings)


def test_warn_if_skip_notion_pre_set_uses_profile_suffix_env(capsys):
    """Issue #113 (follow-up): _warn_if_skip_notion_pre_set の warning 文言が
    profile suffix env を反映する。
    """
    from unittest.mock import MagicMock

    from hokusai.cli_main import _warn_if_skip_notion_pre_set

    cfg = MagicMock()
    cfg.notion_dashboard = MagicMock()
    cfg.notion_dashboard.enabled = True

    with patch.dict(
        os.environ,
        {
            ACTIVE_PROFILE_ENV: "hokusai",
            "HOKUSAI_SKIP_NOTION_HOKUSAI": "1",
        },
        clear=True,
    ):
        _warn_if_skip_notion_pre_set(cfg, "hokusai")
        captured = capsys.readouterr()
        # profile suffix env 名が warning に含まれる（legacy 名のみは含まれない）
        assert "HOKUSAI_SKIP_NOTION_HOKUSAI=1" in captured.err


def test_task_backend_notion_uses_profile_aware_helper():
    """task_backend.notion._is_skip_notion が新 helper を経由"""
    from hokusai.integrations.task_backend.notion import NotionTaskClient

    with patch.dict(
        os.environ,
        {
            ACTIVE_PROFILE_ENV: "hokusai",
            "HOKUSAI_SKIP_NOTION_HOKUSAI": "1",
        },
        clear=True,
    ):
        assert NotionTaskClient._is_skip_notion() is True

    with patch.dict(os.environ, {}, clear=True):
        assert NotionTaskClient._is_skip_notion() is False
