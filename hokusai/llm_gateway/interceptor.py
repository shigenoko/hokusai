"""LLM Gateway interceptor 本体（Phase 1: log-only）

Phase 1 (#39) では block / redact / spend cap などの decision は実装せず、
常に `log` decision を返して prompt を透過させる。送信 prompt の hash と
context を構造化 log として残し、後段（detector / cap / approval gate）の
着手前に「どの phase / model / provider がどれだけ呼ばれているか」を可視化
することが目的。

例外は呼び出し側が握り潰す前提（既存フローへの影響をゼロに保つため、
ClaudeCodeClient 側で try/except する）。本クラスは正常系で例外を投げない。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from ..config.models import LLMGatewayConfig
from ..logging_config import get_logger
from .context import LLMGatewayContext

logger = get_logger("llm_gateway")


# Phase 1 で発行する decision 値（Phase 5+ で "block" / "warn" / "redact" 追加予定）
DECISION_LOG = "log"
DECISION_SKIPPED = "skipped"  # Gateway 無効化時


@dataclass(frozen=True)
class InterceptorDecision:
    """interceptor の判定結果

    Phase 1 では `decision` は "log" または "skipped" のみ。Phase 5+ で
    "block" / "warn" / "redact" を追加する。
    """

    decision: str
    reason: str
    audit_emitted: bool = False


class LLMGatewayInterceptor:
    """LLM 送信前 governance interceptor（Phase 1: log-only）"""

    def __init__(self, config: LLMGatewayConfig):
        self._config = config

    def intercept(
        self, context: LLMGatewayContext, prompt: str
    ) -> InterceptorDecision:
        """prompt 送信直前に呼び、decision を返す。

        Phase 1 は block しない。Gateway が無効化されている場合は
        `decision="skipped"` を返し、有効化されている場合は decision="log"
        を返しつつ audit_log_enabled なら構造化 log entry を残す。

        **`LLMGatewayConfig.log_only` フィールドは Phase 1 では未使用**
        （schema / loader / audit log には残し、Phase 5+ で block decision
        が解禁された際に「block するか log のみで止めるか」を制御する
        フラグとして利用予定）。Phase 1 では実質常に log-only 動作なので
        参照しなくても挙動は変わらない（PR #40 Copilot 2 回目指摘）。

        Args:
            context: 呼び出し context（少なくとも provider は埋める）
            prompt: 送信予定 prompt（hash / length のみ記録、本文は保存しない）

        Returns:
            InterceptorDecision（Phase 1 は常に "log" or "skipped"）
        """
        if not self._config.enabled:
            return InterceptorDecision(
                decision=DECISION_SKIPPED, reason="gateway_disabled"
            )

        if self._config.dry_run:
            reason = "dry_run_log_only"
        else:
            reason = "phase1_log_only"

        audit_emitted = False
        if self._config.audit_log_enabled:
            self._emit_audit(context, prompt, DECISION_LOG, reason)
            audit_emitted = True

        return InterceptorDecision(
            decision=DECISION_LOG, reason=reason, audit_emitted=audit_emitted
        )

    @staticmethod
    def _emit_audit(
        context: LLMGatewayContext,
        prompt: str,
        decision: str,
        reason: str,
    ) -> None:
        """構造化 log entry を出力する。

        prompt 本文は保存せず length / sha256 16 桁 hex のみを残す。secret
        / PII を log にこぼさないため（要件定義 §14 受け入れ基準）。Phase 5+
        で sqlite 永続化 / Notion 同期に拡張するが、Phase 1 は logger のみ。
        """
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        # dataclasses.asdict は MappingProxyType を deepcopy しようとして
        # `cannot pickle 'mappingproxy' object` で落ちるため、context dict は
        # 明示的に組み立てる（metadata は dict にコピーして展開）。
        context_dict = {
            "provider": context.provider,
            "model": context.model,
            "purpose": context.purpose,
            "workflow_id": context.workflow_id,
            "phase": context.phase,
            "metadata": dict(context.metadata),
        }
        entry = {
            "event": "llm_gateway_decision",
            "timestamp": datetime.now().isoformat(),
            "decision": decision,
            "reason": reason,
            "context": context_dict,
            "prompt_length": len(prompt),
            "prompt_hash": prompt_hash,
            # Phase 1 では log_only / dry_run は decision に影響しないが、
            # 監査上「どの設定で動いていたか」を明示するため audit に残す
            # （PR #40 Copilot 2 回目指摘）
            "config_snapshot": {
                "log_only": True,  # Phase 1 は実質的に常に True
                "dry_run": reason == "dry_run_log_only",
            },
        }
        # JSON 形式で 1 行に出力（後で grep / jq 解析しやすい形）。
        # metadata に非 JSON-serializable な値（Path 等）が混ざっていても
        # default=str で文字列化して落ちないようにする（PR #40 Copilot 1
        # 回目指摘）。
        logger.info(
            "llm_gateway_audit %s",
            json.dumps(entry, ensure_ascii=False, default=str),
        )
