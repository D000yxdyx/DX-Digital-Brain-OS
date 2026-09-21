"""Focused persistence tests for the AgentExecution bridge (Phase E.1).

These exercise only the persistence layer (``AgentExecutionService`` + the
``dx_workflow.agent_executions`` table + the frozen five-state lifecycle). They do
NOT touch the Execution Gateway, any provider adapter, the OutboxWorker, or any API
endpoint. Parent rows (task / proposal / model-call / plan-draft / approval / handoff /
run / step) are seeded directly so the AgentExecution foreign keys can be satisfied.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm.session import SessionTransaction

from dxbrain.core.agent_executions import (
    AgentExecutionConflict,
    AgentExecutionPayloadMismatch,
    AgentExecutionService,
)
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


def _seed_graph(migration_factory) -> dict[str, UUID | int]:
    """Insert the minimal governance+workflow parent graph an AgentExecution needs."""
    ids: dict[str, UUID | int] = {
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
    now = datetime.now(UTC)
    # NOTE: within a single transaction SQLAlchemy does not autonomously order
    # cross-schema inserts (Task -> IntentProposal -> ApprovalGrant -> ...). The
    # production app inserts in separate transactions; this seed mirrors that by
    # flushing after each add so every parent row exists before its child FK.
    with migration_factory.begin() as session:
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
                expires_at=now + timedelta(hours=1),
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


def _create_execution(service, ids, *, idempotency_key="exec-1", request_input=None, **kw):
    return service.create(
        task_id=ids["task_id"],
        proposal_id=ids["proposal_id"],
        approval_id=ids["approval_id"],
        workflow_run_id=ids["run_id"],
        workflow_step_id=ids["step_id"],
        draft_id=ids["draft_id"],
        draft_revision=ids["draft_revision"],  # type: ignore[arg-type]
        agent_role="executor",
        provider="multica",
        capability="agent.execution",
        request_input=request_input if request_input is not None else {"prompt": "go"},
        actor_type="system",
        actor_id="test",
        idempotency_key=idempotency_key,
        workspace_ref="default",
        **kw,
    )


def _drive_to(service, view, target: str):
    """Walk a fresh execution to ``target`` using only legal transitions."""
    steps = {
        "requested": [],
        "dispatched": ["dispatched"],
        "running": ["dispatched", "running"],
        "succeeded": ["dispatched", "running", "succeeded"],
        "failed": ["dispatched", "running", "failed"],
    }[target]
    for step in steps:
        view = service.transition(
            view.id, step, actor_type="system", actor_id="test", idempotency_key=f"drive-{step}"
        )
    return view


def test_first_create_succeeds(session_factories) -> None:
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    view = _create_execution(service, ids)

    assert isinstance(view.id, UUID)
    assert view.status == "requested"
    with migration_factory() as session:
        row = session.get(AgentExecution, view.id)
        assert row is not None
        assert row.status == "requested"
        assert (
            session.scalar(
                select(func.count())
                .select_from(DomainEvent)
                .where(DomainEvent.event_type == "agent_execution.requested.v1")
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(AuditEvent)) >= 1
        assert session.scalar(select(func.count()).select_from(EventOutbox)) >= 1


def test_idempotent_replay_returns_same_execution(session_factories) -> None:
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    first = _create_execution(service, ids, idempotency_key="replay-key")
    second = _create_execution(service, ids, idempotency_key="replay-key")

    assert second.id == first.id
    assert second.replayed is True
    with migration_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentExecution)
                .where(
                    AgentExecution.actor_type == "system",
                    AgentExecution.actor_id == "test",
                    AgentExecution.idempotency_key == "replay-key",
                )
            )
            == 1
        )


def test_payload_mismatch_on_different_request(session_factories) -> None:
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    _create_execution(
        service, ids, idempotency_key="mismatch-key", request_input={"prompt": "go"}
    )
    with pytest.raises(AgentExecutionPayloadMismatch):
        _create_execution(
            service,
            ids,
            idempotency_key="mismatch-key",
            request_input={"prompt": "different"},
        )


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("requested", "dispatched"),
        ("requested", "failed"),
        ("dispatched", "running"),
        ("dispatched", "failed"),
        ("running", "succeeded"),
        ("running", "failed"),
    ],
)
def test_every_legal_transition_succeeds(
    session_factories, from_status: str, to_status: str
) -> None:
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    view = _create_execution(service, ids)
    if from_status == "dispatched":
        view = service.transition(
            view.id, "dispatched", actor_type="system", actor_id="test",
            idempotency_key="t1", external_execution_id="ext-1",
        )
    elif from_status == "running":
        view = service.transition(
            view.id, "dispatched", actor_type="system", actor_id="test", idempotency_key="t1"
        )
        view = service.transition(
            view.id, "running", actor_type="system", actor_id="test", idempotency_key="t2"
        )
    assert view.status == from_status

    result = service.transition(
        view.id, to_status, actor_type="system", actor_id="test", idempotency_key="t-final"
    )
    assert result.status == to_status


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("requested", "running"),
        ("requested", "succeeded"),
        ("dispatched", "requested"),
        ("dispatched", "succeeded"),
        ("running", "requested"),
        ("running", "dispatched"),
        ("succeeded", "dispatched"),
        ("succeeded", "running"),
        ("succeeded", "failed"),
        ("failed", "dispatched"),
        ("failed", "running"),
        ("failed", "succeeded"),
    ],
)
def test_every_illegal_transition_fails(
    session_factories, from_status: str, to_status: str
) -> None:
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    view = _create_execution(service, ids)
    view = _drive_to(service, view, from_status)
    with pytest.raises(AgentExecutionConflict):
        service.transition(
            view.id, to_status, actor_type="system", actor_id="test", idempotency_key="ill"
        )


def test_terminal_state_cannot_transition_further(session_factories) -> None:
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    succeeded = _drive_to(service, _create_execution(service, ids, idempotency_key="term-succ"), "succeeded")
    failed = _drive_to(service, _create_execution(service, ids, idempotency_key="term-fail"), "failed")
    for terminal in (succeeded, failed):
        for bad in ("requested", "dispatched", "running", "succeeded", "failed"):
            with pytest.raises(AgentExecutionConflict):
                service.transition(
                    terminal.id, bad, actor_type="system", actor_id="test", idempotency_key="x"
                )


def test_requested_failure_path_works(session_factories) -> None:
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    view = _create_execution(service, ids)
    failed = service.transition(
        view.id,
        "failed",
        actor_type="system",
        actor_id="test",
        idempotency_key="f1",
        error_code="workspace_denied",
        error_detail="workspace_ref default not resolvable",
    )
    assert failed.status == "failed"
    assert failed.error_code == "workspace_denied"
    with migration_factory() as session:
        row = session.get(AgentExecution, view.id)
        assert row.status == "failed"
        assert row.error_code == "workspace_denied"
        assert (
            session.scalar(
                select(func.count())
                .select_from(DomainEvent)
                .where(DomainEvent.event_type == "agent_execution.failed.v1")
            )
            == 1
        )


def test_dispatched_failure_path_works(session_factories) -> None:
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    view = _create_execution(service, ids)
    dispatched = service.transition(
        view.id, "dispatched", actor_type="system", actor_id="test",
        idempotency_key="d1", external_execution_id="ext-9",
    )
    assert dispatched.status == "dispatched"
    assert dispatched.external_execution_id == "ext-9"

    failed = service.transition(
        view.id, "failed", actor_type="system", actor_id="test",
        idempotency_key="f1", error_code="provider_reject",
        error_detail="provider rejected job after handoff",
    )
    assert failed.status == "failed"
    assert failed.error_code == "provider_reject"
    with migration_factory() as session:
        row = session.get(AgentExecution, view.id)
        assert row.status == "failed"
        assert row.dispatched_at is not None
        assert row.finished_at is not None


def test_rollback_prevents_partial_persistence(session_factories, monkeypatch) -> None:
    """A commit failure after all four writes must roll back domain + event + outbox + audit.

    Failure injection is at the test layer (monkeypatched ``SessionTransaction.commit``) --
    the production service carries no test-only hook. All four rows (domain + DomainEvent +
    EventOutbox + AuditEvent) are flushed before the injected failure, so the single
    transaction must roll back together. ``SessionTransaction.__exit__`` rolls back before
    re-raising, so the aborted writes never become visible.
    """
    _, _, migration_factory = session_factories
    ids = _seed_graph(migration_factory)
    service = AgentExecutionService(migration_factory)

    view = _create_execution(service, ids)

    def _failing_commit(self: SessionTransaction) -> None:  # type: ignore[valid-type]
        # Raise only on the transition's commit; the create above already committed cleanly.
        raise RuntimeError("injected failure to verify atomic rollback")

    monkeypatch.setattr(SessionTransaction, "commit", _failing_commit)
    try:
        with pytest.raises(RuntimeError):
            service.transition(
                view.id, "dispatched", actor_type="system", actor_id="test",
                idempotency_key="rb",
            )
    finally:
        # Restore the real commit before the verification read so the read session commits.
        monkeypatch.undo()

    with migration_factory() as session:
        row = session.get(AgentExecution, view.id)
        # Domain row was NOT mutated by the aborted transition.
        assert row.status == "requested"
        assert row.dispatched_at is None
        # Only the create's requested event/outbox/audit survived; the dispatched
        # transition wrote nothing durable.
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert session.scalar(select(func.count()).select_from(EventOutbox)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(DomainEvent)
                .where(DomainEvent.event_type == "agent_execution.dispatched.v1")
            )
            == 0
        )
