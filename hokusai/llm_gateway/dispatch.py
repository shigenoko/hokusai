"""LLM Gateway 共通 dispatch helper（Issue #66）

ClaudeCodeClient / CodexClient / GeminiClient で同形の interceptor 呼び出し
boilerplate が重複していたため共通化する。

- `get_config()` から llm_gateway 設定を取得
- gateway が未設定（古い config）なら no-op
- `LLMGatewayContext` を組み立てて `LLMGatewayInterceptor.intercept()` を呼ぶ
- **例外は完全に握り潰し**、debug ログにのみ stack trace を残す。secret /
  PII を log にこぼさないようメッセージ本文は出さない（要件 §14 受け入れ基準）

後続の Phase 2 拡張（block / warn / spend tracking / PII detector）はこの
helper に集約することで、3 client に同じ修正を 3 回入れる必要をなくす。
"""

from __future__ import annotations

import traceback
from typing import Mapping

from ..logging_config import get_logger

logger = get_logger("llm_gateway")


def log_suppressed_exception(message: str, exc: BaseException) -> None:
    """LLM Gateway 例外を **メッセージ本文を含めずに** debug ログに残す。

    `logger.debug(..., exc_info=True)` だと `exc.args` の文字列が traceback
    と共に log stream に流れてしまい、prompt や secret が呼び出し側から
    例外メッセージに混入したケースで PII が log にこぼれるリスクがある
    （PR #67 Copilot Round 1 指摘 / 要件 §14 受け入れ基準）。

    本関数は例外型名 + frame 一覧（ファイル / 行番号 / 関数名）のみを
    debug ログに出し、`str(exc)` 由来の文字列は一切 log に含めない。

    Args:
        message: ログの先頭メッセージ（呼び出し文脈の説明、user-controlled な
            値を含めない）
        exc: 抑制した例外
    """
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


def dispatch_via_gateway(
    *,
    provider: str,
    model: str,
    purpose: str,
    prompt: str,
    metadata: Mapping[str, object] | None = None,
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

    Returns:
        None。decision は Phase 1 では使わず、副作用（audit log 出力）のみ。

    Notes:
        既存フローへの影響をゼロにするため例外を完全に握り潰す。Phase 5+ で
        block decision を返す時には呼び出し側の例外処理を見直す必要がある。
    """
    try:
        from ..config import get_config
        from .context import LLMGatewayContext
        from .interceptor import LLMGatewayInterceptor

        config = get_config()
        gateway_config = getattr(config, "llm_gateway", None)
        if gateway_config is None:
            return
        context = LLMGatewayContext(
            provider=provider,
            model=model,
            purpose=purpose,
            metadata=dict(metadata or {}),
        )
        LLMGatewayInterceptor(gateway_config).intercept(context, prompt)
    except Exception as exc:
        # exc_info=True は exc.args 経由で例外メッセージ本文を log に流すため、
        # 代わりに log_suppressed_exception で type + frame のみを記録する
        # （secret/PII が prompt 由来で例外メッセージに混入するリスクを排除、
        # PR #67 Copilot Round 1 指摘 / 要件 §14）。
        log_suppressed_exception("LLM Gateway interceptor 内例外を抑制", exc)
