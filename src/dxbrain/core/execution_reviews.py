"""ExecutionReview persistence service (Operator / Task Card MVP, Phase E.4).

Records the human verdict on a technically terminal ``AgentExecution``. The verdict is
an independent fact: it never mutates ``AgentExecution.status`` and a ``succeeded``
execution may be ``rejected`` without changing that technical outcome.

Mirrors the repository's established human-review pattern (advisory xact lock +
idempotency replay / payload-mismatch + atomic DomainEvent + EventOutbox + AuditEvent):

* ``actor_type`` must be ``human``;
* the execution must exist and be ``succeeded``;
* ``decision`` must be ``accepted`` or ``rejected``;
* exactly one final review per execution (a conflicting second decision fails closed);
* a duplicate identical submission replays the existing review.

The event ``governance.execution.reviewed.v1`` is emitted but deliberately has no
worker routing: the existing execution worker ignores it exactly as it ignores other
unrelated event types today.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import (
    AgentExecution,
    AuditEvent,
    DomainEvent,
    EventOutbox,
    ExecutionReview,
)

ReviewDecision = Literal["accepted", "rejected"]


class ExecutionReviewPayloadMismatch(ValueError):
    """An idempotency key was reused with another review payload."""


class ExecutionReviewConflict(ValueError):
    """A review is not legal for the execution's current state or history."""


@dataclass(frozen=True)
class ExecutionReviewView:
    id: UUID
    agent_execution_id: UUID
    decision: str
    reason: str
    actor_type: str
    actor_id: str
    created_at: Any
    replayed: bool = False


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ExecutionReviewService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lock(session: Session, *, actor_type: str, actor_id: str, key: str) -> None:
        lock_key = f"{actor_type}:{actor_id}:execution-review:{key}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _view(row: ExecutionReview, *, replayed: bool = False) -> ExecutionReviewView:
        return ExecutionReviewView(
            id=row.id,
            agent_execution_id=row.agent_execution_id,
            decision=row.decision,
            reason=row.reason,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            created_at=row.created_at,
            replayed=replayed,
        )

    def record_decision(
        self,
        *,
        agent_execution_id: UUID,
        decision: ReviewDecision,
        reason: str,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ExecutionReviewView:
        if actor_type != "human":
            raise ExecutionReviewConflict("only a human actor may review an execution")
        if decision not in ("accepted", "rejected"):
            raise ValueError("execution review decision must be accepted or rejected")

        payload = {
            "agent_execution_id": str(agent_execution_id),
            "decision": decision,
            "reason": reason,
        }
        request_hash = _payload_hash(payload)

        with self._session_factory.begin() as session:
            # Lock on execution identity so concurrent review attempts serialize;
            # idempotency_key is captured for the audit trail only.
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                key=str(agent_execution_id),
            )
            execution = session.scalar(
                select(AgentExecution)
                .where(AgentExecution.id == agent_execution_id)
                .with_for_update()
            )
            if execution is None:
                raise LookupError("agent execution not found")
            if execution.status != "succeeded":
                raise ExecutionReviewConflict(
                    f"cannot review agent execution in status {execution.status}; "
                    "only a succeeded execution may be reviewed"
                )

            existing = session.scalar(
                select(ExecutionReview).where(
                    ExecutionReview.agent_execution_id == agent_execution_id
                )
            )
            if existing is not None:
                # Exactly one final review per execution. A duplicate identical
                # submission replays; a conflicting decision fails closed.
                if (
                    existing.decision == decision
                    and existing.actor_id == actor_id
                    and existing.request_hash == request_hash
                ):
                    return self._view(existing, replayed=True)
                raise ExecutionReviewConflict(
                    "agent execution already has a final review; "
                    "a second, conflicting decision is not allowed"
                )

            review = ExecutionReview(
                agent_execution_id=agent_execution_id,
                decision=decision,
                reason=reason,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(review)
            session.flush()

            detail = {
                "review_id": str(review.id),
                "agent_execution_id": str(agent_execution_id),
                "decision": decision,
            }
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=execution.task_id,
                event_type="governance.execution.reviewed.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="execution.review",
                    task_id=execution.task_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._view(review)

    def get(self, agent_execution_id: UUID) -> ExecutionReviewView | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ExecutionReview).where(
                    ExecutionReview.agent_execution_id == agent_execution_id
                )
            )
            return self._view(row) if row is not None else None
