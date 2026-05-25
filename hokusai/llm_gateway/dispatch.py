"""LLM Gateway 共通 dispatch helper（Issue #66）

ClaudeCodeClient / CodexClient / GeminiClient で同形の interceptor 呼び出し
boilerplate が重複していたため共通化する。

- `get_config()` から llm_gateway 設定を取得
- gateway が未設定（古い config）なら no-op
- `LLMGatewayContext` を組み立てて `LLMGatewayInterceptor.intercept()` を呼ぶ
- **例外は完全に握り潰し**、debug ログには **例外型名 + frame 一覧**
  （filename:lineno in function）のみを残す。`str(exc)` / traceback 全文は
  記録しない（secret / PII が prompt 経由で例外メッセージに混入したケース
  での log 漏洩を防ぐため。要件 §14 受け入れ基準）

後続の Phase 2 拡張（block / warn / spend tracking / PII detector）はこの
helper に集約することで、3 client に同じ修正を 3 回入れる必要をなくす。
"""

from __future__ import annotations

import traceback
from typing import Mapping

from ..logging_config import get_logger

logger = get_logger("llm_gateway")


class LLMGatewayBlockedError(Exception):
    """LLM Gateway が policy violation により送信を block したことを示す例外
    （Issue #102 / Phase 2 enforcement 本体配線）。

    呼び出し側（3 client: claude_code / codex / gemini）はこの例外を握り潰さず
    上位に伝播させ、LLM への実送信を中断する必要がある。

    fail-open 原則（要件 §4.4 / Issue #90）との関係:
    - Gateway 内部の予期せぬ例外（audit log 永続化失敗 / API 失敗 / context
      不足等）→ 握り潰す = fail-open（workflow 継続）
    - 明示的な policy block（`decision="block"`）→ 握り潰さない = fail-closed
      （意図的 enforcement の結果なので必ず送信抑止）

    例外 message と attributes には provider / purpose / policy_hits / reason
    のみを含め、**prompt 本文は絶対に含めない**（secret/PII が例外メッセージ
    経由で log/stderr に漏洩するリスクを排除、要件 §14 受け入れ基準）。
    """

    def __init__(
        self,
        *,
        provider: str,
        purpose: str,
        policy_hits: tuple[str, ...],
        reason: str,
    ):
        self.provider = provider
        self.purpose = purpose
        self.policy_hits = policy_hits
        self.reason = reason
        super().__init__(
            f"LLM Gateway blocked: provider={provider!r}, "
            f"purpose={purpose!r}, policy_hits={list(policy_hits)}, "
            f"reason={reason!r}"
        )


def log_suppressed_exception(message: str, exc: BaseException) -> None:
    """LLM Gateway 例外を **メッセージ本文を含めずに** debug ログに残す。

    `logger.debug(..., exc_info=True)` だと `exc.args` の文字列が traceback
    と共に log stream に流れてしまい、prompt や secret が呼び出し側から
    例外メッセージに混入したケースで PII が log にこぼれるリスクがある
    （Issue #66 Copilot Round 1 指摘 / 要件 §14 受け入れ基準）。

    本関数は例外型名 + frame 一覧（ファイル / 行番号 / 関数名）のみを
    debug ログに出し、`str(exc)` 由来の文字列は一切 log に含めない。

    Args:
        message: ログの先頭メッセージ（呼び出し文脈の説明、user-controlled な
            値を含めない）
        exc: 抑制した例外

    Notes:
        本関数自体は **絶対に例外を投げない**。`exc.__traceback__ is None`
        （未 raise の例外や traceback が失われたケース）でも空 frame として
        log を出すだけにする（Issue #66 Copilot Round 2 指摘）。
    """
    # 関数全体を try/except で包み「絶対に例外を投げない」保証を強化する
    # （Issue #66 Copilot Round 5 指摘: traceback.extract_tb や logger.debug が
    # 失敗するエッジケースでも呼び出し側に伝播させない）。
    try:
        if exc.__traceback__ is None:
            frame_summary: list[str] = []
        else:
            frames = traceback.extract_tb(exc.__traceback__)
            frame_summary = [
                f"{f.filename}:{f.lineno} in {f.name}" for f in frames
            ]
        logger.debug(
            "%s (type=%s, frames=%s)",
            message,
            type(exc).__name__,
            frame_summary,
        )
    except Exception:
        # 最終防衛線: traceback 抽出も logger も失敗するなら静かに諦める
        pass


def dispatch_via_gateway(
    *,
    provider: str,
    model: str,
    purpose: str,
    prompt: str,
    metadata: Mapping[str, object] | None = None,
    workflow_id: str | None = None,
    phase: int | None = None,
) -> None:
    """LLM 送信前 interceptor を呼ぶ共通 helper（Phase 1: log-only）。

    Args:
        provider: LLM provider 識別子（"claude_code" / "codex" / "gemini" / ...）
        model: 利用 model 名（取得不可なら ""、interceptor 側で空 model は
            evaluation skip される）
        purpose: 呼び出し目的（"cross_review" / "generate" / "skill_execution:..." 等）
        prompt: 送信予定 prompt（interceptor は hash / length のみ記録、
            本文は audit に保存されない）
        metadata: 追加情報（has_schema / file_count / append_system_prompt_hash 等）
        workflow_id: 呼び出し元 HOKUSAI workflow_id（state を持つ node から呼ばれた場合）。
            指定されていれば SQLite `audit_logs` テーブルへの永続化が走る
            （Issue #80 / M0.1）。
        phase: 呼び出し元 phase 番号（state を持つ node から呼ばれた場合）。

    Returns:
        None。M1.1 まで decision は副作用（audit log 出力）のみ。
        Phase 2 enforcement 配線 (Issue #102) で `decision="block"` のときに
        `LLMGatewayBlockedError` を raise するように拡張された。

    Raises:
        LLMGatewayBlockedError: interceptor が `decision="block"` を返した
            ときに raise。呼び出し側で握り潰さず LLM 実送信を中断する用途。
            log_only=True (default) では絶対に発生しない（M1.1 仕様）。

    Notes:
        既存フローへの影響を最小化するため、`LLMGatewayBlockedError` 以外の
        例外は完全に握り潰す（M1.3 §4.4 fail-open 原則）。意図的な policy
        block のみ fail-closed で上位伝播する設計。

        **workflow_id / phase の伝播状況** (Issue #80 / M0.1): 本 PR では
        helper API に optional 引数として追加し、interceptor から SQLite
        永続化経路に渡せるパスを整備した。各 client (ClaudeCodeClient /
        CodexClient / GeminiClient) の `execute_*` / `review_*` メソッドから
        workflow_id を受け取り dispatch_via_gateway に渡す配線は **後続 PR**
        の課題（phase node 側で state["workflow_id"] / state["current_phase"]
        を伝播させる必要があり、影響範囲が広いため別 scope）。
    """
    try:
        from ..config import get_config
        from .context import LLMGatewayContext
        from .interceptor import DECISION_BLOCK, LLMGatewayInterceptor

        config = get_config()
        gateway_config = getattr(config, "llm_gateway", None)
        if gateway_config is None:
            return
        context = LLMGatewayContext(
            provider=provider,
            model=model,
            purpose=purpose,
            workflow_id=workflow_id,
            phase=phase,
            metadata=dict(metadata or {}),
        )
        decision = LLMGatewayInterceptor(gateway_config).intercept(
            context, prompt
        )
        # Issue #102 / Phase 2 enforcement 本体配線:
        # interceptor が明示的に block decision を返した場合は、fail-open 対象
        # 外として LLMGatewayBlockedError を上位伝播し LLM 実送信を中断する。
        # log_only=True (default) では interceptor 側で BLOCK 判定が起きない
        # ため、デフォルト挙動では絶対にこの分岐を通らない（後方互換保証）。
        if decision.decision == DECISION_BLOCK:
            raise LLMGatewayBlockedError(
                provider=provider,
                purpose=purpose,
                policy_hits=decision.policy_hits,
                reason=decision.reason,
            )
    except LLMGatewayBlockedError:
        # 意図的な policy block は fail-closed で上位伝播（M1.3 §4.4）。
        # ここで catch / 再 raise を明示することで、下の generic Exception
        # ブロックに巻き込まれて握り潰されないことを保証する。
        raise
    except Exception as exc:
        # exc_info=True は exc.args 経由で例外メッセージ本文を log に流すため、
        # 代わりに log_suppressed_exception で type + frame のみを記録する
        # （secret/PII が prompt 由来で例外メッセージに混入するリスクを排除、
        # Issue #66 Copilot Round 1 指摘 / 要件 §14）。
        log_suppressed_exception("LLM Gateway interceptor 内例外を抑制", exc)
