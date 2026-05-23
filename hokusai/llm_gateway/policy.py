"""LLM Gateway policy 関連の定数列挙（Issue #58 / 要件 §7.1 / §8.1 / §8.2）

- `DetectorRule`: PII / secret detector の rule 名（要件 §7.1。YAML
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
    """Human Approval gate が必要になる理由の識別子（要件 §8.1）。

    Phase 1 schema の `LLMGatewayApprovalsConfig` には現在 3 キー
    （high_cost_model / pii_send_without_redaction / policy_override）のみ
    存在するが、本 enum は要件 §8.1 の 6 条件すべてを定義する。残り 3 条件
    （spend_cap_override / unknown_usage_provider / strict_profile_external_llm）
    は後続 rollout step（spend tracking / provider 制御 / strict profile）
    実装時に config 側へ追加される予定。

    そのため `LLMGatewayApprovalsConfig` のキーと 1:1 ではなく、enum が
    superset を持つ形式（後続拡張のための予約）。
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

    後続 rollout step で Workflow Gates DB と接続する予定。**現時点では
    既存 `WorkflowGatesDBClient` の `ALL_GATE_TYPES` enum に本 `llm_*` 値は
    含まれていない**ため、接続時に以下のいずれかの方針で実装する必要がある:

    1. Workflow Gates 側の `ALL_GATE_TYPES` に `llm_*` を追加（schema 拡張）
    2. Gate 作成時は `human_approval` 等の既存 type を使い、reason / 補助
       プロパティで LLM Gateway 由来であることを識別する

    本 enum は方針 (1) を採用する場合の参照識別子として、または方針 (2)
    の reason 識別子として使える形に定義してある。接続 PR で方針を決定して
    schema or 呼び出しコードを揃えること。
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
