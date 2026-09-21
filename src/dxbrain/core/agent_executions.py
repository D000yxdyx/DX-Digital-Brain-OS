"""AgentExecution persistence service (Agent Execution Bridge V0.2, Phase E.1).

This module is the *persistence* layer only. It is deliberately free of any provider,
adapter, or Execution Gateway logic (those are out of scope for Phase E.1). It implements:

* idempotent creation of an ``agent_executions`` row (payload hash + pg advisory xact lock
  + replay / payload-mismatch, mirroring ``governance.py`` / ``model_calls.py``);
* legal five-state lifecycle transitions with illegal-transition rejection;
* atomic ``DomainEvent`` + ``EventOutbox`` + ``AuditEvent`` writes inside one transaction.

Lifecycle is the frozen five-state machine
(``requested -> dispatched -> running -> succeeded/failed``); ``reported`` was removed in
E.0.1 because the AgentExecution -> Governance ExecutionReport linkage is deferred.

Privilege boundary (preserved from ADR-0010 / design §16): all writes here run as
``dx_api``. ``dx_worker`` is read-only on ``dx_workflow.agent_executions`` and must never
call these mutating methods directly -- the OutboxWorker only *reads* the
``agent_execution.requested.v1`` event and *invokes* the Execution Gateway.

DomainEvent anchoring (design §13): ``DomainEvent.aggregate_id`` is hard-FK'd to
``dx_core.tasks.id`` (an infrastructure-compatibility constraint, NOT a claim that
AgentExecution lacks identity), so events are task-anchored and carry ``agent_execution_id``
in the payload.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import AgentExecution, AuditEvent, DomainEvent, EventOutbox


class AgentExecutionPayloadMismatch(ValueError):
    """An idempotency key was reused with another AgentExecution create payload."""


class AgentExecutionConflict(ValueError):
    """A lifecycle transition is not legal in the execution's current state."""


# Frozen legal transitions (E.0.1 adjudication, design §11).
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "requested": {"dispatched", "failed"},
    "dispatched": {"running", "failed"},
    "running": {"succeeded", "failed"},
}

# Terminal states have no outgoing edge in Phase 1.
TERMINAL_STATES = {"succeeded", "failed"}

# execution status -> DomainEvent verb (design §14).
_STATUS_EVENT_VERB = {
    "dispatched": "dispatched",
    "running": "started",
    "succeeded": "succeeded",
    "failed": "failed",
}


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AgentExecutionView:
    id: UUID
    task_id: UUID
    proposal_id: UUID
    approval_id: UUID
    workflow_run_id: UUID
    workflow_step_id: UUID
    draft_id: UUID
    draft_revision: int
    agent_role: str
    agent_id: UUID | None
    provider: str
    capability: str
    workspace_ref: str | None
    status: str
    external_execution_id: str | None
    error_code: str | None
    created_at: Any
    dispatched_at: Any
    started_at: Any
    finished_at: Any
    replayed: bool = False


class AgentExecutionService:
    """Persist and transition AgentExecution rows; never touch a provider."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lock(session: Session, *, actor_type: str, actor_id: str, key: str) -> None:
        lock_key = f"{actor_type}:{actor_id}:agent-execution:{key}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _record_event(
        session: Session,
        *,
        task_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        actor_type: str,
        actor_id: str,
        operation: str,
    ) -> DomainEvent:
        event = DomainEvent(
            aggregate_type="task",
            aggregate_id=task_id,
            event_type=event_type,
            payload=payload,
        )
        session.add(event)
        session.flush()
        session.add(EventOutbox(event_id=event.id, status="pending"))
        session.add(
            AuditEvent(
                actor_type=actor_type,
                actor_id=actor_id,
                operation=operation,
                task_id=task_id,
                event_id=event.id,
                detail=payload,
            )
        )
        return event

    @staticmethod
    def _view(row: AgentExecution, *, replayed: bool = False) -> AgentExecutionView:
        return AgentExecutionView(
            id=row.id,
            task_id=row.task_id,
            proposal_id=row.proposal_id,
            approval_id=row.approval_id,
            workflow_run_id=row.workflow_run_id,
            workflow_step_id=row.workflow_step_id,
            draft_id=row.draft_id,
            draft_revision=row.draft_revision,
            agent_role=row.agent_role,
            agent_id=row.agent_id,
            provider=row.provider,
            capability=row.capability,
            workspace_ref=row.workspace_ref,
            status=row.status,
            external_execution_id=row.external_execution_id,
            error_code=row.error_code,
            created_at=row.created_at,
            dispatched_at=row.dispatched_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            replayed=replayed,
        )

    def get(self, execution_id: UUID) -> AgentExecutionView | None:
        with self._session_factory() as session:
            row = session.get(AgentExecution, execution_id)
            return self._view(row) if row is not None else None

    def create(
        self,
        *,
        task_id: UUID,
        proposal_id: UUID,
        approval_id: UUID,
        workflow_run_id: UUID,
        workflow_step_id: UUID,
        draft_id: UUID,
        draft_revision: int,
        agent_role: str,
        provider: str,
        capability: str,
        request_input: dict[str, Any],
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
        workspace_ref: str | None = None,
        constraints: dict[str, Any] | None = None,
        agent_id: UUID | None = None,
    ) -> AgentExecutionView:
        payload = {
            "task_id": str(task_id),
            "proposal_id": str(proposal_id),
            "approval_id": str(approval_id),
            "workflow_run_id": str(workflow_run_id),
            "workflow_step_id": str(workflow_step_id),
            "draft_id": str(draft_id),
            "draft_revision": draft_revision,
            "agent_role": agent_role,
            "provider": provider,
            "capability": capability,
            "workspace_ref": workspace_ref,
            "request_input": request_input,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session, actor_type=actor_type, actor_id=actor_id, key=idempotency_key
            )
            replay = session.scalar(
                select(AgentExecution).where(
                    AgentExecution.actor_type == actor_type,
                    AgentExecution.actor_id == actor_id,
                    AgentExecution.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise AgentExecutionPayloadMismatch(
                        "agent execution idempotency key was reused with another payload"
                    )
                return self._view(replay, replayed=True)

            row = AgentExecution(
                task_id=task_id,
                proposal_id=proposal_id,
                approval_id=approval_id,
                workflow_run_id=workflow_run_id,
                workflow_step_id=workflow_step_id,
                draft_id=draft_id,
                draft_revision=draft_revision,
                agent_role=agent_role,
                agent_id=agent_id,
                provider=provider,
                capability=capability,
                workspace_ref=workspace_ref,
                status="requested",
                request_input=request_input,
                constraints=constraints,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            session.add(row)
            session.flush()
            self._record_event(
                session,
                task_id=task_id,
                event_type="agent_execution.requested.v1",
                payload={
                    "agent_execution_id": str(row.id),
                    "workflow_step_id": str(workflow_step_id),
                    "status": "requested",
                },
                actor_type=actor_type,
                actor_id=actor_id,
                operation="agent_execution.request.requested",
            )
            return self._view(row)

    def transition(
        self,
        execution_id: UUID,
        to_status: str,
        *,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
        error_code: str | None = None,
        error_detail: str | None = None,
        summary: str | None = None,
        external_execution_id: str | None = None,
        artifacts: dict[str, Any] | None = None,
        log_references: dict[str, Any] | None = None,
        result_tail: str | None = None,
        exit_code: int | None = None,
    ) -> AgentExecutionView:
        with self._session_factory.begin() as session:
            # Lock keyed by execution identity so concurrent transitions on the same
            # execution serialize; idempotency_key is captured for the audit trail only.
            self._lock(
                session, actor_type=actor_type, actor_id=actor_id, key=str(execution_id)
            )
            row = session.scalar(
                select(AgentExecution)
                .where(AgentExecution.id == execution_id)
                .with_for_update()
            )
            if row is None:
                raise LookupError("agent execution not found")

            from_status = row.status
            if (
                from_status not in LEGAL_TRANSITIONS
                or to_status not in LEGAL_TRANSITIONS[from_status]
            ):
                raise AgentExecutionConflict(
                    f"cannot transition agent execution from {from_status} to {to_status}"
                )

            now = datetime.now(UTC)
            row.status = to_status
            if to_status == "dispatched":
                row.dispatched_at = now
                if external_execution_id is not None:
                    row.external_execution_id = external_execution_id
            elif to_status == "running":
                row.started_at = now
            elif to_status in ("succeeded", "failed"):
                row.finished_at = now
                if summary is not None:
                    row.summary = summary
                if artifacts is not None:
                    row.artifacts = artifacts
                if log_references is not None:
                    row.log_references = log_references
                if result_tail is not None:
                    row.result_tail = result_tail
                if exit_code is not None:
                    row.exit_code = exit_code
                if to_status == "failed":
                    row.error_code = error_code
                    row.error_detail = error_detail

            verb = _STATUS_EVENT_VERB[to_status]
            self._record_event(
                session,
                task_id=row.task_id,
                event_type=f"agent_execution.{verb}.v1",
                payload={
                    "agent_execution_id": str(row.id),
                    "from_status": from_status,
                    "to_status": to_status,
                    "status": to_status,
                },
                actor_type=actor_type,
                actor_id=actor_id,
                operation=f"agent_execution.lifecycle.{to_status}",
            )
            return self._view(row)
