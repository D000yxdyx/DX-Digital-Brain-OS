from __future__ import annotations

from fastapi.testclient import TestClient

from dxbrain.api.app import create_app
from dxbrain.config import Settings
from dxbrain.providers.registry import ModelProviderRegistry


def test_registry_hides_keys_and_resolves_policy_roles() -> None:
    registry = ModelProviderRegistry(
        deepseek_api_key="ds-secret",
        anthropic_api_key=None,
        planner_provider="deepseek",
        reviewer_provider="anthropic",
    )

    providers = {item.name: item for item in registry.list()}
    assert providers["deepseek"].configured is True
    assert providers["anthropic"].configured is False
    assert "secret" not in repr(providers)
    assert registry.resolve("planner").name == "deepseek"
    assert registry.resolve("reviewer").name == "anthropic"


def test_registry_rejects_unconfigured_role_name() -> None:
    registry = ModelProviderRegistry()
    try:
        registry.resolve("executor")
    except LookupError as error:
        assert "executor" in str(error)
    else:
        raise AssertionError("unknown role should fail closed")


def test_provider_status_endpoint_does_not_expose_keys(database_urls: tuple[str, str, str]) -> None:
    settings = Settings(
        database_url=database_urls[0],
        root_api_key="test-root-key",
        deepseek_api_key="ds-secret",
        anthropic_api_key=None,
    )
    client = TestClient(create_app(settings))

    response = client.get(
        "/v1/control/model-providers", headers={"X-DX-API-Key": "test-root-key"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["name"] == "deepseek"
    assert payload[0]["configured"] is True
    assert payload[1]["configured"] is False
    assert "secret" not in response.text
