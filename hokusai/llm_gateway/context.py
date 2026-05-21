"""LLM Gateway interceptor に渡す呼び出し context

呼び出し側（ClaudeCodeClient 等）が分かる範囲で埋め、不明なものは省略する。
workflow_id / phase は state を持つ node から呼ばれる場合のみ埋まる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


_EMPTY_METADATA: Mapping[str, object] = MappingProxyType({})


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
        metadata: 追加情報（skill 名 / repository / その他自由形式）。
            **不変化**: `frozen=True` の dataclass で再代入は防止されるが、dict は
            生成後に中身を書き換えられる。audit 再現性を保つため、構築時に必ず
            `MappingProxyType` でラップして read-only view に変換する
            （PR #40 Copilot 1 回目指摘）。呼び出し側が dict を渡しても、その
            元 dict を変更しても context の view には影響しない。
    """

    provider: str
    model: str = ""
    purpose: str = ""
    workflow_id: str | None = None
    phase: int | None = None
    metadata: Mapping[str, object] = field(default_factory=lambda: _EMPTY_METADATA)

    def __post_init__(self) -> None:
        # 入力 dict を read-only にラップ。元 dict の変更が context に影響しない
        # よう、まず dict(...) で浅いコピーを取ってから MappingProxyType に通す。
        # `frozen=True` のため object.__setattr__ を使う必要がある。
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(
                self, "metadata", MappingProxyType(dict(self.metadata or {}))
            )
