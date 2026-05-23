"""LLM Gateway policy 関連の定数列挙（Issue #58 / 要件 §7.1 / §8.2）

- `DetectorRule`: PII / secret detector の rule 名（YAML
  `pii_redaction.rules` の取りうる値）。
- `ApprovalReason`: Human Approval gate が必要になる条件名（要件 §8.1）。
- `ApprovalGateType`: 作成される Workflow Gate の type（要件 §8.2、既存
  WorkflowGatesDBClient と接続予定）。

実 enforcement は後続 PR で行う。本 module は schema / 後続実装の語彙統一を
目的とする。
"""

from __future__ import annotations


class DetectorRule:
    """PII / secret detector の rule 名（要件 §7.1）。

    YAML の `pii_redaction.rules` 配列に列挙される値。詳細な検出ロジックは
    後続 rollout step（detector / redaction 実装）で追加する。
    """

    EMAIL = "email"
    JP_PHONE_NUMBER = "jp_phone_number"
    CREDIT_CARD = "credit_card"
    JP_MY_NUMBER = "jp_my_number"
    SECRET_LIKE_TOKEN = "secret_like_token"
    ENV_SECRET_REFERENCE = "env_secret_reference"


ALL_DETECTOR_RULES = frozenset({
    DetectorRule.EMAIL,
    DetectorRule.JP_PHONE_NUMBER,
    DetectorRule.CREDIT_CARD,
    DetectorRule.JP_MY_NUMBER,
    DetectorRule.SECRET_LIKE_TOKEN,
    DetectorRule.ENV_SECRET_REFERENCE,
})


def is_valid_detector_rule(value: object) -> bool:
    return isinstance(value, str) and value in ALL_DETECTOR_RULES


class ApprovalReason:
    """Human Approval gate が必要になる理由（要件 §8.1）。

    `LLMGatewayApprovalsConfig` の各キーに対応する識別子としても使う。
    """

    HIGH_COST_MODEL = "high_cost_model"
    PII_SEND_WITHOUT_REDACTION = "pii_send_without_redaction"
    POLICY_OVERRIDE = "policy_override"
    SPEND_CAP_OVERRIDE = "spend_cap_override"
    UNKNOWN_USAGE_PROVIDER = "unknown_usage_provider"
    STRICT_PROFILE_EXTERNAL_LLM = "strict_profile_external_llm"


ALL_APPROVAL_REASONS = frozenset({
    ApprovalReason.HIGH_COST_MODEL,
    ApprovalReason.PII_SEND_WITHOUT_REDACTION,
    ApprovalReason.POLICY_OVERRIDE,
    ApprovalReason.SPEND_CAP_OVERRIDE,
    ApprovalReason.UNKNOWN_USAGE_PROVIDER,
    ApprovalReason.STRICT_PROFILE_EXTERNAL_LLM,
})


def is_valid_approval_reason(value: object) -> bool:
    return isinstance(value, str) and value in ALL_APPROVAL_REASONS


class ApprovalGateType:
    """LLM Gateway が作成する Workflow Gate の type（要件 §8.2）。

    既存 `WorkflowGatesDBClient` の gate_type と整合する文字列を返す。
    後続 PR で実 gate 作成パスと接続する際に使う。
    """

    LLM_HIGH_COST_MODEL_APPROVAL = "llm_high_cost_model_approval"
    LLM_SPEND_CAP_OVERRIDE = "llm_spend_cap_override"
    LLM_PII_SEND_APPROVAL = "llm_pii_send_approval"
    LLM_POLICY_OVERRIDE = "llm_policy_override"


ALL_APPROVAL_GATE_TYPES = frozenset({
    ApprovalGateType.LLM_HIGH_COST_MODEL_APPROVAL,
    ApprovalGateType.LLM_SPEND_CAP_OVERRIDE,
    ApprovalGateType.LLM_PII_SEND_APPROVAL,
    ApprovalGateType.LLM_POLICY_OVERRIDE,
})


def is_valid_approval_gate_type(value: object) -> bool:
    return isinstance(value, str) and value in ALL_APPROVAL_GATE_TYPES
