"""`hokusai llm-gateway-setup` サブコマンドのテスト（PR #125 / F2）

dogfooding-findings.md §7 F2 で記録した「`allowed_providers=None` 既定で
policy_hits が常時空 → `log_only=false` に切り替えても enforcement が
事実上 no-op」という事故を踏む前に警告する wizard の回帰防止テスト。
"""

from __future__ import annotations

import sys
from io import StringIO
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
    high_cost_requires_gate: list[str] | None = None,
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
            high_cost_requires_gate=high_cost_requires_gate or [],
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


def test_warns_when_provider_empty_list_as_deny_all():
    """allowed_providers=[] は no-op ではなく deny-all として別カテゴリで警告
    （PR #125 Copilot Round 1 指摘: None と [] を区別、interceptor 仕様に整合）"""
    cfg = _make_config(allowed_providers=[])
    rc, output = _run_setup([], cfg)
    assert rc == 1
    # deny-all 警告が出る（no-op 警告ではない）
    assert "deny-all" in output
    assert "全 LLM 呼び出しが block" in output


def test_warns_when_models_empty_list_as_deny_all():
    """allowed_models.default=[] も deny-all として警告"""
    cfg = _make_config(allowed_models_default=[])
    rc, output = _run_setup([], cfg)
    assert rc == 1
    assert "deny-all" in output


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


def test_warns_when_only_models_allowlist_set_without_providers():
    """allowed_models.default だけ設定でも allowed_providers が None なら警告
    （PR #125 Copilot Round 6 指摘: interceptor は context.model='' で
    allowed_models 評価を skip するため、ClaudeCodeClient (model='') 経由の
    呼び出しは policy_hits 常時空で no-op になり得る）"""
    cfg = _make_config(
        enabled=False, log_only=True,
        allowed_providers=None,
        allowed_models_default=["claude-sonnet-4"],
    )
    rc, output = _run_setup([], cfg)
    assert rc == 1
    assert "allowed_providers" in output
    assert "no-op" in output or "ClaudeCodeClient" in output


def test_warns_when_only_high_cost_gate_set_without_providers():
    """allowed_models.high_cost_requires_gate のみ非空でも allowed_providers
    が None なら警告（同じく interceptor の model='' skip 仕様）"""
    cfg = _make_config(
        enabled=False, log_only=True,
        allowed_providers=None,
        allowed_models_default=None,
        high_cost_requires_gate=["gpt-5", "claude-opus-4"],
    )
    rc, output = _run_setup([], cfg)
    assert rc == 1
    assert "allowed_providers" in output


def test_no_warning_when_providers_allowlist_set():
    """allowed_providers が allowlist として設定されていれば警告なし
    （interceptor の provider 評価は model に関わらず動作する真の経路）"""
    cfg = _make_config(
        enabled=False, log_only=True,
        allowed_providers=["claude_code", "codex"],
    )
    rc, output = _run_setup([], cfg)
    assert rc == 0
    assert "⚠️  警告:" not in output  # 警告ブロックなし


def test_info_when_providers_set_but_models_empty():
    """allowed_providers 設定済みで allowed_models.* 両方空なら ℹ️ info 注記
    （warning は出さない、provider allowlist のみで動作）"""
    cfg = _make_config(
        enabled=False, log_only=True,
        allowed_providers=["claude_code"],
        allowed_models_default=None,
        high_cost_requires_gate=[],
    )
    rc, output = _run_setup([], cfg)
    assert rc == 0
    # info 注記が出る（warning ではない）
    assert "ℹ️" in output
    assert "provider allowlist のみで動作" in output


# ---------------------------------------------------------------------------
# 情報表示: enabled / log_only の状態
# ---------------------------------------------------------------------------


def test_shows_env_override_hint_when_disabled():
    """enabled=false なら env override のヒントを出す。policy 未設定で warnings
    があるので rc=1 を期待"""
    cfg = _make_config(enabled=False)
    rc, output = _run_setup([], cfg)
    assert rc == 1
    assert "HOKUSAI_LLM_GATEWAY_ENABLED" in output


def test_shows_log_only_observation_mode_message():
    """enabled=true + log_only=true は観察モードのメッセージを出す"""
    cfg = _make_config(
        enabled=True, log_only=True,
        allowed_providers=["codex"],
    )
    rc, output = _run_setup([], cfg)
    assert rc == 0  # policy 設定済み → warnings なし
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
    assert rc == 0  # policy 設定済み → warnings なし
    # 各設定値が表示される
    assert "enabled:" in output
    assert "log_only:" in output
    assert "allowed_providers" in output
    assert "codex" in output
    assert "gemini" in output


def test_returns_error_when_llm_gateway_attr_is_none():
    """WorkflowConfig.llm_gateway が None の場合 rc=1 + stderr エラー
    （PR #125 Copilot Round 1 指摘: 属性自体は存在するが値が None のケース。
    `cfg.llm_gateway = None` を明示的にセットしてテスト）"""
    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.llm_gateway = None

    err_buf = StringIO()
    with patch.object(sys, "stderr", err_buf):
        rc, _ = _run_setup([], cfg)
    assert rc == 1
    assert "llm_gateway" in err_buf.getvalue()
