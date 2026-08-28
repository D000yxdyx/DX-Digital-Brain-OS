from __future__ import annotations

import pytest

from dxbrain.providers.adapters import (
    AnthropicAdapter,
    ChatMessage,
    DeepSeekAdapter,
    ModelRequest,
    OutboundCallsDisabled,
    ProviderAdapterError,
    TransportResponse,
)
from dxbrain.providers.registry import ModelProviderRegistry


def _request() -> ModelRequest:
    return ModelRequest(
        model="test-model",
        system="Return a concise structured answer.",
        messages=(ChatMessage(role="user", content="hello"),),
        max_tokens=64,
        temperature=0.1,
    )


def test_deepseek_adapter_shapes_request_and_normalizes_response() -> None:
    adapter = DeepSeekAdapter(base_url="https://example.test/", api_key="secret")
    prepared = adapter.prepare(_request())

    assert prepared.method == "POST"
    assert prepared.url == "https://example.test/chat/completions"
    assert prepared.headers["Authorization"] == "Bearer secret"
    assert prepared.payload["messages"] == [
        {"role": "system", "content": "Return a concise structured answer."},
        {"role": "user", "content": "hello"},
    ]

    calls: list[dict[str, object]] = []

    def fake_transport(**kwargs: object) -> TransportResponse:
        calls.append(kwargs)
        return TransportResponse(
            status_code=200,
            payload={
                "id": "ds-1",
                "model": "deepseek-chat",
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            },
        )

    result = adapter.complete(_request(), fake_transport)
    assert result.text == "OK"
    assert result.request_id == "ds-1"
    assert result.usage.total_tokens == 11
    assert calls[0]["timeout_seconds"] == 30.0


def test_anthropic_adapter_shapes_request_and_computes_total_usage() -> None:
    adapter = AnthropicAdapter(base_url="https://example.test", api_key="secret")
    prepared = adapter.prepare(_request())

    assert prepared.url == "https://example.test/v1/messages"
    assert prepared.headers["x-api-key"] == "secret"
    assert prepared.headers["anthropic-version"] == "2023-06-01"
    assert prepared.payload["system"] == "Return a concise structured answer."
    assert prepared.payload["messages"] == [{"role": "user", "content": "hello"}]

    result = adapter.complete(
        _request(),
        lambda **_: TransportResponse(
            status_code=200,
            payload={
                "id": "an-1",
                "model": "claude-test",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "A"},
                    {"type": "text", "text": "B"},
                ],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        ),
    )
    assert result.text == "AB"
    assert result.usage.total_tokens == 16


def test_adapter_fails_closed_without_explicit_transport() -> None:
    adapter = DeepSeekAdapter(base_url="https://example.test", api_key="secret")
    with pytest.raises(OutboundCallsDisabled):
        adapter.complete(_request())


def test_adapter_rejects_http_errors_and_malformed_payloads() -> None:
    adapter = AnthropicAdapter(base_url="https://example.test", api_key="secret")
    with pytest.raises(ProviderAdapterError, match="HTTP 429"):
        adapter.complete(
            _request(), lambda **_: TransportResponse(status_code=429, payload={})
        )
    with pytest.raises(ProviderAdapterError, match="no text content"):
        adapter.parse_response(
            {"model": "claude-test", "content": [{"type": "tool_use"}], "usage": {}}
        )


def test_registry_constructs_adapter_only_for_configured_provider() -> None:
    registry = ModelProviderRegistry(deepseek_api_key="ds-secret")
    adapter = registry.adapter("deepseek")
    assert isinstance(adapter, DeepSeekAdapter)
    assert "ds-secret" not in repr(adapter)
    with pytest.raises(LookupError, match="not configured: anthropic"):
        registry.adapter("anthropic")
