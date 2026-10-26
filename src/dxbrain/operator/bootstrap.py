"""Executor agent bootstrap for a fresh DX deployment (Phase E.4R / R4).

A fresh deployment has a chicken-and-egg gap: ``operator create`` requires an
``agent_id``, but ``AgentService.register_agent`` requires an existing ``task_id``,
and the only task is created by ``operator create`` itself. There is therefore no
clean Operator-only path to obtain the *first* executor identity.

This module is that path. It performs the smallest explicit, auditable bootstrap:

    human command
      -> TaskService.create_task        (dedicated bootstrap task)
      -> AgentService.register_agent    (role=executor, status=registered)
      -> AgentService.transition        (registered -> active, by a human actor)

There is no raw SQL, no invented UUID, no direct ``agent.status`` mutation, and no
bypass of audit/event recording. The fixed executor identity contract is:

    name                 dx-codex-executor
    version              1
    role                 executor
    allowed_capabilities ["agent.execution"]
    contract             {input_schema, output_schema, side_effects=True}

The capability set is *not* CLI-configurable: it is fixed here so a caller cannot
bootstrap an executor with an arbitrary (or overly broad) capability surface.

Idempotency / reuse semantics (fail closed, never duplicate):

* no matching agent            -> create task + register + transition to active
* matching agent, status=active, role and capabilities exactly as expected -> REUSE
* matching agent, status=registered                                  -> transition to active
* matching agent, status=paused or retired                           -> STOP
* matching name/version but mismatched role/capability/contract      -> STOP

A status=paused agent is a deliberate human pause and is never auto-resumed; a
status=retired agent is terminal. Neither is silently worked around (no v2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from dxbrain.core.agents import AgentService
from dxbrain.core.tasks import TaskService

EXECUTOR_NAME = "dx-codex-executor"
EXECUTOR_VERSION = "1"
EXECUTOR_ROLE = "executor"
EXECUTOR_CAPABILITIES = ["agent.execution"]

EXECUTOR_CONTRACT: dict[str, Any] = {
    "input_schema": {
        "type": "object",
        "required": ["prompt"],
        "properties": {"prompt": {"type": "string", "minLength": 1}},
    },
    "output_schema": {
        "type": "object",
        "required": ["result"],
        "properties": {"result": {"type": "string"}},
    },
    "side_effects": True,
}


class BootstrapConflict(RuntimeError):
    """An existing executor identity blocks a safe bootstrap (paused/retired/mismatch)."""


@dataclass(frozen=True)
class BootstrapResult:
    agent_id: UUID
    task_id: UUID
    reused: bool


def _expected_agent_matches(agent: Any) -> bool:
    return (
        agent.role == EXECUTOR_ROLE
        and list(agent.allowed_capabilities) == EXECUTOR_CAPABILITIES
        and agent.name == EXECUTOR_NAME
        and agent.version == EXECUTOR_VERSION
    )


def bootstrap_executor(
    *,
    tasks: TaskService,
    agents: AgentService,
    actor_id: str,
    name: str = EXECUTOR_NAME,
    version: str = EXECUTOR_VERSION,
) -> BootstrapResult:
    """Bootstrap the first executor agent, or reuse/activate an existing one.

    ``name`` and ``version`` default to the fixed executor identity; a caller may pass
    them explicitly but a mismatch against an already-registered identity is refused.
    ``actor_id`` is the human actor that authorizes registration and activation.
    """

    actor_type = "human"

    # 1. Look for an existing executor identity with this exact name/version.
    for agent in agents.list_agents():
        if agent.name != name or agent.version != version:
            continue
        if not _expected_agent_matches(agent):
            raise BootstrapConflict(
                f"existing agent {name} v{version} has an unexpected role/capability "
                f"contract; refusing to bootstrap (no silent v2)"
            )
        if agent.status == "active":
            return BootstrapResult(agent_id=agent.id, task_id=agent.task_id, reused=True)
        if agent.status == "registered":
            activated = agents.transition(
                agent_id=agent.id,
                target_status="active",
                reason="executor bootstrap activation by operator",
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=f"bootstrap-executor:{actor_id}:activate:{agent.id}",
            )
            return BootstrapResult(agent_id=activated.id, task_id=activated.task_id, reused=False)
        if agent.status in {"paused", "retired"}:
            raise BootstrapConflict(
                f"existing executor agent {name} v{version} is {agent.status}; "
                "refusing to auto-resume or retire it"
            )
        raise BootstrapConflict(
            f"existing executor agent {name} v{version} is in unknown status {agent.status!r}"
        )

    # 2. No matching executor: create a dedicated bootstrap task, then register the
    #    agent through the formal service (no raw SQL, no invented UUID).
    task = tasks.create_task(
        title="Executor agent bootstrap",
        description="Dedicated task that owns the first dx-codex-executor identity.",
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=f"bootstrap-executor:{actor_id}:task",
    )
    agent = agents.register_agent(
        task_id=task.id,
        name=name,
        version=version,
        role=EXECUTOR_ROLE,
        contract=dict(EXECUTOR_CONTRACT),
        allowed_capabilities=list(EXECUTOR_CAPABILITIES),
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=f"bootstrap-executor:{actor_id}:register",
    )
    # 3. A human actor explicitly activates the newly-registered executor.
    activated = agents.transition(
        agent_id=agent.id,
        target_status="active",
        reason="executor bootstrap activation by operator",
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=f"bootstrap-executor:{actor_id}:activate:{agent.id}",
    )
    return BootstrapResult(agent_id=activated.id, task_id=activated.task_id, reused=False)
