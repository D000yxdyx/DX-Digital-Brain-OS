"""Provider-neutral model request contracts and offline-testable adapters.

The adapters deliberately separate request shaping/response parsing from transport.
Calling ``complete`` without an injected transport fails closed, so merely creating
an adapter can never make an outbound request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderAdapterError(ValueError):
    """The provider response cannot satisfy the normalized model contract."""


class OutboundCallsDisabled(RuntimeError):
    """No transport was supplied; outbound model calls are disabled by default."""


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    system: str | None = None
    max_tokens: int = 1024
    temperature: float | None = None


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ModelResult:
    provider: str
    model: str
    request_id: str | None
    text: str
    usage: TokenUsage


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    payload: Mapping[str, Any]


class HttpxTransport:
    """Single-attempt HTTP transport; retries are intentionally not implicit."""

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        import httpx

        response = httpx.request(
            method,
            url,
            headers=dict(headers),
            json=dict(payload),
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        try:
            decoded = response.json()
        except ValueError:
            decoded = {}
        return TransportResponse(
            status_code=response.status_code,
            payload=decoded if isinstance(decoded, Mapping) else {},
        )


class Transport(Protocol):
    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse: ...


def _message_payload(messages: tuple[ChatMessage, ...]) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in messages]


def _required_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ProviderAdapterError(f"provider response field '{key}' must be an object")
    return value


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderAdapterError(f"provider response field '{key}' must be a non-empty string")
    return value


def _nonnegative_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key, 0)
    if not isinstance(value, int) or value < 0:
        raise ProviderAdapterError(f"provider usage field '{key}' must be a non-negative integer")
    return value


class _BaseAdapter:
    def __init__(
        self, *, provider: str, base_url: str, api_key: str, timeout_seconds: float = 30.0
    ) -> None:
        self.provider = provider
        self.base_url = base_url
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider={self.provider!r}, "
            f"base_url={self.base_url!r}, timeout_seconds={self.timeout_seconds!r})"
        )

    def _complete(self, request: ModelRequest, transport: Transport | None) -> ModelResult:
        if transport is None:
            raise OutboundCallsDisabled(
                "outbound model calls are disabled; inject an explicit transport to enable one"
            )
        prepared = self.prepare(request)
        response = transport(
            method=prepared.method,
            url=prepared.url,
            headers=prepared.headers,
            payload=prepared.payload,
            timeout_seconds=self.timeout_seconds,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise ProviderAdapterError(
                f"{self.provider} returned HTTP {response.status_code}"
            )
        return self.parse_response(response.payload)


class DeepSeekAdapter(_BaseAdapter):
    """OpenAI-compatible adapter for DeepSeek chat completions."""

    def __init__(
        self, *, base_url: str, api_key: str, timeout_seconds: float = 30.0
    ) -> None:
        super().__init__(
            provider="deepseek",
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    def prepare(self, request: ModelRequest) -> PreparedRequest:
        messages = (
            []
            if request.system is None
            else [{"role": "system", "content": request.system}]
        )
        messages.extend(_message_payload(request.messages))
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return PreparedRequest(
            method="POST",
            url=f"{self.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )

    def parse_response(self, payload: Mapping[str, Any]) -> ModelResult:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderAdapterError("DeepSeek response field 'choices' must be a non-empty list")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ProviderAdapterError("DeepSeek response choice must be an object")
        message = _required_mapping(first, "message")
        text = _required_string(message, "content")
        usage = _required_mapping(payload, "usage")
        input_tokens = _nonnegative_int(usage, "prompt_tokens")
        output_tokens = _nonnegative_int(usage, "completion_tokens")
        total_tokens = _nonnegative_int(usage, "total_tokens")
        if total_tokens == 0 and (input_tokens or output_tokens):
            total_tokens = input_tokens + output_tokens
        return ModelResult(
            provider=self.provider,
            model=_required_string(payload, "model"),
            request_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            text=text,
            usage=TokenUsage(input_tokens, output_tokens, total_tokens),
        )

    def complete(self, request: ModelRequest, transport: Transport | None = None) -> ModelResult:
        return self._complete(request, transport)


class AnthropicAdapter(_BaseAdapter):
    """Native Messages adapter for Anthropic-compatible endpoints."""

    def __init__(
        self, *, base_url: str, api_key: str, timeout_seconds: float = 30.0
    ) -> None:
        super().__init__(
            provider="anthropic",
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    def prepare(self, request: ModelRequest) -> PreparedRequest:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": _message_payload(request.messages),
            "max_tokens": request.max_tokens,
        }
        if request.system is not None:
            payload["system"] = request.system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return PreparedRequest(
            method="POST",
            url=f"{self.base_url.rstrip('/')}/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            payload=payload,
        )

    def parse_response(self, payload: Mapping[str, Any]) -> ModelResult:
        content = payload.get("content")
        if not isinstance(content, list) or not content:
            raise ProviderAdapterError(
                "Anthropic response field 'content' must be a non-empty list"
            )
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "text":
                continue
            value = block.get("text")
            if isinstance(value, str):
                text_parts.append(value)
        if not text_parts:
            raise ProviderAdapterError("Anthropic response has no text content block")
        usage = _required_mapping(payload, "usage")
        input_tokens = _nonnegative_int(usage, "input_tokens")
        output_tokens = _nonnegative_int(usage, "output_tokens")
        return ModelResult(
            provider=self.provider,
            model=_required_string(payload, "model"),
            request_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            text="".join(text_parts),
            usage=TokenUsage(input_tokens, output_tokens, input_tokens + output_tokens),
        )

    def complete(self, request: ModelRequest, transport: Transport | None = None) -> ModelResult:
        return self._complete(request, transport)


__all__ = [
    "AnthropicAdapter",
    "ChatMessage",
    "DeepSeekAdapter",
    "HttpxTransport",
    "ModelRequest",
    "ModelResult",
    "OutboundCallsDisabled",
    "PreparedRequest",
    "ProviderAdapterError",
    "TokenUsage",
    "Transport",
    "TransportResponse",
]
