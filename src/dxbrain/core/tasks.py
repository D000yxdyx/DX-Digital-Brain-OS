from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import (
    AuditEvent,
    DomainEvent,
    EntityState,
    EventOutbox,
    IdempotencyRequest,
    StateTransition,
    Task,
)


class IdempotencyPayloadMismatch(ValueError):
    """An idempotency key was reused for a different request."""


class StaleStateVersion(ValueError):
    """A command was made against an older state version."""


@dataclass(frozen=True)
class TaskView:
    id: UUID
    title: str
    description: str | None
    status: str
    version: int
    replayed: bool = False


@dataclass(frozen=True)
class TaskListView:
    """Read-model row for the operational Control UI."""

    id: UUID
    title: str
    status: str
    version: int
    created_at: datetime


@dataclass(frozen=True)
class ControlOverview:
    """Small read-only health projection over the kernel tables."""

    task_count: int
    event_count: int
    audit_count: int
    state_counts: dict[str, int]
    outbox_counts: dict[str, int]


def _payload_hash(payload: dict[str, str | None]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class TaskService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_task(
        self,
        *,
        title: str,
        description: str | None,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
        failure_point: Literal["after_event", "after_state"] | None = None,
    ) -> TaskView:
        """Create the task and all kernel artifacts in one database transaction."""

        payload = {"title": title, "description": description}
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            # Serialize concurrent replays without granting UPDATE on the idempotency record.
            # PostgreSQL releases this transaction-scoped advisory lock on rollback or commit.
            lock_key = f"{actor_type}:{actor_id}:task.create:{idempotency_key}"
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
            replay = session.scalar(
                select(IdempotencyRequest).where(
                    IdempotencyRequest.actor_type == actor_type,
                    IdempotencyRequest.actor_id == actor_id,
                    IdempotencyRequest.operation == "task.create",
                    IdempotencyRequest.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise IdempotencyPayloadMismatch(
                        "idempotency key was reused with another payload"
                    )
                task = session.get(Task, replay.task_id)
                state = session.scalar(
                    select(EntityState).where(
                        EntityState.entity_type == "task", EntityState.entity_id == replay.task_id
                    )
                )
                if task is None or state is None:
                    raise RuntimeError("idempotency record references an incomplete task")
                return TaskView(
                    task.id,
                    task.title,
                    task.description,
                    state.status,
                    state.version,
                    replayed=True,
                )

            task = Task(title=title, description=description)
            session.add(task)
            session.flush()

            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=task.id,
                event_type="task.created.v1",
                payload={"task_id": str(task.id), **payload},
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            if failure_point == "after_event":
                raise RuntimeError("injected rollback after event")

            state = EntityState(
                entity_type="task",
                entity_id=task.id,
                status="created",
                version=1,
                last_event_id=event.id,
            )
            transition = StateTransition(
                entity_type="task",
                entity_id=task.id,
                version=1,
                previous_status=None,
                next_status="created",
                event_id=event.id,
            )
            session.add_all([state, transition])
            session.add(
                IdempotencyRequest(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="task.create",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    task_id=task.id,
                )
            )
            if failure_point == "after_state":
                raise RuntimeError("injected rollback after state")
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="task.create",
                    task_id=task.id,
                    event_id=event.id,
                    detail={"idempotency_key": idempotency_key, "status": "created"},
                )
            )
            return TaskView(task.id, task.title, task.description, state.status, state.version)

    def transition_task_state(
        self,
        *,
        task_id: UUID,
        expected_version: int,
        next_status: str,
        actor_type: str,
        actor_id: str,
    ) -> TaskView:
        """A testable optimistic-concurrency state command, preserving kernel artifacts."""

        with self._session_factory.begin() as session:
            task = session.get(Task, task_id)
            state = session.scalar(
                select(EntityState)
                .where(EntityState.entity_type == "task", EntityState.entity_id == task_id)
                .with_for_update()
            )
            if task is None or state is None:
                raise LookupError("task not found")
            if state.version != expected_version:
                raise StaleStateVersion(f"expected {expected_version}, current {state.version}")

            previous_status = state.status
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=task_id,
                event_type="task.state_changed.v1",
                payload={"task_id": str(task_id), "from": previous_status, "to": next_status},
            )
            session.add(event)
            session.flush()
            next_version = state.version + 1
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                StateTransition(
                    entity_type="task",
                    entity_id=task_id,
                    version=next_version,
                    previous_status=previous_status,
                    next_status=next_status,
                    event_id=event.id,
                )
            )
            state.status = next_status
            state.version = next_version
            state.last_event_id = event.id
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="task.transition",
                    task_id=task_id,
                    event_id=event.id,
                    detail={"from": previous_status, "to": next_status, "version": next_version},
                )
            )
            return TaskView(task.id, task.title, task.description, next_status, next_version)

    def get_task(self, task_id: UUID) -> tuple[TaskView, list[StateTransition]]:
        with self._session_factory() as session:
            task = session.get(Task, task_id)
            state = session.scalar(
                select(EntityState).where(
                    EntityState.entity_type == "task", EntityState.entity_id == task_id
                )
            )
            if task is None or state is None:
                raise LookupError("task not found")
            history = list(
                session.scalars(
                    select(StateTransition)
                    .where(
                        StateTransition.entity_type == "task", StateTransition.entity_id == task_id
                    )
                    .order_by(StateTransition.version)
                )
            )
            return TaskView(
                task.id, task.title, task.description, state.status, state.version
            ), history

    def list_tasks(self, *, limit: int = 50) -> list[TaskListView]:
        """Return newest tasks for the read-only operational console."""

        with self._session_factory() as session:
            rows = session.execute(
                select(Task, EntityState)
                .join(EntityState, EntityState.entity_id == Task.id)
                .where(EntityState.entity_type == "task")
                .order_by(Task.created_at.desc(), Task.id.desc())
                .limit(limit)
            ).all()
            return [
                TaskListView(
                    id=task.id,
                    title=task.title,
                    status=state.status,
                    version=state.version,
                    created_at=task.created_at,
                )
                for task, state in rows
            ]

    def get_control_overview(self) -> ControlOverview:
        """Return counts only; this endpoint has no mutation or model inference."""

        with self._session_factory() as session:
            state_rows = session.execute(
                select(EntityState.status, func.count())
                .where(EntityState.entity_type == "task")
                .group_by(EntityState.status)
            ).all()
            outbox_rows = session.execute(
                select(EventOutbox.status, func.count())
                .group_by(EventOutbox.status)
            ).all()
            return ControlOverview(
                task_count=session.scalar(select(func.count()).select_from(Task)) or 0,
                event_count=session.scalar(select(func.count()).select_from(DomainEvent)) or 0,
                audit_count=session.scalar(select(func.count()).select_from(AuditEvent)) or 0,
                state_counts={status: count for status, count in state_rows},
                outbox_counts={status: count for status, count in outbox_rows},
            )
