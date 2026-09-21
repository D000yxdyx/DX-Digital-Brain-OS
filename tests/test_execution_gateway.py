"""Phase E.2A tests: execution contracts, the Fake provider, and the Execution Gateway.

Scope discipline (Owner constraint for E.2A): these tests never touch the
OutboxWorker, the worker CLI, configuration, migrations, or the E.1 service internals.
The Gateway is invoked directly. Parent rows are seeded through a local helper so this
module does not import a private helper from another test module.

Lifecycle paths covered (frozen E.2A semantics):

* requested -> dispatched -> running -> succeeded
* requested -> failed                                (pre-handoff failure)
* requested -> dispatched -> failed                  (handoff attempted, provider rejected)
* requested -> dispatched -> failed                  (accepted, never started)
* requested -> dispatched -> running -> failed       (runtime failure)
* requested -> dispatched -> failed                  (operational collection failure)
* requested -> failed                                (provider not configured)

Unexpected exceptions (dispatch or collect) propagate with zero lifecycle writes; they
are never converted into a lifecycle outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.agent_executions import AgentExecutionService
from dxbrain.core.models import (
    AgentExecution,
    ApprovalGrant,
    AuditEvent,
    DomainEvent,
    EventOutbox,
    IntentProposal,
    ModelCall,
    PlanDraft,
    Task,
    WorkflowHandoff,
    WorkflowRun,
    WorkflowStep,
)
from dxbrain.execution.contracts import (
    AGENT_EXECUTION_REQUESTED_EVENT,
    OUTCOME_SUCCEEDED,
    DispatchReceipt,
    DispatchRequest,
    ExecutionContractError,
    ExecutionOutcome,
    ExecutionProviderError,
)
from dxbrain.execution.fake import (
    BEHAVIOR_COLLECT_ERROR,
    BEHAVIOR_HANDOFF_REJECTED,
    BEHAVIOR_NEVER_STARTED,
    BEHAVIOR_PRE_HANDOFF_FAILURE,
    BEHAVIOR_RUNTIME_FAILED,
    BEHAVIOR_SUCCEEDED,
    BEHAVIOR_UNEXPECTED_ERROR,
    FakeExecutionProvider,
)
from dxbrain.execution.gateway import ExecutionGateway

_EXECUTION_DIR = Path(__file__).parents[1] / "src" / "dxbrain" / "execution"


def _seed_parents(factory) -> dict[str, Any]:
    """Insert the governance + workflow parents an AgentExecution FK-references.

    Mirrors production behaviour by flushing after each add: within one transaction
    SQLAlchemy does not order cross-schema inserts.
    """

    ids: dict[str, Any] = {
        "task_id": uuid4(),
        "proposal_id": uuid4(),
        "model_call_id": uuid4(),
        "draft_id": uuid4(),
        "approval_id": uuid4(),
        "handoff_id": uuid4(),
        "run_id": uuid4(),
        "step_id": uuid4(),
        "draft_revision": 1,
    }
    with factory.begin() as session:
        session.add(Task(id=ids["task_id"], title="agent execution target"))
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


def _create_requested(factory, *, provider: str = "fake", idempotency_key: str | None = None):
    """Seed parents and create one ``requested`` execution through the E.1 service."""

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
        idempotency_key=idempotency_key or f"exec-{uuid4()}",
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


def _event_types(factory) -> set[str]:
    with factory() as session:
        return set(session.scalars(select(DomainEvent.event_type)).all())


def _sample_request(execution_id: UUID | None = None) -> DispatchRequest:
    return DispatchRequest(
        execution_id=execution_id or UUID(int=1),
        task_id=UUID(int=2),
        capability="agent.execution",
        agent_role="executor",
        draft_id=UUID(int=3),
        draft_revision=1,
        request_input={"prompt": "go"},
    )


class _ContractViolatingProvider:
    """Returns accepted=True without a handoff attempt -- a contract violation."""

    name = "fake"

    def dispatch(self, request: DispatchRequest) -> DispatchReceipt:
        return DispatchReceipt(handoff_attempted=False, accepted=True)

    def collect(self, receipt: DispatchReceipt) -> ExecutionOutcome:
        return ExecutionOutcome(receipt.external_execution_id, True, OUTCOME_SUCCEEDED)


class _UnexpectedCollectProvider:
    """Accepts the handoff, then blows up while collecting -- an unexpected bug."""

    name = "fake"

    def dispatch(self, request: DispatchRequest) -> DispatchReceipt:
        return DispatchReceipt(
            handoff_attempted=True,
            accepted=True,
            external_execution_id=FakeExecutionProvider.external_execution_id(
                request.execution_id
            ),
        )

    def collect(self, receipt: DispatchReceipt) -> ExecutionOutcome:
        raise RuntimeError("adapter bug: unexpected failure while collecting")


class _CountingSessionFactory(sessionmaker):
    """Wraps a session factory to expose how many sessions are currently open."""

    def __init__(self, factory: sessionmaker) -> None:
        super().__init__(bind=factory.kw["bind"], expire_on_commit=False, autoflush=False)
        self.open_sessions = 0
        self.created = 0

    def __call__(self, **local_kw: Any) -> Session:
        session = super().__call__(**local_kw)
        self.created += 1
        self.open_sessions += 1
        original_close = session.close

        def _close() -> None:
            self.open_sessions -= 1
            original_close()

        session.close = _close  # type: ignore[method-assign]
        return session


def test_contract_and_fake_modules_import_no_sqlalchemy() -> None:
    """The provider isolation boundary: adapters must never reach the database."""

    for name in ("contracts.py", "fake.py"):
        source = (_EXECUTION_DIR / name).read_text(encoding="utf-8")
        assert "sqlalchemy" not in source
        assert "dxbrain.core.models" not in source


def test_fake_provider_is_deterministic() -> None:
    provider = FakeExecutionProvider()
    request = _sample_request()

    first = provider.dispatch(request)
    second = provider.dispatch(request)

    assert first == second
    assert first.handoff_attempted is True
    assert first.accepted is True
    # Derived, never random: the same execution id always maps to the same external id.
    assert first.external_execution_id == "fake-00000000-0000-0000-0000-000000000001"
    assert provider.collect(first) == provider.collect(first)


@pytest.mark.parametrize(
    "behavior,handoff_attempted,accepted",
    [
        (BEHAVIOR_SUCCEEDED, True, True),
        (BEHAVIOR_PRE_HANDOFF_FAILURE, False, False),
        (BEHAVIOR_HANDOFF_REJECTED, True, False),
        (BEHAVIOR_NEVER_STARTED, True, True),
        (BEHAVIOR_RUNTIME_FAILED, True, True),
        (BEHAVIOR_COLLECT_ERROR, True, True),
    ],
)
def test_fake_behavior_matrix(behavior: str, handoff_attempted: bool, accepted: bool) -> None:
    provider = FakeExecutionProvider(behavior)
    receipt = provider.dispatch(_sample_request())

    assert receipt.handoff_attempted is handoff_attempted
    assert receipt.accepted is accepted


def test_fake_unexpected_failure_raises() -> None:
    provider = FakeExecutionProvider(BEHAVIOR_UNEXPECTED_ERROR)
    with pytest.raises(RuntimeError):
        provider.dispatch(_sample_request())


def test_fake_rejects_unknown_behavior() -> None:
    with pytest.raises(ValueError):
        FakeExecutionProvider("not-a-behavior")


def test_happy_path_drives_full_lifecycle(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == OUTCOME_SUCCEEDED
    row = _row(migration_factory, view.id)
    assert row.status == "succeeded"
    assert row.external_execution_id == f"fake-{view.id}"
    assert row.dispatched_at is not None
    assert row.started_at is not None
    assert row.finished_at is not None
    assert row.exit_code == 0
    assert row.summary == "fake execution completed"
    assert row.error_code is None

    counts = _counts(migration_factory)
    assert counts == {"domain": 4, "audit": 4, "outbox": 4}
    assert _event_types(migration_factory) == {
        "agent_execution.requested.v1",
        "agent_execution.dispatched.v1",
        "agent_execution.started.v1",
        "agent_execution.succeeded.v1",
    }


def test_pre_handoff_failure_never_reaches_dispatched(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider(BEHAVIOR_PRE_HANDOFF_FAILURE))

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == "failed:provider_unavailable"
    row = _row(migration_factory, view.id)
    assert row.status == "failed"
    assert row.dispatched_at is None
    assert row.external_execution_id is None
    assert row.error_code == "provider_unavailable"
    assert _event_types(migration_factory) == {
        "agent_execution.requested.v1",
        "agent_execution.failed.v1",
    }
    assert _counts(migration_factory) == {"domain": 2, "audit": 2, "outbox": 2}


def test_handoff_rejected_dispatches_then_fails(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider(BEHAVIOR_HANDOFF_REJECTED))

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == "failed:provider_reject"
    row = _row(migration_factory, view.id)
    assert row.status == "failed"
    # dispatched is a DX fact: the handoff was attempted even though the provider refused.
    assert row.dispatched_at is not None
    assert row.started_at is None
    assert row.external_execution_id is None
    assert row.error_code == "provider_reject"
    assert "agent_execution.dispatched.v1" in _event_types(migration_factory)


def test_accepted_but_never_started_fails_after_dispatch(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider(BEHAVIOR_NEVER_STARTED))

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == "failed:provider_reject"
    row = _row(migration_factory, view.id)
    assert row.status == "failed"
    assert row.dispatched_at is not None
    assert row.started_at is None
    assert row.finished_at is not None
    assert row.external_execution_id == f"fake-{view.id}"


def test_runtime_failure_passes_through_running(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider(BEHAVIOR_RUNTIME_FAILED))

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == "failed:runtime_error"
    row = _row(migration_factory, view.id)
    assert row.status == "failed"
    assert row.dispatched_at is not None
    assert row.started_at is not None
    assert row.finished_at is not None
    assert row.exit_code == 1
    assert row.error_code == "runtime_error"
    assert _event_types(migration_factory) == {
        "agent_execution.requested.v1",
        "agent_execution.dispatched.v1",
        "agent_execution.started.v1",
        "agent_execution.failed.v1",
    }


def test_provider_not_configured_fails_before_dispatch(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory, provider="multica")
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == "failed:provider_not_configured"
    row = _row(migration_factory, view.id)
    assert row.status == "failed"
    assert row.dispatched_at is None
    assert row.error_code == "provider_not_configured"
    assert "agent_execution.dispatched.v1" not in _event_types(migration_factory)


def test_unexpected_dispatch_exception_propagates_without_lifecycle_write(
    session_factories,
) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider(BEHAVIOR_UNEXPECTED_ERROR))

    with pytest.raises(RuntimeError):
        gateway.handle_event(_requested_event_id(migration_factory, view.id))

    row = _row(migration_factory, view.id)
    assert row.status == "requested"
    assert row.dispatched_at is None
    assert _counts(migration_factory) == {"domain": 1, "audit": 1, "outbox": 1}


def test_operational_collect_failure_is_typed_as_provider_error() -> None:
    """collect_error is an operational outcome, not an unexpected adapter bug."""

    provider = FakeExecutionProvider(BEHAVIOR_COLLECT_ERROR)
    receipt = provider.dispatch(_sample_request())

    with pytest.raises(ExecutionProviderError):
        provider.collect(receipt)


def test_operational_collect_failure_fails_after_dispatch(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider(BEHAVIOR_COLLECT_ERROR))

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == "failed:provider_unavailable"
    row = _row(migration_factory, view.id)
    assert row.status == "failed"
    assert row.dispatched_at is not None
    assert row.started_at is None
    assert row.finished_at is not None
    assert row.error_code == "provider_unavailable"
    assert _event_types(migration_factory) == {
        "agent_execution.requested.v1",
        "agent_execution.dispatched.v1",
        "agent_execution.failed.v1",
    }
    assert _counts(migration_factory) == {"domain": 3, "audit": 3, "outbox": 3}


def test_unexpected_collect_exception_propagates_and_keeps_dispatched(session_factories) -> None:
    """An unexpected adapter bug must NOT be converted into a lifecycle outcome."""

    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, _UnexpectedCollectProvider())

    with pytest.raises(RuntimeError):
        gateway.handle_event(_requested_event_id(migration_factory, view.id))

    # No terminal state is fabricated: the row keeps the last DX-recorded fact.
    row = _row(migration_factory, view.id)
    assert row.status == "dispatched"
    assert row.started_at is None
    assert row.finished_at is None
    assert row.external_execution_id == f"fake-{view.id}"


def test_contract_violation_writes_nothing(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, _ContractViolatingProvider())

    with pytest.raises(ExecutionContractError):
        gateway.handle_event(_requested_event_id(migration_factory, view.id))

    row = _row(migration_factory, view.id)
    assert row.status == "requested"
    assert _counts(migration_factory) == {"domain": 1, "audit": 1, "outbox": 1}


def test_redelivery_is_a_noop(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())
    event_id = _requested_event_id(migration_factory, view.id)

    assert gateway.handle_event(event_id) == OUTCOME_SUCCEEDED
    after_first = _counts(migration_factory)

    assert gateway.handle_event(event_id) == "noop:succeeded"
    assert _counts(migration_factory) == after_first
    assert _row(migration_factory, view.id).status == "succeeded"


def test_non_requested_event_is_ignored(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())
    gateway.handle_event(_requested_event_id(migration_factory, view.id))

    with migration_factory() as session:
        other = session.scalar(
            select(DomainEvent).where(
                DomainEvent.event_type == "agent_execution.dispatched.v1"
            )
        )
        assert other is not None
        other_id = other.id
    before = _counts(migration_factory)

    assert gateway.handle_event(other_id) == "ignored:agent_execution.dispatched.v1"
    assert _counts(migration_factory) == before


def test_unknown_execution_raises(session_factories) -> None:
    api_factory, _, _ = session_factories
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())

    with pytest.raises(LookupError):
        gateway.handle_execution(uuid4())


def test_event_type_constant_matches_the_event_e1_emits(session_factories) -> None:
    _, _, migration_factory = session_factories
    view = _create_requested(migration_factory)

    assert AGENT_EXECUTION_REQUESTED_EVENT in _event_types(migration_factory)
    assert _requested_event_id(migration_factory, view.id) is not None


def test_no_session_is_open_across_provider_calls(session_factories) -> None:
    api_factory, _, migration_factory = session_factories
    view = _create_requested(migration_factory)
    spy = _CountingSessionFactory(api_factory)
    observed: list[tuple[str, int]] = []

    class _RecordingProvider:
        name = "fake"

        def dispatch(self, request: DispatchRequest) -> DispatchReceipt:
            observed.append(("dispatch", spy.open_sessions))
            return DispatchReceipt(
                handoff_attempted=True,
                accepted=True,
                external_execution_id=FakeExecutionProvider.external_execution_id(
                    request.execution_id
                ),
            )

        def collect(self, receipt: DispatchReceipt) -> ExecutionOutcome:
            observed.append(("collect", spy.open_sessions))
            return ExecutionOutcome(
                receipt.external_execution_id, True, OUTCOME_SUCCEEDED, exit_code=0
            )

    gateway = ExecutionGateway(spy, _RecordingProvider())
    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == OUTCOME_SUCCEEDED
    assert observed == [("dispatch", 0), ("collect", 0)]
    assert spy.open_sessions == 0
