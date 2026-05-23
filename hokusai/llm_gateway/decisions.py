"""LLM Gateway decision / action 列挙（Issue #58 / 要件 §4.3 / §7.2）

各 request に対する判定の取りうる値を一箇所に集約する。後続 rollout step
（detector / spend / approval gate enforcement）はここで定義した enum を
返すことで、interceptor 経由の audit log / Operations Console と一貫した
語彙で動作する。
"""

from __future__ import annotations


class Decision:
    """各 LLM request に対する判定（要件 §4.3）。

    `_check_*` 系の interceptor 内部 helper が返す値の語彙としても使う。
    現状は文字列定数。将来 strict typing に移行する場合は `StrEnum` 化を検討。
    """

    ALLOW = "allow"
    REDACT = "redact"
    WARN = "warn"
    LOG = "log"
    BLOCK = "block"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"


ALL_DECISIONS = frozenset({
    Decision.ALLOW,
    Decision.REDACT,
    Decision.WARN,
    Decision.LOG,
    Decision.BLOCK,
    Decision.REQUIRE_HUMAN_APPROVAL,
})


def is_valid_decision(value: object) -> bool:
    """Decision 値の妥当性チェック（YAML 入力等の検証用）"""
    return isinstance(value, str) and value in ALL_DECISIONS


class FailMode:
    """spend cap / PII detection が「実行不能」になった際の挙動（要件 §4.1）。

    `spend_cap.fail_mode` / `pii_redaction.fail_mode` の取りうる値。

    - `block`: 該当 request を送信不可とする
    - `warn`: 警告のみ出して送信は許可する
    - `log`: 記録のみ行い送信は許可する
    """

    BLOCK = "block"
    WARN = "warn"
    LOG = "log"


ALL_FAIL_MODES = frozenset({FailMode.BLOCK, FailMode.WARN, FailMode.LOG})


def is_valid_fail_mode(value: object) -> bool:
    return isinstance(value, str) and value in ALL_FAIL_MODES


class RedactionAction:
    """PII / secret 検出時の action（要件 §7.2）。

    `pii_redaction.default_action` および各 detector rule に紐づく action
    の取りうる値。`Decision` と一部重複する（redact / warn / block / log /
    require_human_approval）が、専用 namespace として分けることで「decision
    全体」と「detection 由来の action」を語彙レベルで区別する。
    """

    REDACT = "redact"
    WARN = "warn"
    BLOCK = "block"
    LOG = "log"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"


ALL_REDACTION_ACTIONS = frozenset({
    RedactionAction.REDACT,
    RedactionAction.WARN,
    RedactionAction.BLOCK,
    RedactionAction.LOG,
    RedactionAction.REQUIRE_HUMAN_APPROVAL,
})


def is_valid_redaction_action(value: object) -> bool:
    return isinstance(value, str) and value in ALL_REDACTION_ACTIONS
