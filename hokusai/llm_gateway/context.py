"""LLM Gateway interceptor に渡す呼び出し context

呼び出し側（ClaudeCodeClient 等）が分かる範囲で埋め、不明なものは省略する。
workflow_id / phase は state を持つ node から呼ばれる場合のみ埋まる。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LLMGatewayContext:
    """LLM 送信前 interceptor 呼び出し context（#39 / Phase 1）

    Phase 1 は log-only なので、欠落フィールドがあっても interceptor は決して
    block しない。Phase 5 以降で decision に意味を持たせる際は workflow_id /
    phase / model / provider の最低限を要求するように boundary を調整する。

    Attributes:
        provider: LLM provider 識別子（"claude_code" / "codex" / "gemini" / ...）
        model: 利用 model 名（取得できない場合は ""）
        purpose: 呼び出し目的（"skill_execution" / "execute_prompt" / "review" 等）
        workflow_id: 呼び出し元 HOKUSAI workflow_id（state を持つ node から呼ばれた場合）
        phase: 呼び出し元 phase 番号（state を持つ node から呼ばれた場合）
        metadata: 追加情報（skill 名 / repository / その他自由形式）
    """

    provider: str
    model: str = ""
    purpose: str = ""
    workflow_id: str | None = None
    phase: int | None = None
    metadata: dict = field(default_factory=dict)
