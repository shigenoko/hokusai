"""LLM Gateway config schema + loader テスト（Issue #58 / 要件 §4.1）

PR #39 の最小 schema（enabled / log_only 等）に追加した:
- decisions / policy 定数の整合
- フル schema の dataclass 既定値
- YAML 解析（部分指定 / 不正型 fallback / enum 検証）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.config.loaders import _parse_llm_gateway_config
from hokusai.config.models import (
    LLMGatewayAllowedModelsConfig,
    LLMGatewayApprovalsConfig,
    LLMGatewayAuditConfig,
    LLMGatewayConfig,
    LLMGatewayPiiRedactionConfig,
    LLMGatewaySpendCapConfig,
)
from hokusai.llm_gateway import (
    ALL_APPROVAL_GATE_TYPES,
    ALL_APPROVAL_REASONS,
    ALL_DECISIONS,
    ALL_DETECTOR_RULES,
    ALL_FAIL_MODES,
    ALL_REDACTION_ACTIONS,
    ApprovalGateType,
    ApprovalReason,
    Decision,
    DetectorRule,
    FailMode,
    RedactionAction,
    is_valid_approval_gate_type,
    is_valid_approval_reason,
    is_valid_decision,
    is_valid_detector_rule,
    is_valid_fail_mode,
    is_valid_redaction_action,
)


# ---------------------------------------------------------------------------
# decisions / policy 定数
# ---------------------------------------------------------------------------


def test_decision_constants_cover_all_required_values():
    """要件 §4.3 の 6 decision がすべて enum に含まれる"""
    assert Decision.ALLOW in ALL_DECISIONS
    assert Decision.REDACT in ALL_DECISIONS
    assert Decision.WARN in ALL_DECISIONS
    assert Decision.LOG in ALL_DECISIONS
    assert Decision.BLOCK in ALL_DECISIONS
    assert Decision.REQUIRE_HUMAN_APPROVAL in ALL_DECISIONS
    assert len(ALL_DECISIONS) == 6


def test_is_valid_decision():
    assert is_valid_decision("allow")
    assert is_valid_decision("block")
    assert not is_valid_decision("unknown")
    assert not is_valid_decision(None)
    assert not is_valid_decision(42)


def test_fail_mode_constants():
    """spend_cap.fail_mode / pii_redaction.fail_mode の取りうる 3 値"""
    assert FailMode.BLOCK in ALL_FAIL_MODES
    assert FailMode.WARN in ALL_FAIL_MODES
    assert FailMode.LOG in ALL_FAIL_MODES
    assert len(ALL_FAIL_MODES) == 3
    assert is_valid_fail_mode("block")
    assert not is_valid_fail_mode("unknown")


def test_redaction_action_constants():
    """PII / secret 検出時の action 5 種（要件 §7.2）"""
    assert RedactionAction.REDACT in ALL_REDACTION_ACTIONS
    assert RedactionAction.WARN in ALL_REDACTION_ACTIONS
    assert RedactionAction.BLOCK in ALL_REDACTION_ACTIONS
    assert RedactionAction.LOG in ALL_REDACTION_ACTIONS
    assert RedactionAction.REQUIRE_HUMAN_APPROVAL in ALL_REDACTION_ACTIONS
    assert len(ALL_REDACTION_ACTIONS) == 5
    assert is_valid_redaction_action("redact")
    assert not is_valid_redaction_action("unknown")


def test_detector_rule_constants():
    """MVP 対応の 6 detector（要件 §7.1）"""
    assert DetectorRule.EMAIL in ALL_DETECTOR_RULES
    assert DetectorRule.JP_PHONE_NUMBER in ALL_DETECTOR_RULES
    assert DetectorRule.CREDIT_CARD in ALL_DETECTOR_RULES
    assert DetectorRule.JP_MY_NUMBER in ALL_DETECTOR_RULES
    assert DetectorRule.SECRET_LIKE_TOKEN in ALL_DETECTOR_RULES
    assert DetectorRule.ENV_SECRET_REFERENCE in ALL_DETECTOR_RULES
    assert len(ALL_DETECTOR_RULES) == 6
    assert is_valid_detector_rule("email")
    assert not is_valid_detector_rule("unknown")


def test_approval_reason_constants():
    """Human Approval が必要な 6 条件（要件 §8.1）"""
    assert ApprovalReason.HIGH_COST_MODEL in ALL_APPROVAL_REASONS
    assert ApprovalReason.PII_SEND_WITHOUT_REDACTION in ALL_APPROVAL_REASONS
    assert ApprovalReason.POLICY_OVERRIDE in ALL_APPROVAL_REASONS
    assert ApprovalReason.SPEND_CAP_OVERRIDE in ALL_APPROVAL_REASONS
    assert ApprovalReason.UNKNOWN_USAGE_PROVIDER in ALL_APPROVAL_REASONS
    assert ApprovalReason.STRICT_PROFILE_EXTERNAL_LLM in ALL_APPROVAL_REASONS
    assert len(ALL_APPROVAL_REASONS) == 6
    assert is_valid_approval_reason("high_cost_model")


def test_approval_gate_type_constants():
    """Workflow Gates DB と連携する 4 gate type（要件 §8.2）"""
    assert ApprovalGateType.LLM_HIGH_COST_MODEL_APPROVAL in ALL_APPROVAL_GATE_TYPES
    assert ApprovalGateType.LLM_SPEND_CAP_OVERRIDE in ALL_APPROVAL_GATE_TYPES
    assert ApprovalGateType.LLM_PII_SEND_APPROVAL in ALL_APPROVAL_GATE_TYPES
    assert ApprovalGateType.LLM_POLICY_OVERRIDE in ALL_APPROVAL_GATE_TYPES
    assert len(ALL_APPROVAL_GATE_TYPES) == 4
    assert is_valid_approval_gate_type("llm_high_cost_model_approval")
    assert not is_valid_approval_gate_type("unknown")


# ---------------------------------------------------------------------------
# LLMGatewayConfig 既定値
# ---------------------------------------------------------------------------


def test_llm_gateway_config_defaults():
    cfg = LLMGatewayConfig()
    # 既存フィールド（PR #39）の後方互換
    assert cfg.enabled is False
    assert cfg.dry_run is False
    assert cfg.log_only is True
    assert cfg.audit_log_enabled is True
    # 新フィールド（Issue #58）
    assert cfg.allowed_providers == []
    assert isinstance(cfg.allowed_models, LLMGatewayAllowedModelsConfig)
    assert cfg.allowed_models.default == []
    assert cfg.allowed_models.high_cost_requires_gate == []
    assert isinstance(cfg.spend_cap, LLMGatewaySpendCapConfig)
    assert cfg.spend_cap.monthly_jpy is None
    assert cfg.spend_cap.fail_mode == "block"
    assert isinstance(cfg.pii_redaction, LLMGatewayPiiRedactionConfig)
    assert cfg.pii_redaction.enabled is False
    assert cfg.pii_redaction.default_action == "redact"
    assert isinstance(cfg.approvals, LLMGatewayApprovalsConfig)
    assert cfg.approvals.high_cost_model == "disabled"
    assert isinstance(cfg.audit, LLMGatewayAuditConfig)
    assert cfg.audit.store_prompt_hash is True
    assert cfg.audit.store_full_prompt is False


# ---------------------------------------------------------------------------
# loader（YAML 解析）
# ---------------------------------------------------------------------------


def test_loader_returns_defaults_when_section_missing():
    cfg = _parse_llm_gateway_config({})
    assert cfg.enabled is False
    assert cfg.allowed_providers == []


def test_loader_returns_defaults_when_section_not_dict():
    cfg = _parse_llm_gateway_config({"llm_gateway": "not a dict"})
    assert cfg.enabled is False


def test_loader_parses_full_schema():
    """要件 §4.1 のフル schema YAML が正しく解析される"""
    raw = {
        "llm_gateway": {
            "enabled": True,
            "allowed_providers": ["openai", "anthropic"],
            "allowed_models": {
                "default": ["gpt-5.4", "claude-sonnet-4.5"],
                "high_cost_requires_gate": ["gpt-5.5"],
            },
            "spend_cap": {
                "monthly_jpy": 50000,
                "daily_jpy": 5000,
                "per_workflow_jpy": 500,
                "per_phase_jpy": 200,
                "fail_mode": "block",
            },
            "pii_redaction": {
                "enabled": True,
                "rules": ["email", "jp_phone_number"],
                "default_action": "redact",
                "fail_mode": "block",
            },
            "approvals": {
                "high_cost_model": "required",
                "pii_send_without_redaction": "required",
                "policy_override": "required",
            },
            "audit": {
                "store_prompt_hash": True,
                "store_redacted_preview": True,
                "store_full_prompt": False,
            },
        }
    }
    cfg = _parse_llm_gateway_config(raw)
    assert cfg.enabled is True
    assert cfg.allowed_providers == ["openai", "anthropic"]
    assert cfg.allowed_models.default == ["gpt-5.4", "claude-sonnet-4.5"]
    assert cfg.allowed_models.high_cost_requires_gate == ["gpt-5.5"]
    assert cfg.spend_cap.monthly_jpy == 50000
    assert cfg.spend_cap.fail_mode == "block"
    assert cfg.pii_redaction.enabled is True
    assert cfg.pii_redaction.rules == ["email", "jp_phone_number"]
    assert cfg.pii_redaction.default_action == "redact"
    assert cfg.approvals.high_cost_model == "required"
    assert cfg.audit.store_redacted_preview is True


def test_loader_filters_invalid_detector_rules():
    """rules に未知 / 非 str 値が混入したら除外して採用する"""
    raw = {
        "llm_gateway": {
            "pii_redaction": {
                "rules": ["email", "unknown_detector", 42, "credit_card"]
            }
        }
    }
    cfg = _parse_llm_gateway_config(raw)
    # email / credit_card のみ採用、unknown_detector と 42 は除外
    assert cfg.pii_redaction.rules == ["email", "credit_card"]


def test_loader_falls_back_to_default_for_invalid_fail_mode():
    """fail_mode に enum 外の値を渡したら既定値 (block) にフォールバック"""
    raw = {
        "llm_gateway": {
            "spend_cap": {"fail_mode": "not_a_real_mode"},
            "pii_redaction": {"fail_mode": "invalid"},
        }
    }
    cfg = _parse_llm_gateway_config(raw)
    assert cfg.spend_cap.fail_mode == "block"
    assert cfg.pii_redaction.fail_mode == "block"


def test_loader_falls_back_for_invalid_redaction_default_action():
    raw = {
        "llm_gateway": {
            "pii_redaction": {"default_action": "not_a_real_action"}
        }
    }
    cfg = _parse_llm_gateway_config(raw)
    assert cfg.pii_redaction.default_action == "redact"


def test_loader_falls_back_for_invalid_approval_level():
    """approvals.* に required/optional/disabled 以外を渡したら既定にフォールバック"""
    raw = {
        "llm_gateway": {
            "approvals": {"high_cost_model": "not_a_level"}
        }
    }
    cfg = _parse_llm_gateway_config(raw)
    assert cfg.approvals.high_cost_model == "disabled"


def test_loader_filters_non_str_in_allowed_providers():
    raw = {
        "llm_gateway": {
            "allowed_providers": ["openai", 42, {"obj": True}, "anthropic"],
        }
    }
    cfg = _parse_llm_gateway_config(raw)
    assert cfg.allowed_providers == ["openai", "anthropic"]


def test_loader_treats_bool_as_invalid_int_in_spend_cap():
    """bool は isinstance(True, int) で True になってしまうため除外する
    （`True` を spend_cap.monthly_jpy として受け取ると 1 として動作してしまう）"""
    raw = {
        "llm_gateway": {
            "spend_cap": {"monthly_jpy": True}
        }
    }
    cfg = _parse_llm_gateway_config(raw)
    assert cfg.spend_cap.monthly_jpy is None


def test_loader_preserves_backward_compat_with_minimal_schema():
    """PR #39 の最小 schema のみ指定でも既定値で fill されて動く"""
    raw = {
        "llm_gateway": {
            "enabled": True,
            "log_only": False,
        }
    }
    cfg = _parse_llm_gateway_config(raw)
    assert cfg.enabled is True
    assert cfg.log_only is False
    # 新フィールドは既定値
    assert cfg.allowed_providers == []
    assert cfg.spend_cap.monthly_jpy is None
    assert cfg.pii_redaction.enabled is False
