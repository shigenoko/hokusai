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
from .interceptor import InterceptorDecision, LLMGatewayInterceptor

__all__ = [
    "InterceptorDecision",
    "LLMGatewayContext",
    "LLMGatewayInterceptor",
]
