from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from dxbrain.api.app import create_app
from dxbrain.config import Settings
from dxbrain.core.models import Agent, AgentLifecycleCommand, AuditEvent, DomainEvent


def test_agent_registry_lifecycle_and_permission_contract(
    session_factories, database_urls: tuple[str, str, str]
) -> None:
    _, _, migration_factory = session_factories
    client = TestClient(
        create_app(Settings(database_url=database_urls[0], root_api_key="test-root-key"))
    )
    root = {"X-DX-API-Key": "test-root-key"}
    task = client.post(
        "/v1/tasks",
        headers={**root, "Idempotency-Key": "agent-task"},
        json={"title": "agent registry target"},
    )
    assert task.status_code == 201
    task_id = task.json()["id"]
    request = {
        "name": "researcher",
        "version": "0.1.0",
        "role": "planner",
        "contract": {
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "side_effects": True,
        },
        "allowed_capabilities": ["kernel.read", "model.call"],
    }
    headers = {**root, "Idempotency-Key": "agent-register"}
    registered = client.post(f"/v1/tasks/{task_id}/agents", headers=headers, json=request)
    assert registered.status_code == 201
    assert registered.json()["status"] == "registered"
    replay = client.post(f"/v1/tasks/{task_id}/agents", headers=headers, json=request)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    agent_id = registered.json()["id"]
    denied = client.get(
        f"/v1/agents/{agent_id}/permission-check",
        params={"capability": "kernel.read"},
        headers=root,
    )
    assert denied.status_code == 200
    assert denied.json()["allowed"] is False
    assert denied.json()["requires_approval"] is True
    activated = client.post(
        f"/v1/agents/{agent_id}/lifecycle",
        headers={**root, "Idempotency-Key": "agent-activate"},
        json={"target_status": "active", "reason": "contract reviewed"},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    allowed = client.get(
        f"/v1/agents/{agent_id}/permission-check",
        params={"capability": "kernel.read"},
        headers=root,
    )
    assert allowed.json()["allowed"] is True
    unknown = client.get(
        f"/v1/agents/{agent_id}/permission-check",
        params={"capability": "tool.write"},
        headers=root,
    )
    assert unknown.json()["allowed"] is False
    paused = client.post(
        f"/v1/agents/{agent_id}/lifecycle",
        headers={**root, "Idempotency-Key": "agent-pause"},
        json={"target_status": "paused", "reason": "manual hold"},
    )
    assert paused.status_code == 200
    retired = client.post(
        f"/v1/agents/{agent_id}/lifecycle",
        headers={**root, "Idempotency-Key": "agent-retire"},
        json={"target_status": "retired", "reason": "test closeout"},
    )
    assert retired.status_code == 200
    invalid = client.post(
        f"/v1/agents/{agent_id}/lifecycle",
        headers={**root, "Idempotency-Key": "agent-reactivate"},
        json={"target_status": "active", "reason": "should fail"},
    )
    assert invalid.status_code == 409
    with migration_factory() as session:
        assert session.scalar(select(func.count()).select_from(Agent)) == 1
        assert session.scalar(select(func.count()).select_from(AgentLifecycleCommand)) == 3
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 5
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 5
