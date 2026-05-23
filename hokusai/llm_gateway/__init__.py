"""LLM Gateway: HOKUSAI から LLM / Agent への送信前 governance layer

Phase 1 (#39 / v0.6.0〜) で導入する薄い縦割り MVP。

公開する API:
- `LLMGatewayContext`: workflow_id / phase / provider / model / purpose / metadata
- `LLMGatewayInterceptor`: 送信前 hook。Phase 1 は log-only decision で透過
- `InterceptorDecision`: interceptor の返り値

詳細は docs/hokusai-llm-gateway-requirements.md（特に §13.3 rollout）と
docs/hokusai-llm-gateway-callsite-inventory.md を参照。
"""

from .context import LLMGatewayContext
from .decisions import (
    ALL_DECISIONS,
    ALL_FAIL_MODES,
    ALL_REDACTION_ACTIONS,
    Decision,
    FailMode,
    RedactionAction,
    is_valid_decision,
    is_valid_fail_mode,
    is_valid_redaction_action,
)
from .dispatch import dispatch_via_gateway
from .interceptor import InterceptorDecision, LLMGatewayInterceptor
from .policy import (
    ALL_APPROVAL_GATE_TYPES,
    ALL_APPROVAL_REASONS,
    ALL_DETECTOR_RULES,
    ApprovalGateType,
    ApprovalReason,
    DetectorRule,
    is_valid_approval_gate_type,
    is_valid_approval_reason,
    is_valid_detector_rule,
)

__all__ = [
    "ALL_APPROVAL_GATE_TYPES",
    "ALL_APPROVAL_REASONS",
    "ALL_DECISIONS",
    "ALL_DETECTOR_RULES",
    "ALL_FAIL_MODES",
    "ALL_REDACTION_ACTIONS",
    "ApprovalGateType",
    "ApprovalReason",
    "Decision",
    "DetectorRule",
    "FailMode",
    "InterceptorDecision",
    "LLMGatewayContext",
    "LLMGatewayInterceptor",
    "RedactionAction",
    "dispatch_via_gateway",
    "is_valid_approval_gate_type",
    "is_valid_approval_reason",
    "is_valid_decision",
    "is_valid_detector_rule",
    "is_valid_fail_mode",
    "is_valid_redaction_action",
]
