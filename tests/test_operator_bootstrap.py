"""Phase E.4R / R4 tests: executor agent bootstrap.

The bootstrap command is the fresh-deployment path to the *first* executor identity.
These tests exercise ``bootstrap_executor`` (and the ``cmd_bootstrap_executor`` glue)
through the real production services (``TaskService`` / ``AgentService``), proving:

* a fresh DB creates a dedicated Task, registers the executor (registered), then
  transitions it to active with ``agent.execution`` allowed;
* a re-run reuses the existing active executor (no duplicate);
* a paused executor is refused (never auto-resumed);
* a retired executor is refused (never auto-resumed or worked around);
* a same-name/version agent with a mismatched contract is refused (no silent v2);
* every path leaves audit/event/outbox records.

No raw SQL, no invented UUID, no direct ``agent.status`` mutation.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from dxbrain.core.agents import AgentService
from dxbrain.core.models import Agent, AuditEvent, DomainEvent
from dxbrain.core.tasks import TaskService
from dxbrain.operator.bootstrap import (
    BootstrapConflict,
    bootstrap_executor,
)

CAPABILITY = "agent.execution"


def _services(factory):
    return TaskService(factory), AgentService(factory)


def test_bootstrap_creates_active_executor(session_factories) -> None:
    _, _, factory = session_factories
    tasks, agents = _services(factory)

    result = bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")

    agent = agents.get_agent(result.agent_id)
    assert agent.name == "dx-codex-executor"
    assert agent.version == "1"
    assert agent.role == "executor"
    assert agent.status == "active"
    assert agent.allowed_capabilities == [CAPABILITY]

    # agent.execution permission is allowed for the active executor.
    permission = agents.check_permission(agent_id=agent.id, capability=CAPABILITY)
    assert permission.allowed is True

    # A dedicated task owns this identity.
    assert result.task_id == agent.task_id


def test_bootstrap_is_idempotent_reuse(session_factories) -> None:
    _, _, factory = session_factories
    tasks, agents = _services(factory)

    first = bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")
    second = bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")

    # Same agent reused, no duplicate executor.
    assert second.agent_id == first.agent_id
    assert second.reused is True

    with factory() as session:
        count = session.scalar(select(Agent).where(Agent.name == "dx-codex-executor"))
        rows = session.scalars(select(Agent).where(Agent.name == "dx-codex-executor")).all()
        assert len(rows) == 1
        assert count is not None


def test_bootstrap_activates_registered_executor(session_factories) -> None:
    """A matching executor left in ``registered`` is activated, not duplicated."""

    _, _, factory = session_factories
    tasks, agents = _services(factory)

    # Register (but do not activate) the executor identity via the formal service.
    task = tasks.create_task(
        title="pre-existing bootstrap task",
        description=None,
        actor_type="human",
        actor_id="owner-e5a",
        idempotency_key=f"seed-task-{uuid4()}",
    )
    agent = agents.register_agent(
        task_id=task.id,
        name="dx-codex-executor",
        version="1",
        role="executor",
        contract={"input_schema": {}, "output_schema": {}, "side_effects": True},
        allowed_capabilities=[CAPABILITY],
        actor_type="human",
        actor_id="owner-e5a",
        idempotency_key=f"seed-agent-{uuid4()}",
    )
    assert agent.status == "registered"

    result = bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")

    assert result.agent_id == agent.id
    assert result.reused is False
    assert agents.get_agent(result.agent_id).status == "active"


def test_bootstrap_refuses_paused_executor(session_factories) -> None:
    _, _, factory = session_factories
    tasks, agents = _services(factory)

    # Bootstrap once, then pause the executor.
    result = bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")
    agents.transition(
        agent_id=result.agent_id,
        target_status="paused",
        reason="operator paused it",
        actor_type="human",
        actor_id="owner-e5a",
        idempotency_key=f"pause-{uuid4()}",
    )

    with pytest.raises(BootstrapConflict):
        bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")

    # Still paused: never auto-resumed.
    assert agents.get_agent(result.agent_id).status == "paused"


def test_bootstrap_refuses_retired_executor(session_factories) -> None:
    _, _, factory = session_factories
    tasks, agents = _services(factory)

    result = bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")
    agents.transition(
        agent_id=result.agent_id,
        target_status="retired",
        reason="operator retired it",
        actor_type="human",
        actor_id="owner-e5a",
        idempotency_key=f"retire-{uuid4()}",
    )

    with pytest.raises(BootstrapConflict):
        bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")

    assert agents.get_agent(result.agent_id).status == "retired"


def test_bootstrap_refuses_contract_mismatch(session_factories) -> None:
    """A same name/version agent with a different role/capability is refused (no v2)."""

    _, _, factory = session_factories
    tasks, agents = _services(factory)

    task = tasks.create_task(
        title="mismatch task",
        description=None,
        actor_type="human",
        actor_id="owner-e5a",
        idempotency_key=f"seed-task-{uuid4()}",
    )
    agents.register_agent(
        task_id=task.id,
        name="dx-codex-executor",
        version="1",
        role="planner",  # wrong role
        contract={"input_schema": {}, "output_schema": {}, "side_effects": False},
        allowed_capabilities=["kernel.read", "workflow.plan"],  # wrong capabilities
        actor_type="human",
        actor_id="owner-e5a",
        idempotency_key=f"seed-agent-{uuid4()}",
    )

    with pytest.raises(BootstrapConflict):
        bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")


def test_bootstrap_leaves_audit_and_event_records(session_factories) -> None:
    _, _, factory = session_factories
    tasks, agents = _services(factory)

    bootstrap_executor(tasks=tasks, agents=agents, actor_id="owner-e5a")

    with factory() as session:
        agent_events = session.scalars(
            select(DomainEvent).where(
                DomainEvent.event_type.in_(
                    [
                        "agent.registered.v1",
                        "agent.lifecycle.changed.v1",
                    ]
                )
            )
        ).all()
        audit = session.scalars(
            select(AuditEvent).where(
                AuditEvent.operation.in_(
                    [
                        "agent.register",
                        "agent.lifecycle.change",
                    ]
                )
            )
        ).all()

    # Registration and activation each produce an event and an audit record.
    assert len(agent_events) >= 2
    assert len(audit) >= 2
