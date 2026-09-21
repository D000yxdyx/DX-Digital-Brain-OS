"""Phase E.2B tests: OutboxWorker -> Execution Gateway integration (dual principal).

Scope discipline: this module never touches the E.1 service, models, migrations or the
API. Parent rows are seeded through a local helper; no private helper is imported from
another test module.

What must be proven here:

A. outbox -> worker -> gateway -> fake provider -> full lifecycle, published + inbox.
B. the dx_worker principal really cannot UPDATE agent_executions.
C. the two principals are two different database roles, and the CLI gives each of them
   its own session factory.
D. without DX_EXECUTION_DATABASE_URL the Gateway is not wired and the worker keeps its
   original behaviour.
E. a handler exception leaves the row in processing (not published, no inbox ack), and
   the already-existing lease-expiry predicate reselects it later. No retry machinery.
F. lifecycle events emitted by transitions are ignored by the Gateway: no recursion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError

from dxbrain.config import WorkerSettings
from dxbrain.core.agent_executions import AgentExecutionService
from dxbrain.core.models import (
    AgentExecution,
    ApprovalGrant,
    AuditEvent,
    DomainEvent,
    EventInbox,
    EventOutbox,
    IntentProposal,
    ModelCall,
    PlanDraft,
    Task,
    WorkflowHandoff,
    WorkflowRun,
    WorkflowStep,
)
from dxbrain.execution.contracts import AGENT_EXECUTION_REQUESTED_EVENT
from dxbrain.execution.fake import BEHAVIOR_PRE_HANDOFF_FAILURE, FakeExecutionProvider
from dxbrain.execution.gateway import ExecutionGateway
from dxbrain.worker import cli
from dxbrain.worker.outbox import OutboxWorker


def _seed_parents(factory) -> dict[str, Any]:
    """Insert the governance + workflow parents an AgentExecution FK-references."""

    ids: dict[str, Any] = {
        "task_id": uuid4(),
        "proposal_id": uuid4(),
        "model_call_id": uuid4(),
        "draft_id": uuid4(),
        "approval_id": uuid4(),
        "handoff_id": uuid4(),
        "run_id": uuid4(),
        "step_id": uuid4(),
    }
    with factory.begin() as session:
        session.add(Task(id=ids["task_id"], title="agent execution worker target"))
        session.flush()
        session.add(
            IntentProposal(
                id=ids["proposal_id"],
                task_id=ids["task_id"],
                objective="o",
                rationale="r",
                scope={"side_effects": "none"},
                requested_capabilities=["agent.execution"],
                plan=[{"title": "t", "capability": "agent.execution", "parameters": {}}],
                actor_type="system",
                actor_id="seed",
                idempotency_key="seed-proposal",
                request_hash="h",
            )
        )
        session.flush()
        session.add(
            ModelCall(
                id=ids["model_call_id"],
                task_id=ids["task_id"],
                role="planner",
                mode="dry_run",
                status="dry_run",
                provider="deepseek",
                model="deepseek-chat",
                request_hash="h",
                request_summary={},
                response_summary={},
                actor_type="system",
                actor_id="seed",
                idempotency_key="seed-model-call",
            )
        )
        session.flush()
        session.add(
            PlanDraft(
                id=ids["draft_id"],
                task_id=ids["task_id"],
                proposal_id=ids["proposal_id"],
                model_call_id=ids["model_call_id"],
                parser_version="1",
                source_hash="h",
                steps=[{"title": "t"}],
                warnings=[],
                actor_type="system",
                actor_id="seed",
                idempotency_key="seed-draft",
                request_hash="h",
            )
        )
        session.flush()
        session.add(
            ApprovalGrant(
                id=ids["approval_id"],
                proposal_id=ids["proposal_id"],
                task_id=ids["task_id"],
                scope={"capabilities": ["agent.execution"], "side_effects": "none"},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                actor_type="system",
                actor_id="seed",
                idempotency_key="seed-approval",
                request_hash="h",
            )
        )
        session.flush()
        session.add(
            WorkflowHandoff(
                id=ids["handoff_id"],
                task_id=ids["task_id"],
                proposal_id=ids["proposal_id"],
                draft_id=ids["draft_id"],
                draft_revision=1,
                actor_type="system",
                actor_id="seed",
                idempotency_key="seed-handoff",
                request_hash="h",
            )
        )
        session.flush()
        session.add(
            WorkflowRun(
                id=ids["run_id"],
                task_id=ids["task_id"],
                proposal_id=ids["proposal_id"],
                handoff_id=ids["handoff_id"],
                draft_id=ids["draft_id"],
                draft_revision=1,
                actor_type="system",
                actor_id="seed",
                idempotency_key="seed-run",
                request_hash="h",
            )
        )
        session.flush()
        session.add(
            WorkflowStep(
                id=ids["step_id"],
                run_id=ids["run_id"],
                ordinal=1,
                title="step",
                capability="agent.execution",
                parameters={},
            )
        )
        session.flush()
    return ids


def _create_requested(factory, *, provider: str = "fake"):
    ids = _seed_parents(factory)
    service = AgentExecutionService(factory)
    return service.create(
        task_id=ids["task_id"],
        proposal_id=ids["proposal_id"],
        approval_id=ids["approval_id"],
        workflow_run_id=ids["run_id"],
        workflow_step_id=ids["step_id"],
        draft_id=ids["draft_id"],
        draft_revision=1,
        agent_role="executor",
        provider=provider,
        capability="agent.execution",
        request_input={"prompt": "go"},
        actor_type="system",
        actor_id="test",
        idempotency_key=f"exec-{uuid4()}",
        workspace_ref="default",
    )


def _requested_event_id(factory, execution_id: UUID) -> UUID:
    with factory() as session:
        events = session.scalars(
            select(DomainEvent).where(
                DomainEvent.event_type == AGENT_EXECUTION_REQUESTED_EVENT
            )
        ).all()
    for event in events:
        if event.payload.get("agent_execution_id") == str(execution_id):
            return event.id
    raise AssertionError(f"no {AGENT_EXECUTION_REQUESTED_EVENT} event for {execution_id}")


def _row(factory, execution_id: UUID) -> AgentExecution:
    with factory() as session:
        return session.get(AgentExecution, execution_id)


def _counts(factory) -> dict[str, int]:
    with factory() as session:
        return {
            "domain": session.scalar(select(func.count()).select_from(DomainEvent)),
            "audit": session.scalar(select(func.count()).select_from(AuditEvent)),
            "outbox": session.scalar(select(func.count()).select_from(EventOutbox)),
        }


def _outbox_state(factory, event_id: UUID) -> tuple[str, Any]:
    with factory() as session:
        row = session.scalar(select(EventOutbox).where(EventOutbox.event_id == event_id))
        assert row is not None
        return row.status, row.published_at


def _pending_outbox(factory) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count()).select_from(EventOutbox).where(EventOutbox.status == "pending")
        )


def _lease_owner(factory, event_id: UUID):
    with factory() as session:
        row = session.scalar(select(EventOutbox).where(EventOutbox.event_id == event_id))
        assert row is not None
        return row.lease_owner


def _expire_lease(factory, event_id: UUID) -> None:
    """Push an existing lease into the past. Test setup only -- no retry machinery."""

    with factory.begin() as session:
        session.execute(
            text(
                "UPDATE dx_events.event_outbox SET lease_until = now() - interval '1 minute' "
                "WHERE event_id = :event_id"
            ),
            {"event_id": event_id},
        )


def _inbox_rows(factory, consumer_name: str) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(EventInbox)
            .where(EventInbox.consumer_name == consumer_name)
        )


def _raising_handler(event_id: UUID) -> None:
    raise RuntimeError("handler failure")


def test_outbox_to_gateway_drives_full_lifecycle(session_factories) -> None:
    """A. requested outbox row -> worker -> gateway -> fake provider -> succeeded."""

    api_factory, worker_factory, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())
    worker = OutboxWorker(
        worker_factory, consumer_name="exec-consumer", handler=gateway.handle_event
    )

    event_id = worker.consume_one()

    assert event_id == _requested_event_id(migration_factory, view.id)
    row = _row(migration_factory, view.id)
    assert row.status == "succeeded"
    assert row.dispatched_at is not None
    assert row.started_at is not None
    assert row.finished_at is not None
    assert row.external_execution_id == f"fake-{view.id}"

    status, published_at = _outbox_state(migration_factory, event_id)
    assert status == "published"
    assert published_at is not None
    assert _inbox_rows(migration_factory, "exec-consumer") == 1

    # The three lifecycle events written by the transitions are pending, not consumed:
    # a single consume_one() must not cascade.
    assert _counts(migration_factory) == {"domain": 4, "audit": 4, "outbox": 4}
    assert _pending_outbox(migration_factory) == 3


def test_worker_principal_cannot_update_agent_executions(session_factories) -> None:
    """B. dx_worker is read-only on agent_executions -- proven against the real DB."""

    _, worker_factory, migration_factory = session_factories
    view = _create_requested(migration_factory)

    with pytest.raises(ProgrammingError) as excinfo:
        with worker_factory.begin() as session:
            session.execute(text("UPDATE dx_workflow.agent_executions SET status = 'failed'"))

    assert "permission denied" in str(excinfo.value).lower()
    assert _row(migration_factory, view.id).status == "requested"


def test_worker_and_execution_principals_are_distinct_roles(session_factories) -> None:
    """C. The two session factories really are two different database roles."""

    api_factory, worker_factory, _ = session_factories

    with worker_factory() as session:
        worker_user = session.scalar(text("SELECT current_user"))
    with api_factory() as session:
        execution_user = session.scalar(text("SELECT current_user"))

    assert worker_user == "dx_worker"
    assert execution_user == "dx_api"
    assert worker_user != execution_user


def test_cli_gives_each_principal_its_own_session_factory(
    session_factories, database_urls, monkeypatch
) -> None:
    """C. OutboxWorker only ever sees the worker factory; the Gateway only the api one."""

    api_url, worker_url, _ = database_urls
    api_factory, worker_factory, _ = session_factories
    factories = {worker_url: worker_factory, api_url: api_factory}
    requested_urls: list[str] = []
    captured: dict[str, Any] = {}

    def _fake_create_session_factory(url: str):
        requested_urls.append(url)
        return factories[url]

    class _RecordingWorker(OutboxWorker):
        def __init__(self, session_factory, consumer_name="kernel-tracer", *, handler=None):
            captured["worker_factory"] = session_factory
            captured["handler"] = handler
            super().__init__(session_factory, consumer_name=consumer_name, handler=handler)

    class _RecordingGateway:
        def __init__(self, session_factory, provider):
            captured["gateway_factory"] = session_factory
            captured["provider"] = provider

        def handle_event(self, event_id: UUID) -> str:
            return "ignored:stub"

    monkeypatch.setattr(cli, "create_session_factory", _fake_create_session_factory)
    monkeypatch.setattr(cli, "OutboxWorker", _RecordingWorker)
    monkeypatch.setattr(cli, "ExecutionGateway", _RecordingGateway)
    monkeypatch.setenv("DX_WORKER_DATABASE_URL", worker_url)
    monkeypatch.setenv("DX_EXECUTION_DATABASE_URL", api_url)

    cli.main()

    assert requested_urls == [worker_url, api_url]
    assert captured["worker_factory"] is worker_factory
    assert captured["gateway_factory"] is api_factory
    assert captured["gateway_factory"] is not captured["worker_factory"]
    assert isinstance(captured["provider"], FakeExecutionProvider)
    assert captured["handler"] is not None


def test_execution_url_absent_leaves_gateway_unwired(
    session_factories, database_urls, monkeypatch
) -> None:
    """D. No execution URL -> no Gateway, and the worker behaves exactly as before."""

    _, worker_url, _ = database_urls
    _, worker_factory, migration_factory = session_factories
    monkeypatch.setenv("DX_WORKER_DATABASE_URL", worker_url)
    monkeypatch.delenv("DX_EXECUTION_DATABASE_URL", raising=False)

    settings = WorkerSettings()
    assert settings.database_url == worker_url
    assert settings.execution_database_url is None
    assert cli.build_execution_handler(settings) is None

    view = _create_requested(migration_factory)
    worker = OutboxWorker(worker_factory, consumer_name="unwired-consumer")

    event_id = worker.consume_one()

    assert event_id is not None
    status, published_at = _outbox_state(migration_factory, event_id)
    assert status == "published"
    assert published_at is not None
    assert _inbox_rows(migration_factory, "unwired-consumer") == 1
    assert _row(migration_factory, view.id).status == "requested"


def test_handler_failure_keeps_processing_and_lease_reclaim_reconsumes(
    session_factories,
) -> None:
    """E. Handler exception -> not published; lease expiry -> reselected. No retry added."""

    _, worker_factory, migration_factory = session_factories
    view = _create_requested(migration_factory)
    state = {"fail": True}

    def _maybe_failing_handler(event_id: UUID) -> None:
        if state["fail"]:
            raise RuntimeError("handler failure")

    worker = OutboxWorker(
        worker_factory, consumer_name="failing-consumer", handler=_maybe_failing_handler
    )

    with pytest.raises(RuntimeError):
        worker.consume_one()

    event_id = _requested_event_id(migration_factory, view.id)
    status, published_at = _outbox_state(migration_factory, event_id)
    assert status == "processing"
    assert published_at is None
    assert _inbox_rows(migration_factory, "failing-consumer") == 0

    # Push the lease into the past. This only exercises the lease-expiry predicate that
    # already exists in OutboxWorker.consume_one; nothing retry-like is added here.
    _expire_lease(migration_factory, event_id)

    state["fail"] = False
    assert worker.consume_one() == event_id

    status, published_at = _outbox_state(migration_factory, event_id)
    assert status == "published"
    assert published_at is not None
    assert _inbox_rows(migration_factory, "failing-consumer") == 1


def test_expired_lease_is_reclaimed_by_a_different_worker_instance(session_factories) -> None:
    """Cross-instance crash recovery: a NEW worker must be able to finish the event.

    worker_A leases and its handler raises, so the row stays ``processing`` with A's
    lease. Once the lease is expired, a different worker instance (different owner) must
    re-acquire the row -- ownership has to transfer -- and publish it.
    """

    _, worker_factory, migration_factory = session_factories
    view = _create_requested(migration_factory)

    worker_a = OutboxWorker(
        worker_factory,
        consumer_name="reclaim-consumer",
        handler=_raising_handler,
    )
    with pytest.raises(RuntimeError):
        worker_a.consume_one()

    event_id = _requested_event_id(migration_factory, view.id)
    status, published_at = _outbox_state(migration_factory, event_id)
    assert status == "processing"
    assert published_at is None
    owner_a = _lease_owner(migration_factory, event_id)
    assert owner_a == worker_a._owner
    assert _inbox_rows(migration_factory, "reclaim-consumer") == 0

    # Test setup only: expire A's lease so the existing predicate reselects the row.
    _expire_lease(migration_factory, event_id)

    observed: dict[str, object] = {}

    def _capture_owner(event_id: UUID) -> None:
        # Runs between the leasing transaction and the publishing transaction, so this
        # observes the lease owner exactly at the transaction boundary.
        observed["owner"] = _lease_owner(migration_factory, event_id)

    worker_b = OutboxWorker(
        worker_factory, consumer_name="reclaim-consumer", handler=_capture_owner
    )
    assert worker_b._owner != worker_a._owner

    assert worker_b.consume_one() == event_id

    # Ownership transferred to B before publishing.
    assert observed["owner"] == worker_b._owner
    assert observed["owner"] != owner_a

    status, published_at = _outbox_state(migration_factory, event_id)
    assert status == "published"
    assert published_at is not None
    assert _inbox_rows(migration_factory, "reclaim-consumer") == 1
    assert _lease_owner(migration_factory, event_id) is None
    assert _row(migration_factory, view.id).status == "requested"


def test_lifecycle_events_do_not_recurse(session_factories) -> None:
    """F. dispatched / started / succeeded events are consumed but never dispatched."""

    api_factory, worker_factory, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())
    worker = OutboxWorker(
        worker_factory, consumer_name="recurse-consumer", handler=gateway.handle_event
    )

    assert worker.consume_one() == _requested_event_id(migration_factory, view.id)
    assert _counts(migration_factory) == {"domain": 4, "audit": 4, "outbox": 4}

    for _ in range(3):
        assert worker.consume_one() is not None

    assert _counts(migration_factory) == {"domain": 4, "audit": 4, "outbox": 4}
    assert _row(migration_factory, view.id).status == "succeeded"
    assert _pending_outbox(migration_factory) == 0


def test_failed_event_does_not_recurse(session_factories) -> None:
    """F. failed.v1 is consumed by the worker but ignored by the Gateway."""

    api_factory, worker_factory, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider(BEHAVIOR_PRE_HANDOFF_FAILURE))
    worker = OutboxWorker(
        worker_factory, consumer_name="failed-consumer", handler=gateway.handle_event
    )

    assert worker.consume_one() == _requested_event_id(migration_factory, view.id)
    assert _row(migration_factory, view.id).status == "failed"
    assert _counts(migration_factory) == {"domain": 2, "audit": 2, "outbox": 2}

    assert worker.consume_one() is not None

    assert _counts(migration_factory) == {"domain": 2, "audit": 2, "outbox": 2}
    assert _row(migration_factory, view.id).status == "failed"
