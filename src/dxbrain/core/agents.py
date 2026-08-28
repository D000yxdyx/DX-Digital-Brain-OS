from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import (
    Agent,
    AgentLifecycleCommand,
    AuditEvent,
    DomainEvent,
    EventOutbox,
    Task,
)


class AgentConflict(ValueError):
    """An Agent registration or lifecycle transition is not legal."""


class AgentPayloadMismatch(ValueError):
    """An Agent idempotency key was reused with another request."""


@dataclass(frozen=True)
class AgentView:
    id: UUID
    task_id: UUID
    name: str
    version: str
    role: str
    contract: dict[str, Any]
    allowed_capabilities: list[str]
    status: str
    created_at: Any
    updated_at: Any
    replayed: bool = False


@dataclass(frozen=True)
class AgentPermissionView:
    agent_id: UUID
    capability: str
    allowed: bool
    requires_approval: bool
    reasons: list[str]


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class AgentService:
    """Registry and lifecycle contracts; this service never invokes Agent tools."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lock(session: Session, *, actor_type: str, actor_id: str, key: str) -> None:
        lock_key = f"{actor_type}:{actor_id}:agent:{key}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _view(row: Agent, *, replayed: bool = False) -> AgentView:
        return AgentView(
            id=row.id,
            task_id=row.task_id,
            name=row.name,
            version=row.version,
            role=row.role,
            contract=dict(row.contract),
            allowed_capabilities=list(row.allowed_capabilities),
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
            replayed=replayed,
        )

    @staticmethod
    def _validate_contract(contract: dict[str, Any]) -> None:
        required = {"input_schema", "output_schema", "side_effects"}
        missing = sorted(required.difference(contract))
        if missing:
            raise ValueError(f"agent contract missing fields: {', '.join(missing)}")
        if not isinstance(contract["side_effects"], bool):
            raise ValueError("agent contract side_effects must be boolean")

    def register_agent(
        self,
        *,
        task_id: UUID,
        name: str,
        version: str,
        role: str,
        contract: dict[str, Any],
        allowed_capabilities: list[str],
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> AgentView:
        self._validate_contract(contract)
        if not allowed_capabilities:
            raise ValueError("agent must declare at least one capability")
        payload = {
            "task_id": str(task_id),
            "name": name,
            "version": version,
            "role": role,
            "contract": contract,
            "allowed_capabilities": allowed_capabilities,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(session, actor_type=actor_type, actor_id=actor_id, key=idempotency_key)
            if session.get(Task, task_id) is None:
                raise LookupError("task not found")
            replay = session.scalar(
                select(Agent).where(
                    Agent.actor_type == actor_type,
                    Agent.actor_id == actor_id,
                    Agent.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise AgentPayloadMismatch(
                        "agent idempotency key was reused with another payload"
                    )
                return self._view(replay, replayed=True)
            existing = session.scalar(
                select(Agent).where(Agent.name == name, Agent.version == version)
            )
            if existing is not None:
                raise AgentConflict("an agent with this name and version is already registered")
            row = Agent(
                task_id=task_id,
                name=name,
                version=version,
                role=role,
                contract=contract,
                allowed_capabilities=allowed_capabilities,
                status="registered",
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(row)
            session.flush()
            detail = {
                "agent_id": str(row.id),
                "name": name,
                "version": version,
                "status": "registered",
                "capabilities": allowed_capabilities,
            }
            event = DomainEvent(
                aggregate_type="agent",
                aggregate_id=task_id,
                event_type="agent.registered.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="agent.register",
                    task_id=event.aggregate_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._view(row)

    def transition(
        self,
        *,
        agent_id: UUID,
        target_status: str,
        reason: str,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> AgentView:
        if actor_type != "human":
            raise AgentConflict("only a human actor may change Agent lifecycle")
        if target_status not in {"active", "paused", "retired"}:
            raise ValueError("invalid Agent target status")
        payload = {"agent_id": str(agent_id), "target_status": target_status, "reason": reason}
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                key=f"lifecycle:{agent_id}:{idempotency_key}",
            )
            agent = session.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
            if agent is None:
                raise LookupError("agent not found")
            replay = session.scalar(
                select(AgentLifecycleCommand).where(
                    AgentLifecycleCommand.agent_id == agent_id,
                    AgentLifecycleCommand.actor_type == actor_type,
                    AgentLifecycleCommand.actor_id == actor_id,
                    AgentLifecycleCommand.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise AgentPayloadMismatch(
                        "agent lifecycle key was reused with another payload"
                    )
                return self._view(agent, replayed=True)
            legal = {
                "registered": {"active", "retired"},
                "active": {"paused", "retired"},
                "paused": {"active", "retired"},
                "retired": set(),
            }
            if target_status not in legal[agent.status]:
                raise AgentConflict(
                    f"cannot transition Agent from {agent.status} to {target_status}"
                )
            session.add(
                AgentLifecycleCommand(
                    agent_id=agent.id,
                    previous_status=agent.status,
                    target_status=target_status,
                    reason=reason,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
            )
            previous_status = agent.status
            agent.status = target_status
            session.flush()
            aggregate_id = agent.task_id
            detail = {
                "agent_id": str(agent.id),
                "previous_status": previous_status,
                "target_status": target_status,
                "reason": reason,
            }
            event = DomainEvent(
                aggregate_type="agent",
                aggregate_id=aggregate_id,
                event_type="agent.lifecycle.changed.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="agent.lifecycle.change",
                    task_id=aggregate_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._view(agent)

    def get_agent(self, agent_id: UUID) -> AgentView:
        with self._session_factory() as session:
            row = session.get(Agent, agent_id)
            if row is None:
                raise LookupError("agent not found")
            return self._view(row)

    def list_agents(self) -> list[AgentView]:
        with self._session_factory() as session:
            rows = session.scalars(select(Agent).order_by(Agent.created_at, Agent.id)).all()
            return [self._view(row) for row in rows]

    def check_permission(self, *, agent_id: UUID, capability: str) -> AgentPermissionView:
        with self._session_factory() as session:
            agent = session.get(Agent, agent_id)
            if agent is None:
                raise LookupError("agent not found")
            reasons: list[str] = []
            if agent.status != "active":
                reasons.append(f"agent status is {agent.status}, not active")
            if capability not in agent.allowed_capabilities:
                reasons.append("capability is not declared by the agent contract")
            return AgentPermissionView(
                agent_id=agent_id,
                capability=capability,
                allowed=not reasons,
                requires_approval=bool(agent.contract.get("side_effects", False)),
                reasons=reasons,
            )
