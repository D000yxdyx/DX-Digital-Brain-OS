"""Phase E.4 tests: Operator task-card contract and bounded outbox drain.

Scope discipline:
* Task-card tests exercise the pure contract helpers and the existing production
  services (TaskService, GovernanceService) directly — no plan/execute live call.
* Outbox-drain tests compose the *existing* OutboxWorker + ExecutionGateway + fake
  provider exactly as the operator does, and prove the loop is bounded and stops the
  instant the target leaves ``requested``.

No real Codex, no live model call, no hardcoded proxy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from dxbrain.core.agent_executions import AgentExecutionService
from dxbrain.core.models import (
    ApprovalGrant,
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
from dxbrain.execution.contracts import AGENT_EXECUTION_REQUESTED_EVENT
from dxbrain.execution.fake import FakeExecutionProvider
from dxbrain.execution.gateway import ExecutionGateway
from dxbrain.operator.cli import (
    OperatorError,
    build_scope,
    parse_intent,
    require_single_step,
)
from dxbrain.operator.outbox_loop import (
    OutboxLoopError,
    drain_until_terminal,
    read_status,
    snapshot_drain_bound,
)
from dxbrain.worker.outbox import OutboxWorker

# --- pure task-card contract -------------------------------------------------------

class _Step:
    def __init__(self, id, ordinal, title, capability, parameters, status, result_summary=None):
        self.id = id
        self.ordinal = ordinal
        self.title = title
        self.capability = capability
        self.parameters = parameters
        self.status = status
        self.result_summary = result_summary


def _valid_scope() -> dict[str, Any]:
    return {
        "workspace_ref": "default",
        "provider": "codex",
        "capability": "agent.execution",
        "agent_id": str(uuid4()),
    }


def test_intent_roundtrip() -> None:
    scope = _valid_scope()
    intent = parse_intent(scope)
    assert build_scope(intent) == scope


def test_malformed_scope_missing_fields_fails_closed() -> None:
    scope = _valid_scope()
    del scope["workspace_ref"]
    with pytest.raises(OperatorError):
        parse_intent(scope)


def test_malformed_scope_bad_workspace_ref_fails_closed() -> None:
    scope = _valid_scope()
    scope["workspace_ref"] = "/etc/passwd"
    with pytest.raises(OperatorError):
        parse_intent(scope)


def test_malformed_scope_bad_agent_id_fails_closed() -> None:
    scope = _valid_scope()
    scope["agent_id"] = "not-a-uuid"
    with pytest.raises(OperatorError):
        parse_intent(scope)


def test_malformed_scope_bad_provider_fails_closed() -> None:
    scope = _valid_scope()
    scope["provider"] = ""
    with pytest.raises(OperatorError):
        parse_intent(scope)


def test_malformed_scope_non_dict_fails_closed() -> None:
    with pytest.raises(OperatorError):
        parse_intent("not-a-dict")  # type: ignore[arg-type]


def test_require_single_step_accepts_one() -> None:
    step = _Step(uuid4(), 1, "t", "agent.execution", {}, "pending")
    assert require_single_step([step]) is step


def test_require_single_step_rejects_multi() -> None:
    steps = [
        _Step(uuid4(), 1, "a", "agent.execution", {}, "pending"),
        _Step(uuid4(), 2, "b", "agent.execution", {}, "pending"),
    ]
    with pytest.raises(OperatorError):
        require_single_step(steps)


def test_require_single_step_rejects_empty() -> None:
    with pytest.raises(OperatorError):
        require_single_step([])


# --- create / approve composition + stable intent ---------------------------------

def _seed_execution_parents(factory) -> dict[str, Any]:
    ids = {
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
        session.add(Task(id=ids["task_id"], title="operator flow target"))
        session.flush()
        session.add(
            IntentProposal(
                id=ids["proposal_id"],
                task_id=ids["task_id"],
                objective="o",
                rationale="r",
                scope=_valid_scope(),
                requested_capabilities=["model.call", "agent.execution"],
                plan=[{"title": "t", "capability": "agent.execution", "parameters": {}}],
                actor_type="human",
                actor_id="daryl",
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
                steps=[{"title": "t", "capability": "agent.execution", "parameters": {}}],
                warnings=[],
                actor_type="human",
                actor_id="daryl",
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
                scope={"capabilities": ["model.call", "agent.execution"]},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                actor_type="human",
                actor_id="daryl",
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
                actor_type="human",
                actor_id="daryl",
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
                actor_type="human",
                actor_id="daryl",
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


def _create_requested(factory, *, provider: str = "fake") -> Any:
    ids = _seed_execution_parents(factory)
    service = AgentExecutionService(factory)
    return service.create(
        task_id=ids["task_id"],
        proposal_id=ids["proposal_id"],
        approval_id=ids["approval_id"],
        workflow_run_id=ids["run_id"],
        workflow_step_id=ids["step_id"],
        draft_id=ids["draft_id"],
        draft_revision=1,
        agent_role="operator",
        agent_id=uuid4(),
        provider=provider,
        capability="agent.execution",
        workspace_ref="default",
        request_input={"title": "step", "capability": "agent.execution", "parameters": {}},
        actor_type="human",
        actor_id="daryl",
        idempotency_key=f"exec-{uuid4()}",
    )


def _requested_event_id(factory, execution_id) -> Any:
    with factory() as session:
        events = session.scalars(
            select(DomainEvent).where(DomainEvent.event_type == AGENT_EXECUTION_REQUESTED_EVENT)
        ).all()
    for event in events:
        if event.payload.get("agent_execution_id") == str(execution_id):
            return event.id
    raise AssertionError("no requested event")


# --- bounded outbox drain ----------------------------------------------------------

def test_drain_stops_when_target_leaves_requested(session_factories) -> None:
    api_factory, worker_factory, migration_factory = session_factories
    view = _create_requested(migration_factory, provider="fake")
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())
    worker = OutboxWorker(worker_factory, consumer_name="op-drain", handler=gateway.handle_event)

    result = drain_until_terminal(
        worker=worker,
        session_factory=migration_factory,
        execution_id=view.id,
    )

    assert result.target_status == "succeeded"
    assert read_status(migration_factory, view.id) == "succeeded"


def test_drain_consumes_unrelated_older_event_then_stops(session_factories) -> None:
    """An older unrelated pending event is consumed before the target, then the loop
    stops once the target leaves requested (it does not drain future work forever)."""

    api_factory, worker_factory, migration_factory = session_factories
    # Create an older unrelated outbox event first (a task create).
    with migration_factory.begin() as session:
        older_task = Task(id=uuid4(), title="older unrelated task")
        session.add(older_task)
        session.flush()
        older_event = DomainEvent(
            aggregate_type="task",
            aggregate_id=older_task.id,
            event_type="task.created.v1",
            payload={"task_id": str(older_task.id)},
        )
        session.add(older_event)
        session.flush()
        session.add(EventOutbox(event_id=older_event.id, status="pending"))

    view = _create_requested(migration_factory, provider="fake")
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())
    worker = OutboxWorker(worker_factory, consumer_name="op-drain", handler=gateway.handle_event)

    result = drain_until_terminal(
        worker=worker,
        session_factory=migration_factory,
        execution_id=view.id,
    )

    # The older event was consumed, then the target event, then the loop stopped.
    assert result.target_status == "succeeded"
    assert result.consumed_events >= 2
    assert read_status(migration_factory, view.id) == "succeeded"


def test_drain_missing_execution_raises(session_factories) -> None:
    api_factory, worker_factory, migration_factory = session_factories
    gateway = ExecutionGateway(api_factory, FakeExecutionProvider())
    worker = OutboxWorker(worker_factory, consumer_name="op-drain", handler=gateway.handle_event)

    with pytest.raises(OutboxLoopError):
        drain_until_terminal(
            worker=worker,
            session_factory=migration_factory,
            execution_id=uuid4(),
        )


def test_snapshot_bound_is_finite(session_factories) -> None:
    _, _, migration_factory = session_factories
    assert snapshot_drain_bound(migration_factory) >= 0


def test_create_stores_stable_intent_in_proposal_scope(session_factories) -> None:
    """create -> proposal.scope holds the E.4 intent and parse_intent reads it back."""

    _, _, factory = session_factories
    from dxbrain.core.governance import GovernanceService
    from dxbrain.core.tasks import TaskService

    tasks = TaskService(factory)
    governance = GovernanceService(factory)
    scope = _valid_scope()
    task = tasks.create_task(
        title="read the textbook",
        description="read chapter one",
        actor_type="human",
        actor_id="daryl",
        idempotency_key="task-1",
    )
    proposal = governance.create_proposal(
        task_id=task.id,
        objective="read the textbook",
        rationale="read chapter one",
        scope=scope,
        requested_capabilities=["model.call", "agent.execution"],
        plan=[{"title": "t", "capability": "agent.execution", "parameters": {}}],
        actor_type="human",
        actor_id="daryl",
        idempotency_key="proposal-1",
    )

    # Reload from DB and parse: no retyping required, stable stored intent.
    stored = governance.get_proposal(proposal.id)
    intent = parse_intent(stored.scope)
    assert intent.workspace_ref == "default"
    assert intent.provider == "codex"
    assert intent.capability == "agent.execution"
    assert intent.agent_id == UUID(scope["agent_id"])
    assert stored.requested_capabilities == ["model.call", "agent.execution"]


def test_approve_uses_stored_requested_capabilities(session_factories) -> None:
    """approve grants the capabilities recorded at create time (stored, not retyped)."""

    _, _, factory = session_factories
    from datetime import UTC, datetime, timedelta

    from dxbrain.core.governance import GovernanceService
    from dxbrain.core.tasks import TaskService

    tasks = TaskService(factory)
    governance = GovernanceService(factory)
    task = tasks.create_task(
        title="t", description="d", actor_type="human", actor_id="daryl",
        idempotency_key="task-1",
    )
    proposal = governance.create_proposal(
        task_id=task.id,
        objective="t",
        rationale="d",
        scope=_valid_scope(),
        requested_capabilities=["model.call", "agent.execution"],
        plan=[{"title": "t", "capability": "agent.execution", "parameters": {}}],
        actor_type="human",
        actor_id="daryl",
        idempotency_key="proposal-1",
    )
    governance.review_proposal(
        proposal_id=proposal.id,
        decision="approve",
        risk_level="low",
        reason="ok",
        actor_type="human",
        actor_id="daryl",
        idempotency_key="review-1",
    )
    grant = governance.grant_approval(
        proposal_id=proposal.id,
        scope={"capabilities": proposal.requested_capabilities},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        actor_type="human",
        actor_id="daryl",
        idempotency_key="approval-1",
    )

    assert grant.scope == {"capabilities": ["model.call", "agent.execution"]}
