from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import (
    AuditEvent,
    DomainEvent,
    EventOutbox,
    PlanDraft,
    WorkflowCommand,
    WorkflowHandoff,
    WorkflowRun,
    WorkflowStep,
)


class WorkflowConflict(ValueError):
    """A workflow command is not legal for the current state."""


class WorkflowPayloadMismatch(ValueError):
    """A workflow idempotency key was reused with another request."""


@dataclass(frozen=True)
class WorkflowStepView:
    id: UUID
    ordinal: int
    title: str
    capability: str
    parameters: dict[str, Any]
    status: str
    result_summary: str | None


@dataclass(frozen=True)
class WorkflowRunView:
    id: UUID
    task_id: UUID
    proposal_id: UUID
    handoff_id: UUID
    draft_id: UUID
    draft_revision: int
    status: str
    steps: list[WorkflowStepView]
    created_at: Any
    updated_at: Any
    completed_at: Any
    replayed: bool = False


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class WorkflowService:
    """Sequential, side-effect-free Workflow MVP for confirmed plan handoffs."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lock(session: Session, *, actor_type: str, actor_id: str, key: str) -> None:
        lock_key = f"{actor_type}:{actor_id}:workflow:{key}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _view(session: Session, run: WorkflowRun, *, replayed: bool = False) -> WorkflowRunView:
        steps = session.scalars(
            select(WorkflowStep).where(WorkflowStep.run_id == run.id).order_by(WorkflowStep.ordinal)
        ).all()
        return WorkflowRunView(
            id=run.id,
            task_id=run.task_id,
            proposal_id=run.proposal_id,
            handoff_id=run.handoff_id,
            draft_id=run.draft_id,
            draft_revision=run.draft_revision,
            status=run.status,
            steps=[
                WorkflowStepView(
                    id=step.id,
                    ordinal=step.ordinal,
                    title=step.title,
                    capability=step.capability,
                    parameters=dict(step.parameters),
                    status=step.status,
                    result_summary=step.result_summary,
                )
                for step in steps
            ],
            created_at=run.created_at,
            updated_at=run.updated_at,
            completed_at=run.completed_at,
            replayed=replayed,
        )

    def accept_handoff(
        self,
        *,
        handoff_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkflowRunView:
        if actor_type != "human":
            raise WorkflowConflict("only a human actor may accept a workflow handoff")
        payload = {"handoff_id": str(handoff_id), "operation": "workflow.accept"}
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                key=f"accept:{handoff_id}:{idempotency_key}",
            )
            replay = session.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.actor_type == actor_type,
                    WorkflowRun.actor_id == actor_id,
                    WorkflowRun.idempotency_key == idempotency_key,
                )
            )
            handoff = session.scalar(
                select(WorkflowHandoff).where(WorkflowHandoff.id == handoff_id).with_for_update()
            )
            if handoff is None:
                raise LookupError("workflow handoff not found")
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise WorkflowPayloadMismatch(
                        "workflow accept key was reused with another payload"
                    )
                return self._view(session, replay, replayed=True)
            if handoff.status != "requested":
                raise WorkflowConflict(f"cannot accept handoff in status {handoff.status}")
            draft = session.scalar(
                select(PlanDraft).where(PlanDraft.id == handoff.draft_id).with_for_update()
            )
            if draft is None:
                raise LookupError("plan draft not found")
            if draft.status != "confirmed":
                raise WorkflowConflict("only a confirmed plan draft may enter Workflow")
            if draft.revision != handoff.draft_revision:
                raise WorkflowConflict("handoff revision is no longer the draft revision")
            run = WorkflowRun(
                task_id=handoff.task_id,
                proposal_id=handoff.proposal_id,
                handoff_id=handoff.id,
                draft_id=draft.id,
                draft_revision=draft.revision,
                status="ready",
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(run)
            session.flush()
            for ordinal, raw_step in enumerate(draft.steps, start=1):
                title = raw_step.get("title")
                capability = raw_step.get("capability")
                if not isinstance(title, str) or not isinstance(capability, str):
                    raise WorkflowConflict("confirmed draft contains an invalid step contract")
                session.add(
                    WorkflowStep(
                        run_id=run.id,
                        ordinal=ordinal,
                        title=title[:500],
                        capability=capability[:128],
                        parameters=dict(raw_step.get("parameters") or {}),
                        status="pending",
                    )
                )
            handoff.status = "accepted"
            session.flush()
            detail = {
                "run_id": str(run.id),
                "handoff_id": str(handoff.id),
                "draft_id": str(draft.id),
                "draft_revision": draft.revision,
                "status": "ready",
                "step_count": len(draft.steps),
            }
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=run.task_id,
                event_type="workflow.run.created.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="workflow.run.accept",
                    task_id=run.task_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._view(session, run)

    def advance_run(
        self,
        *,
        run_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkflowRunView:
        if actor_type != "human":
            raise WorkflowConflict("only a human actor may advance a workflow")
        return self._transition_run(
            run_id=run_id,
            operation="workflow.advance",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def pause_run(
        self,
        *,
        run_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkflowRunView:
        if actor_type != "human":
            raise WorkflowConflict("only a human actor may pause a workflow")
        return self._transition_run(
            run_id=run_id,
            operation="workflow.pause",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def cancel_run(
        self,
        *,
        run_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkflowRunView:
        if actor_type != "human":
            raise WorkflowConflict("only a human actor may cancel a workflow")
        return self._transition_run(
            run_id=run_id,
            operation="workflow.cancel",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def _transition_run(
        self,
        *,
        run_id: UUID,
        operation: str,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkflowRunView:
        payload = {"run_id": str(run_id), "operation": operation}
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                key=f"command:{run_id}:{operation}:{idempotency_key}",
            )
            run = session.scalar(
                select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
            )
            if run is None:
                raise LookupError("workflow run not found")
            replay = session.scalar(
                select(WorkflowCommand).where(
                    WorkflowCommand.run_id == run_id,
                    WorkflowCommand.operation == operation,
                    WorkflowCommand.actor_type == actor_type,
                    WorkflowCommand.actor_id == actor_id,
                    WorkflowCommand.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise WorkflowPayloadMismatch(
                        "workflow command key was reused with another payload"
                    )
                return self._view(session, run, replayed=True)
            session.add(
                WorkflowCommand(
                    run_id=run_id,
                    operation=operation,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
            )
            steps = session.scalars(
                select(WorkflowStep)
                .where(WorkflowStep.run_id == run.id)
                .order_by(WorkflowStep.ordinal)
            ).all()
            if not steps:
                raise WorkflowConflict("workflow run has no steps")
            if operation == "workflow.pause":
                if run.status not in ("ready", "running"):
                    raise WorkflowConflict(f"cannot pause workflow in status {run.status}")
                run.status = "paused"
            elif operation == "workflow.cancel":
                if run.status in ("completed", "cancelled", "failed"):
                    raise WorkflowConflict(f"cannot cancel workflow in status {run.status}")
                run.status = "cancelled"
                for step in steps:
                    if step.status in ("pending", "running"):
                        step.status = "cancelled"
            else:
                if run.status == "ready":
                    run.status = "running"
                    first = next((step for step in steps if step.status == "pending"), None)
                    if first is not None:
                        first.status = "running"
                elif run.status == "running":
                    current = next((step for step in steps if step.status == "running"), None)
                    if current is None:
                        raise WorkflowConflict("running workflow has no running step")
                    current.status = "succeeded"
                    current.result_summary = "completed by explicit workflow advance"
                    next_step = next((step for step in steps if step.status == "pending"), None)
                    if next_step is None:
                        run.status = "completed"
                        run.completed_at = datetime.now(UTC)
                    else:
                        next_step.status = "running"
                elif run.status == "paused":
                    raise WorkflowConflict("workflow is paused; resume is not enabled in MVP")
                else:
                    raise WorkflowConflict(f"cannot advance workflow in status {run.status}")
            session.flush()
            detail = {
                "run_id": str(run.id),
                "operation": operation,
                "run_status": run.status,
                "step_statuses": {str(step.ordinal): step.status for step in steps},
            }
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=run.task_id,
                event_type="workflow.run.transitioned.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation=operation,
                    task_id=run.task_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._view(session, run)

    def get_run(self, run_id: UUID) -> WorkflowRunView:
        with self._session_factory() as session:
            run = session.get(WorkflowRun, run_id)
            if run is None:
                raise LookupError("workflow run not found")
            return self._view(session, run)

    def list_runs(self, proposal_id: UUID) -> list[WorkflowRunView]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.proposal_id == proposal_id)
                .order_by(WorkflowRun.created_at, WorkflowRun.id)
            ).all()
            return [self._view(session, row) for row in rows]
