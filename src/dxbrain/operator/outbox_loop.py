"""Bounded outbox drain loop for the Operator MVP (Phase E.4).

The existing ``OutboxWorker`` leases one event per ``consume_one()`` call with no
event-type filtering, so a target ``agent_execution.requested.v1`` event may be
preceded by older unrelated pending events. This module composes the *existing*
worker (never ``worker/outbox.py`` itself) into a finite loop that:

1. snapshots the pending + processing counts at loop start to derive a finite bound;
2. consumes exactly one existing outbox event per iteration;
3. re-reads the target execution status after each consume;
4. stops immediately once the target leaves ``requested``;
5. stops with an error if the queue empties or the bound is exhausted while the
   target is still ``requested``.

It never manually UPDATEs outbox rows, never deletes events, never calls a provider
or the Gateway directly, and never retries a failed execution. The loop is safe
against infinite draining because it stops the instant the target leaves ``requested``
regardless of how many unrelated events were consumed first.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import EventOutbox
from dxbrain.worker.outbox import OutboxWorker


class OutboxLoopError(RuntimeError):
    """The bounded drain loop could not reach a terminal target state."""


@dataclass(frozen=True)
class OutboxLoopResult:
    """What the bounded drain loop observed about the target execution."""

    execution_id: UUID
    consumed_events: int
    target_status: str


def snapshot_drain_bound(session_factory: sessionmaker[Session]) -> int:
    """Return the finite number of events the loop is allowed to consume.

    The worker leases either a ``pending`` event or an expired ``processing`` event, so
    both populations are counted. This is a read-only snapshot taken once, before any
    event is consumed.
    """

    with session_factory() as session:
        pending = session.scalar(
            select(func.count()).select_from(EventOutbox).where(EventOutbox.status == "pending")
        ) or 0
        processing = session.scalar(
            select(func.count())
            .select_from(EventOutbox)
            .where(EventOutbox.status == "processing")
        ) or 0
    return pending + processing


def read_status(session_factory: sessionmaker[Session], execution_id: UUID) -> str | None:
    """Return the target execution's current status, or None if it does not exist."""

    from dxbrain.core.models import AgentExecution

    with session_factory() as session:
        row = session.scalar(
            select(AgentExecution).where(AgentExecution.id == execution_id)
        )
        return row.status if row is not None else None


def drain_until_terminal(
    *,
    worker: OutboxWorker,
    session_factory: sessionmaker[Session],
    execution_id: UUID,
    initial_bound: int | None = None,
) -> OutboxLoopResult:
    """Consume existing outbox events until the target leaves ``requested``.

    ``worker`` must be an ``OutboxWorker`` wired with the existing execution handler
    (``build_execution_handler``), i.e. the same composition ``dxbrain-worker`` uses.
    The loop is finite by construction and never touches outbox rows directly.
    """

    bound = snapshot_drain_bound(session_factory) if initial_bound is None else initial_bound
    consumed = 0
    while consumed < bound:
        event_id = worker.consume_one()
        consumed += 1
        status = read_status(session_factory, execution_id)
        if status is None:
            raise OutboxLoopError(f"agent execution {execution_id} not found during drain")
        if status != "requested":
            return OutboxLoopResult(
                execution_id=execution_id, consumed_events=consumed, target_status=status
            )
        if event_id is None:
            # Queue emptied while the target is still requested: the target event is
            # not present, so do not loop forever and do not retry.
            raise OutboxLoopError(
                f"outbox drained (empty queue) while agent execution {execution_id} "
                "was still requested; target event absent"
            )

    raise OutboxLoopError(
        f"outbox drain bound {bound} exhausted while agent execution {execution_id} "
        "was still requested"
    )
