from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dxbrain.providers.adapters import AnthropicAdapter, DeepSeekAdapter

ProviderName = Literal["deepseek", "anthropic"]


@dataclass(frozen=True)
class ProviderDescriptor:
    """Safe-to-display metadata; secrets are intentionally not retained here."""

    name: ProviderName
    configured: bool
    base_url: str
    default_model: str
    transport: Literal["openai_compatible", "native_messages"]


class ModelProviderRegistry:
    """Registry for model providers and policy-level roles.

    The registry describes providers, resolves role policy, and can construct an
    adapter. Adapters still require an explicitly injected transport before any
    outbound call can occur.
    """

    def __init__(
        self,
        *,
        deepseek_api_key: str | None = None,
        deepseek_base_url: str = "https://api.deepseek.com",
        deepseek_model: str = "deepseek-chat",
        anthropic_api_key: str | None = None,
        anthropic_base_url: str = "https://api.anthropic.com",
        anthropic_model: str = "claude-sonnet-4-20250514",
        planner_provider: ProviderName = "deepseek",
        reviewer_provider: ProviderName = "anthropic",
        fallback_provider: ProviderName | None = None,
    ) -> None:
        self._providers: dict[ProviderName, ProviderDescriptor] = {
            "deepseek": ProviderDescriptor(
                name="deepseek",
                configured=bool(deepseek_api_key),
                base_url=deepseek_base_url.rstrip("/"),
                default_model=deepseek_model,
                transport="openai_compatible",
            ),
            "anthropic": ProviderDescriptor(
                name="anthropic",
                configured=bool(anthropic_api_key),
                base_url=anthropic_base_url.rstrip("/"),
                default_model=anthropic_model,
                transport="native_messages",
            ),
        }
        self._api_keys: dict[ProviderName, str | None] = {
            "deepseek": deepseek_api_key,
            "anthropic": anthropic_api_key,
        }
        configured_names = set(self._providers)
        for role, provider_name in {
            "planner": planner_provider,
            "reviewer": reviewer_provider,
            "fallback": fallback_provider,
        }.items():
            if provider_name is not None and provider_name not in configured_names:
                raise ValueError(f"unknown provider '{provider_name}' configured for role '{role}'")
        self._roles: dict[str, ProviderName | None] = {
            "planner": planner_provider,
            "reviewer": reviewer_provider,
            "fallback": fallback_provider,
        }

    def list(self) -> list[ProviderDescriptor]:
        """Return deterministic provider metadata without exposing API keys."""

        return list(self._providers.values())

    def resolve(self, role: str) -> ProviderDescriptor:
        """Resolve a policy role, rejecting unknown or unavailable roles."""

        provider_name = self._roles.get(role)
        if provider_name is None:
            raise LookupError(f"no provider policy configured for role: {role}")
        return self._providers[provider_name]

    def adapter(self, provider_name: ProviderName) -> DeepSeekAdapter | AnthropicAdapter:
        """Build a provider adapter without enabling network access."""

        descriptor = self._providers[provider_name]
        api_key = self._api_keys[provider_name]
        if not api_key:
            raise LookupError(f"provider is not configured: {provider_name}")
        if provider_name == "deepseek":
            return DeepSeekAdapter(
                base_url=descriptor.base_url,
                api_key=api_key,
            )
        return AnthropicAdapter(
            base_url=descriptor.base_url,
            api_key=api_key,
        )
