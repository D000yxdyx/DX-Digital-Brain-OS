"""Model-provider contracts and deterministic routing metadata."""

from dxbrain.providers.adapters import (
    AnthropicAdapter,
    ChatMessage,
    DeepSeekAdapter,
    HttpxTransport,
    ModelRequest,
    ModelResult,
    OutboundCallsDisabled,
    PreparedRequest,
    ProviderAdapterError,
    TokenUsage,
    TransportResponse,
)
from dxbrain.providers.registry import ModelProviderRegistry, ProviderDescriptor

__all__ = [
    "AnthropicAdapter",
    "ChatMessage",
    "DeepSeekAdapter",
    "HttpxTransport",
    "ModelProviderRegistry",
    "ModelRequest",
    "ModelResult",
    "OutboundCallsDisabled",
    "PreparedRequest",
    "ProviderAdapterError",
    "ProviderDescriptor",
    "TokenUsage",
    "TransportResponse",
]
