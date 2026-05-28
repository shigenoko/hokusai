"""`hokusai llm-gateway-setup` サブコマンドのテスト（PR #125 / F2）

dogfooding-findings.md §7 F2 で記録した「`allowed_providers=None` 既定で
policy_hits が常時空 → `log_only=false` に切り替えても enforcement が
事実上 no-op」という事故を踏む前に警告する wizard の回帰防止テスト。
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from hokusai.cli_main import _build_parser, _handle_llm_gateway_setup
from hokusai.config.models import (
    LLMGatewayAllowedModelsConfig,
    LLMGatewayConfig,
)


def _make_config(
    *,
    enabled: bool = False,
    log_only: bool = True,
    allowed_providers: list[str] | None = None,
    allowed_models_default: list[str] | None = None,
):
    """テスト用 minimal WorkflowConfig stub"""
    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.llm_gateway = LLMGatewayConfig(
        enabled=enabled,
        log_only=log_only,
        allowed_providers=allowed_providers,
        allowed_models=LLMGatewayAllowedModelsConfig(
            default=allowed_models_default,
            high_cost_requires_gate=[],
        ),
    )
    return cfg


def _run_setup(args_list: list[str], cfg) -> tuple[int, str]:
    """`hokusai llm-gateway-setup ...` を実 parser 経由で実行し (rc, stdout) を返す"""
    parser, _, _ = _build_parser()
    args = parser.parse_args(["llm-gateway-setup", *args_list])
    buf = StringIO()
    with patch.object(sys, "stdout", buf):
        rc = _handle_llm_gateway_setup(args, cfg)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 警告ケース: policy 未設定（F2 本丸）
# ---------------------------------------------------------------------------


def test_warns_when_no_policy_and_enforce_off():
    """log_only=true でも policy 未設定なら将来切替時のリスクとして警告"""
    cfg = _make_config(enabled=False, log_only=True)
    rc, output = _run_setup([], cfg)
    assert rc == 1
    assert "警告" in output
    assert "allowed_providers" in output
    assert "no-op" in output
    # 推奨設定例も出力
    assert "llm_gateway:" in output
    assert "claude_code" in output


def test_warns_when_no_policy_and_enforce_on():
    """log_only=false + 未設定なら即座の no-op として警告（より強い表現）"""
    cfg = _make_config(enabled=True, log_only=False)
    rc, output = _run_setup([], cfg)
    assert rc == 1
    assert "警告" in output
    # 「即座の no-op」を示す文言
    assert "enforcement on" in output


def test_warns_when_provider_empty_list():
    """allowed_providers=[] でも未設定扱い（has_provider_allowlist=False）"""
    cfg = _make_config(allowed_providers=[])
    rc, output = _run_setup([], cfg)
    assert rc == 1
    assert "警告" in output


# ---------------------------------------------------------------------------
# 安全ケース: policy 設定済み
# ---------------------------------------------------------------------------


def test_no_warning_when_provider_allowlist_set():
    """allowed_providers が設定されていれば警告なし、rc=0"""
    cfg = _make_config(
        enabled=False, log_only=True,
        allowed_providers=["codex"],
    )
    rc, output = _run_setup([], cfg)
    assert rc == 0
    assert "警告" not in output
    assert "✅ 診断" in output


def test_no_warning_when_models_allowlist_set():
    """allowed_models.default が設定されていれば警告なし"""
    cfg = _make_config(
        enabled=False, log_only=True,
        allowed_models_default=["claude-sonnet-4"],
    )
    rc, output = _run_setup([], cfg)
    assert rc == 0
    assert "警告" not in output


# ---------------------------------------------------------------------------
# 情報表示: enabled / log_only の状態
# ---------------------------------------------------------------------------


def test_shows_env_override_hint_when_disabled():
    """enabled=false なら env override のヒントを出す"""
    cfg = _make_config(enabled=False)
    rc, output = _run_setup([], cfg)
    assert "HOKUSAI_LLM_GATEWAY_ENABLED" in output


def test_shows_log_only_observation_mode_message():
    """enabled=true + log_only=true は観察モードのメッセージを出す"""
    cfg = _make_config(
        enabled=True, log_only=True,
        allowed_providers=["codex"],
    )
    rc, output = _run_setup([], cfg)
    assert "log_only=true" in output
    assert "観察モード" in output


# ---------------------------------------------------------------------------
# 現設定の表示
# ---------------------------------------------------------------------------


def test_outputs_current_config_values():
    """現 config の値（enabled / log_only / providers 等）が出力に含まれる"""
    cfg = _make_config(
        enabled=True, log_only=False,
        allowed_providers=["codex", "gemini"],
        allowed_models_default=["gpt-4"],
    )
    rc, output = _run_setup([], cfg)
    # 各設定値が表示される
    assert "enabled:" in output
    assert "log_only:" in output
    assert "allowed_providers" in output
    assert "codex" in output
    assert "gemini" in output


def test_returns_error_when_config_has_no_llm_gateway_attr(tmp_path):
    """WorkflowConfig に llm_gateway 属性がない場合 rc=1 + stderr エラー"""
    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.llm_gateway = None

    err_buf = StringIO()
    with patch.object(sys, "stderr", err_buf):
        rc, _ = _run_setup([], cfg)
    assert rc == 1
    assert "llm_gateway" in err_buf.getvalue()
