"""LLM Gateway interceptor 本体（Phase 1: log-only / M1.1: block decision 解禁）

Phase 1 (#39) では block / redact / spend cap などの decision は実装せず、
常に `log` decision を返して prompt を透過させていた。M1.1 (#86) で
`log_only=False` + `policy_hits` 非空のときに `decision="block"` を返す経路を
追加した（実際の送信抑止 = dispatch 側で BLOCK を見て止める配線は M1.2 以降）。
送信 prompt の hash と context を構造化 log として残し、後段（detector / cap
/ approval gate）の着手前に「どの phase / model / provider がどれだけ呼ばれて
いるか」を可視化することが目的。

例外は呼び出し側が握り潰す前提（既存フローへの影響をゼロに保つため、
ClaudeCodeClient 側で try/except する）。本クラスは正常系で例外を投げない。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..config.models import LLMGatewayConfig
from ..logging_config import get_logger
from .context import LLMGatewayContext

if TYPE_CHECKING:
    # 循環 import を避けるため TYPE_CHECKING 下で forward reference
    # （_audit_store_cache の型注釈を明確化するため、Issue #80 Copilot Round 2 指摘）
    from ..persistence.sqlite_store import SQLiteStore


# SQLiteStore のモジュールレベルキャッシュ（Issue #80 Copilot Round 3 指摘）。
# dispatch_via_gateway が毎回 LLMGatewayInterceptor を new するため、interceptor
# instance 上のキャッシュは効かない（毎回新しい dict が作られて再利用されない）。
# モジュールレベルに移すことで、同 database_path への 2 回目以降の呼び出しで
# SQLiteStore.__init__()（DDL/PRAGMA/INDEX 作成）の再実行を避ける。
#
# テスト間の干渉を避けるため `_reset_audit_store_cache()` で明示的に clear
# できる。本番では interceptor 寿命より長く生存して再利用される。
_AUDIT_STORE_CACHE: dict[str, "SQLiteStore"] = {}


def _reset_audit_store_cache() -> None:
    """SQLiteStore モジュールキャッシュを clear する（テスト用）。

    テスト間で database_path が変わるケース（tmp_path 毎回違う）で前回の
    キャッシュが残ると無関係な接続を保持し続けるため、テストの autouse
    fixture から呼ぶことを想定。
    """
    _AUDIT_STORE_CACHE.clear()

logger = get_logger("llm_gateway")


# interceptor が発行する decision 値。`Decision` (decisions.py) と語彙を揃える。
# Phase 1 では LOG / SKIPPED のみ使用。M1.1 (#86) で BLOCK を追加し、
# `_config.log_only=False` かつ `policy_hits` が非空のときに返すようにした
# （実際の送信抑止 = dispatch 側で BLOCK を見て止める配線は M1.2 以降）。
DECISION_LOG = "log"
DECISION_SKIPPED = "skipped"  # Gateway 無効化時
DECISION_BLOCK = "block"  # log_only=False + policy_hits 非空のとき

# M1.1 で block decision を返す際の reason。audit log の `reason` フィールドに
# 残り、後段 (Operations Console / 監査) からなぜ block されたかを後追いする
# 入口になる（hit した個別 policy 名は `policy_hits` 配列側に残る）。
REASON_PHASE2_POLICY_BLOCK = "phase2_policy_block"


# Phase 1 §8a で発行する policy_hits 値（Phase 2 で decision="block" 切替の基礎）
POLICY_HIT_UNKNOWN_PROVIDER = "unknown_provider"
POLICY_HIT_UNKNOWN_MODEL = "unknown_model"
POLICY_HIT_HIGH_COST_MODEL = "high_cost_model"


@dataclass(frozen=True)
class InterceptorDecision:
    """interceptor の判定結果

    `decision` の取りうる値:
    - "log": 透過動作（Phase 1 既存）
    - "skipped": Gateway 無効化時
    - "block": `log_only=False` + `policy_hits` 非空時 (M1.1 / #86)
    - "warn" / "redact": Phase 5+ で追加予定

    `policy_hits` は Phase 1 §8a で追加された policy 評価結果。M1.1 で
    `log_only=False` のときに decision="block" の判定材料として利用するよう
    になった（`log_only=True` 時はこれまで通り audit に積むのみで動作には
    影響させない）。
    """

    decision: str
    reason: str
    audit_emitted: bool = False
    policy_hits: tuple[str, ...] = ()


class LLMGatewayInterceptor:
    """LLM 送信前 governance interceptor（Phase 1 log-only + M1.1 block 解禁）"""

    def __init__(self, config: LLMGatewayConfig):
        self._config = config
        # SQLiteStore キャッシュはモジュールレベル (_AUDIT_STORE_CACHE) で管理する。
        # interceptor instance 上に持つと dispatch_via_gateway が毎回 new するため
        # 効かない（Issue #80 Copilot Round 3 指摘）。

    def intercept(
        self, context: LLMGatewayContext, prompt: str
    ) -> InterceptorDecision:
        """prompt 送信直前に呼び、decision を返す。

        Gateway が無効化されている場合は `decision="skipped"` を返し、有効化
        されている場合は policy_hits を評価して decision を組み立てる。

        **decision 決定ルール** (M1.1 / #86):

        - `log_only=True` (default) → 常に `decision="log"`。Phase 1 同等の
          透過動作で、policy_hits の有無に関わらず block しない。
        - `log_only=False` かつ `policy_hits` が非空 → `decision="block"`,
          `reason="phase2_policy_block"`。送信を止めたい意思表示を audit に
          残す（実際の送信抑止は dispatch 側で BLOCK を見て止める配線が
          M1.2 以降で入る予定）。
        - `log_only=False` かつ `policy_hits` 空 → `decision="log"`。hit が
          無いなら block 対象なしなので透過動作を維持する。

        Args:
            context: 呼び出し context（少なくとも provider は埋める）
            prompt: 送信予定 prompt（hash / length のみ記録、本文は保存しない）

        Returns:
            InterceptorDecision（`decision` は "log" / "skipped" / "block"）
        """
        if not self._config.enabled:
            return InterceptorDecision(
                decision=DECISION_SKIPPED, reason="gateway_disabled"
            )

        policy_hits = self._evaluate_policy_hits(context)

        if not self._config.log_only and policy_hits:
            decision = DECISION_BLOCK
            reason = REASON_PHASE2_POLICY_BLOCK
        elif self._config.dry_run:
            decision = DECISION_LOG
            reason = "dry_run_log_only"
        else:
            decision = DECISION_LOG
            reason = "phase1_log_only"

        audit_emitted = False
        if self._config.audit_log_enabled:
            self._emit_audit(context, prompt, decision, reason, policy_hits)
            audit_emitted = True

        return InterceptorDecision(
            decision=decision,
            reason=reason,
            audit_emitted=audit_emitted,
            policy_hits=policy_hits,
        )

    def _evaluate_policy_hits(
        self, context: LLMGatewayContext
    ) -> tuple[str, ...]:
        """LLMGatewayConfig の policy schema を log-only 評価する（要件 §8a）

        Phase 1 では block しないため、hit を audit log に積むだけ。Phase 2
        enforcement PR で hit 内容に応じて decision を block 等に切り替える。

        - `allowed_providers`: None なら未指定として skip（要件 §4.2）。
          list なら context.provider が含まれない場合 "unknown_provider" を hit。
        - `allowed_models.default`: None なら skip。list でも context.model が
          空文字（呼び出し側で取得できなかった場合）のときは誤検知防止のため
          skip。それ以外で context.model が含まれない場合 "unknown_model" を hit。
        - `allowed_models.high_cost_requires_gate`: 空 list なら skip。非空でも
          context.model が空文字なら skip。context.model が含まれる場合
          "high_cost_model" を hit（後続 PR で approval gate と接続される予定）。

        **空 model の扱い**: `LLMGatewayContext.model` は呼び出し側が model 名を
        取得できないとき "" になる（例: `ClaudeCodeClient` は現状 model を context
        に埋めない）。空文字を「allowlist にない」と判定すると `unknown_model`
        が常時 hit して audit が誤検知だらけになるため、空文字は評価 skip と
        する（Copilot Round 1 指摘）。
        """
        hits: list[str] = []

        allowed_providers = self._config.allowed_providers
        if (
            allowed_providers is not None
            and context.provider not in allowed_providers
        ):
            hits.append(POLICY_HIT_UNKNOWN_PROVIDER)

        # 空 model は誤検知防止のため allowed_models 系の evaluation を skip
        if context.model:
            allowed_default = self._config.allowed_models.default
            if (
                allowed_default is not None
                and context.model not in allowed_default
            ):
                hits.append(POLICY_HIT_UNKNOWN_MODEL)

            high_cost = self._config.allowed_models.high_cost_requires_gate
            if high_cost and context.model in high_cost:
                hits.append(POLICY_HIT_HIGH_COST_MODEL)

        return tuple(hits)

    def _emit_audit(
        self,
        context: LLMGatewayContext,
        prompt: str,
        decision: str,
        reason: str,
        policy_hits: tuple[str, ...] = (),
    ) -> None:
        """構造化 log entry を出力する。

        prompt 本文は保存せず length / sha256 16 桁 hex のみを残す。secret
        / PII を log にこぼさないため（要件定義 §14 受け入れ基準）。

        **永続化** (Issue #80 / M0.1): logger.info で 1 行構造化ログを出すと
        同時に、`context.workflow_id` が埋まっていれば SQLite `audit_logs`
        テーブルにも INSERT する。Phase 2 enforcement で「なぜ block されたか」
        を後追いできる土台。workflow_id が None / "" のときは orphan レコード
        回避と NOT NULL 違反回避のため SQLite 書き込みを skip（logger 出力は
        継続）。なお SQLite 側は `PRAGMA foreign_keys=OFF` デフォルトのため、
        FK 自体は検証されない。
        """
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        # phase を logger / SQLite で同じ値に揃える（None なら 0 sentinel に正規化）。
        # SQLite 側は `audit_logs.phase` が INTEGER NOT NULL のため None を入れられず、
        # logger 側だけ None で残ると同一レコード内で表現が割れるので統一する
        # （Issue #80 Copilot Round 1 指摘）。
        phase_normalized = context.phase if context.phase is not None else 0
        # dataclasses.asdict は MappingProxyType を deepcopy しようとして
        # `cannot pickle 'mappingproxy' object` で落ちるため、context dict は
        # 明示的に組み立てる（metadata は dict にコピーして展開）。
        context_dict = {
            "provider": context.provider,
            "model": context.model,
            "purpose": context.purpose,
            "workflow_id": context.workflow_id,
            "phase": phase_normalized,
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
            # Phase 1 §8a: log-only 評価で hit した policy 名のリスト。
            # 空 list なら policy 違反候補なし。Phase 2 enforcement PR で
            # この内容に応じて decision を block 等に切り替える前段。
            "policy_hits": list(policy_hits),
            # 監査上「どの設定で動いていたか」を再現できるよう、interceptor
            # に渡された LLMGatewayConfig の実値をそのまま記録する。
            # Phase 1 では log_only / dry_run は decision に影響しないが、
            # ユーザーが `log_only=False` を設定したケースも audit に残す
            # ことで Phase 5+ への移行検証を可能にする（PR #40 Copilot 3
            # 回目指摘: ハードコード/推定ではなく実値を記録）。
            "config_snapshot": {
                "enabled": self._config.enabled,
                "log_only": self._config.log_only,
                "dry_run": self._config.dry_run,
                "audit_log_enabled": self._config.audit_log_enabled,
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

        # SQLite audit_logs テーブルへの永続化（Issue #80 / M0.1）。
        # logger.info 出力とは独立に SQLite に書く。workflow_id が無い
        # interceptor 呼び出し（CLI 起動初期 / テスト等）は orphan レコード
        # 回避と NOT NULL 違反回避のため skip。書き込み失敗は完全に握り潰す
        # （fail-open 原則）。
        if context.workflow_id:
            self._persist_audit_to_sqlite(
                workflow_id=context.workflow_id,
                phase=phase_normalized,
                decision=decision,
                entry=entry,
            )

    def _persist_audit_to_sqlite(
        self,
        *,
        workflow_id: str,
        phase: int,
        decision: str,
        entry: dict,
    ) -> None:
        """audit entry を SQLite audit_logs テーブルに INSERT する（Issue #80 / M0.1）。

        既存 `SQLiteStore.add_audit_log` を再利用する。action は固定で
        ``"llm_gateway_decision"``、status は decision 値（"log" / "skipped"
        / Phase 2 以降 "block" 等）、details_json は entry 全体。

        失敗時は debug log に型名 + frame のみ残して呼び出し側へは伝播
        させない（`dispatch.log_suppressed_exception` と同じ思想）。

        SQLiteStore は database_path 単位でモジュールレベルキャッシュし、同
        path での 2 回目以降の呼び出しは DDL/PRAGMA/INDEX の再実行を避ける
        （Issue #80 Copilot Round 1/3 指摘: レイテンシ / ロック競合対策。
        dispatch_via_gateway が interceptor を毎回 new するためモジュール
        レベルに置く必要がある）。
        """
        try:
            from ..config import get_config
            from ..persistence.sqlite_store import SQLiteStore

            config = get_config()
            db_path_key = str(config.database_path)
            store = _AUDIT_STORE_CACHE.get(db_path_key)
            if store is None:
                store = SQLiteStore(config.database_path)
                _AUDIT_STORE_CACHE[db_path_key] = store
            store.add_audit_log(
                workflow_id=workflow_id,
                phase=phase,
                action="llm_gateway_decision",
                status=decision,
                details=entry,
            )
        except Exception as exc:
            try:
                from .dispatch import log_suppressed_exception

                log_suppressed_exception(
                    "LLM Gateway audit log の SQLite 永続化に失敗（logger 出力は継続）",
                    exc,
                )
            except Exception:
                logger.debug(
                    "LLM Gateway audit log SQLite 永続化に失敗 (type=%s)",
                    type(exc).__name__,
                )
