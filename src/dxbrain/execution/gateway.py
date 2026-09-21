"""Execution Gateway: the DX-owned boundary that turns a provider result into a lifecycle fact.

The Gateway is the only component that may translate provider output into
AgentExecution lifecycle writes, and it performs every write through the frozen
Phase E.1 service (``dxbrain.core.agent_executions.AgentExecutionService``). It never
issues a raw UPDATE and never bypasses the atomic ``DomainEvent`` + ``EventOutbox`` +
``AuditEvent`` triple that E.1 writes inside one transaction.

Privilege boundary (ADR-0010 / persistence design §16): every lifecycle write runs as
``dx_api``. The Gateway therefore owns the ``dx_api`` session factory. ``dx_worker`` is
read-only on ``agent_executions`` and reaches the Gateway by invocation only -- wiring
that call path is Phase E.2B and is deliberately absent here.

Session discipline: no database session or transaction is held open across
``dispatch()`` or ``collect()``. Each read opens its own short session and closes it
before the provider is called; each lifecycle write is one independent E.1
transaction.

Failure mapping: pre-handoff receipts give ``requested -> failed``; a handoff that was
attempted always writes ``dispatched`` first and then ``failed`` if the provider did not
accept it; an operational collection failure (``ProviderCollectionError``) gives
``dispatched -> failed`` with ``error_code = provider_unavailable``. Every other
exception propagates with zero lifecycle writes -- no generic ``except Exception``.

Event routing is a single explicit whitelist entry (``agent_execution.requested.v1``).
E.1 also emits ``dispatched`` / ``started`` / ``succeeded`` / ``failed`` events; routing
anything else to the Gateway would let its own writes re-trigger dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.agent_executions import AgentExecutionService
from dxbrain.core.models import AgentExecution, DomainEvent
from dxbrain.execution.contracts import (
    AGENT_EXECUTION_REQUESTED_EVENT,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    DispatchRequest,
    ExecutionContractError,
    ExecutionOutcome,
    ExecutionProvider,
    ProviderCollectionError,
)

DEFAULT_ACTOR_TYPE = "system"
DEFAULT_ACTOR_ID = "execution-gateway"


@dataclass(frozen=True)
class ExecutionSnapshot:
    """A detached copy of the execution facts the Gateway needs.

    Copied instead of holding the ORM row, so no session stays open (and no ORM object
    can leak) while the provider is being called.
    """

    id: UUID
    task_id: UUID
    provider: str
    capability: str
    agent_role: str
    draft_id: UUID
    draft_revision: int
    workspace_ref: str | None
    request_input: dict[str, Any]
    constraints: dict[str, Any] | None
    status: str


class ExecutionGateway:
    """Translate one AgentExecution request into provider calls and lifecycle writes."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: ExecutionProvider,
        *,
        actor_type: str = DEFAULT_ACTOR_TYPE,
        actor_id: str = DEFAULT_ACTOR_ID,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._service = AgentExecutionService(session_factory)
        self._actor_type = actor_type
        self._actor_id = actor_id

    def handle_event(self, event_id: UUID) -> str:
        """Handle one outbox event. Only ``agent_execution.requested.v1`` is routed."""

        event_type, payload = self._read_event(event_id)
        if event_type != AGENT_EXECUTION_REQUESTED_EVENT:
            return f"ignored:{event_type}"

        raw_execution_id = payload.get("agent_execution_id")
        if not raw_execution_id:
            raise ExecutionContractError(
                f"event {event_id} of type {event_type} carries no agent_execution_id"
            )
        try:
            execution_id = UUID(str(raw_execution_id))
        except ValueError as error:
            raise ExecutionContractError(
                f"event {event_id} carries an invalid agent_execution_id"
            ) from error
        return self.handle_execution(execution_id)

    def handle_execution(self, execution_id: UUID) -> str:
        """Drive one execution from ``requested`` to a terminal state."""

        snapshot = self._read_execution(execution_id)
        if snapshot is None:
            raise LookupError(f"agent execution {execution_id} not found")

        # Idempotency: an outbox redelivery must never dispatch the same execution twice.
        if snapshot.status != "requested":
            return f"noop:{snapshot.status}"

        if snapshot.provider != self._provider.name:
            return self._fail(
                execution_id,
                "provider_not_configured",
                f"execution requested provider {snapshot.provider!r} but the gateway is "
                f"wired to {self._provider.name!r}",
            )

        request = DispatchRequest(
            execution_id=snapshot.id,
            task_id=snapshot.task_id,
            capability=snapshot.capability,
            agent_role=snapshot.agent_role,
            draft_id=snapshot.draft_id,
            draft_revision=snapshot.draft_revision,
            request_input=snapshot.request_input,
            constraints=snapshot.constraints,
            workspace_ref=snapshot.workspace_ref,
        )

        receipt = self._provider.dispatch(request)
        if receipt.accepted and not receipt.handoff_attempted:
            raise ExecutionContractError(
                f"provider {self._provider.name!r} reported accepted=True without a handoff attempt"
            )

        if not receipt.handoff_attempted:
            return self._fail(
                execution_id,
                receipt.error_code or "provider_unavailable",
                receipt.error_detail,
            )

        # The handoff was attempted, so "dispatched" is a DX fact regardless of whether
        # the provider accepted it.
        self._transition(
            execution_id, "dispatched", external_execution_id=receipt.external_execution_id
        )

        if not receipt.accepted:
            return self._fail(
                execution_id,
                receipt.error_code or "provider_reject",
                receipt.error_detail,
            )

        try:
            outcome = self._provider.collect(receipt)
        except ProviderCollectionError as error:
            # Expected operational outcome: the handoff already happened, so the
            # execution must reach a terminal state instead of being stranded in
            # dispatched (a stranded row can never be resumed by a redelivery, which
            # no-ops once status != requested). Only this class is caught: contract
            # violations and unexpected adapter bugs still propagate untouched.
            return self._fail(execution_id, "provider_unavailable", str(error))

        if outcome.status not in (OUTCOME_SUCCEEDED, OUTCOME_FAILED):
            raise ExecutionContractError(
                f"provider {self._provider.name!r} returned an unknown outcome status "
                f"{outcome.status!r}"
            )

        if outcome.started:
            self._transition(execution_id, "running")

        if outcome.status == OUTCOME_FAILED:
            return self._fail(
                execution_id,
                outcome.error_code or "runtime_error",
                outcome.error_detail,
                outcome=outcome,
            )

        self._transition(
            execution_id,
            "succeeded",
            summary=outcome.summary,
            artifacts=outcome.artifacts,
            log_references=outcome.log_references,
            result_tail=outcome.result_tail,
            exit_code=outcome.exit_code,
        )
        return OUTCOME_SUCCEEDED

    def _read_event(self, event_id: UUID) -> tuple[str, dict[str, Any]]:
        with self._session_factory() as session:
            event = session.get(DomainEvent, event_id)
            if event is None:
                raise LookupError(f"domain event {event_id} not found")
            return event.event_type, dict(event.payload or {})

    def _read_execution(self, execution_id: UUID) -> ExecutionSnapshot | None:
        with self._session_factory() as session:
            row = session.get(AgentExecution, execution_id)
            if row is None:
                return None
            return ExecutionSnapshot(
                id=row.id,
                task_id=row.task_id,
                provider=row.provider,
                capability=row.capability,
                agent_role=row.agent_role,
                draft_id=row.draft_id,
                draft_revision=row.draft_revision,
                workspace_ref=row.workspace_ref,
                request_input=dict(row.request_input or {}),
                constraints=dict(row.constraints) if row.constraints else None,
                status=row.status,
            )

    def _transition(self, execution_id: UUID, to_status: str, **fields: Any) -> Any:
        return self._service.transition(
            execution_id,
            to_status,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            idempotency_key=f"{execution_id}:{to_status}",
            **fields,
        )

    def _fail(
        self,
        execution_id: UUID,
        error_code: str,
        error_detail: str | None,
        *,
        outcome: ExecutionOutcome | None = None,
    ) -> str:
        self._transition(
            execution_id,
            "failed",
            error_code=error_code,
            error_detail=error_detail,
            summary=outcome.summary if outcome else None,
            artifacts=outcome.artifacts if outcome else None,
            log_references=outcome.log_references if outcome else None,
            result_tail=outcome.result_tail if outcome else None,
            exit_code=outcome.exit_code if outcome else None,
        )
        return f"failed:{error_code}"
