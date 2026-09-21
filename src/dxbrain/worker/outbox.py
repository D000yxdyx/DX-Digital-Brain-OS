from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import EventInbox, EventOutbox


class OutboxWorker:
    """A minimal durable consumer; handlers are intentionally internal-only in v0.1.

    ``handler`` is optional and purely additive: with ``handler=None`` the worker
    behaves exactly as it did before handlers existed (lease -> inbox -> publish).

    Ordering is fixed and must not be "optimised": the handler runs strictly between
    the leasing transaction and the publishing transaction, never inside either one, so
    a slow or failing handler holds no database transaction open. A handler exception
    propagates and the publishing transaction is skipped, which leaves the row in
    ``processing`` with its existing lease -- the already-present lease-expiry predicate
    makes it recoverable on a later call. No retry logic is introduced here.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        consumer_name: str = "kernel-tracer",
        *,
        handler: Callable[[UUID], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._consumer_name = consumer_name
        self._owner = f"{consumer_name}:{uuid4()}"
        self._handler = handler

    def consume_one(self, lease_seconds: int = 30) -> UUID | None:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            row = session.scalar(
                select(EventOutbox)
                .where(
                    or_(
                        EventOutbox.status == "pending",
                        and_(EventOutbox.status == "processing", EventOutbox.lease_until < now),
                    )
                )
                .order_by(EventOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "processing"
            row.lease_owner = self._owner
            row.lease_until = now + timedelta(seconds=lease_seconds)
            event_id = row.event_id

        if self._handler is not None:
            self._handler(event_id)

        with self._session_factory.begin() as session:
            session.execute(
                insert(EventInbox)
                .values(consumer_name=self._consumer_name, event_id=event_id)
                .on_conflict_do_nothing(constraint="uq_inbox_consumer_event")
            )
            row = session.scalar(
                select(EventOutbox)
                .where(EventOutbox.event_id == event_id, EventOutbox.lease_owner == self._owner)
                .with_for_update()
            )
            if row is None:
                return None
            row.status = "published"
            row.lease_owner = None
            row.lease_until = None
            row.published_at = datetime.now(UTC)
        return event_id
