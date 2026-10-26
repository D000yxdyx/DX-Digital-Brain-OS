"""Phase E.4 tests: ExecutionReviewService (the human verdict on an execution).

Scope discipline: this module tests the review service only. It seeds a terminal
(``succeeded``) AgentExecution and its parents through a local ORM helper; it does not
import private helpers from other test modules, does not touch the Gateway, and does not
route the review event.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from dxbrain.core.execution_reviews import (
    ExecutionReviewConflict,
    ExecutionReviewService,
)
from dxbrain.core.models import (
    AgentExecution,
    ApprovalGrant,
    AuditEvent,
    DomainEvent,
    EventOutbox,
    ExecutionReview,
    IntentProposal,
    ModelCall,
    PlanDraft,
    Task,
    WorkflowHandoff,
    WorkflowRun,
    WorkflowStep,
)


def _seed_execution(factory, status: str) -> UUID:
    ids = {
        "task_id": uuid4(),
        "proposal_id": uuid4(),
        "model_call_id": uuid4(),
        "draft_id": uuid4(),
        "approval_id": uuid4(),
        "handoff_id": uuid4(),
        "run_id": uuid4(),
        "step_id": uuid4(),
        "execution_id": uuid4(),
    }
    with factory.begin() as session:
        session.add(Task(id=ids["task_id"], title="review target"))
        session.flush()
        session.add(
            IntentProposal(
                id=ids["proposal_id"],
                task_id=ids["task_id"],
                objective="o",
                rationale="r",
                scope={},
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
                scope={"capabilities": ["agent.execution"]},
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
        session.add(
            AgentExecution(
                id=ids["execution_id"],
                task_id=ids["task_id"],
                proposal_id=ids["proposal_id"],
                approval_id=ids["approval_id"],
                workflow_run_id=ids["run_id"],
                workflow_step_id=ids["step_id"],
                draft_id=ids["draft_id"],
                draft_revision=1,
                agent_role="executor",
                provider="fake",
                capability="agent.execution",
                workspace_ref="default",
                status=status,
                request_input={"prompt": "go"},
                idempotency_key="seed-execution",
                request_hash="h",
                actor_type="system",
                actor_id="seed",
            )
        )
        session.flush()
    return ids["execution_id"]


def _execution_status(factory, execution_id: UUID) -> str:
    with factory() as session:
        return session.get(AgentExecution, execution_id).status


def _review_rows(factory, execution_id: UUID) -> list[ExecutionReview]:
    with factory() as session:
        return list(
            session.scalars(
                select(ExecutionReview).where(
                    ExecutionReview.agent_execution_id == execution_id
                )
            )
        )


def test_accept_review_on_succeeded_execution(session_factories) -> None:
    _, _, factory = session_factories
    eid = _seed_execution(factory, "succeeded")
    service = ExecutionReviewService(factory)

    review = service.record_decision(
        agent_execution_id=eid,
        decision="accepted",
        reason="looks good",
        actor_type="human",
        actor_id="test-owner",
        idempotency_key="review-1",
    )

    assert review.decision == "accepted"
    assert _execution_status(factory, eid) == "succeeded"


def test_reject_review_on_succeeded_execution_keeps_succeeded(session_factories) -> None:
    _, _, factory = session_factories
    eid = _seed_execution(factory, "succeeded")
    service = ExecutionReviewService(factory)

    review = service.record_decision(
        agent_execution_id=eid,
        decision="rejected",
        reason="wrong result",
        actor_type="human",
        actor_id="test-owner",
        idempotency_key="review-1",
    )

    assert review.decision == "rejected"
    # The technical outcome is untouched: succeeded + rejected is valid and preserved.
    assert _execution_status(factory, eid) == "succeeded"


def test_non_human_actor_rejected(session_factories) -> None:
    _, _, factory = session_factories
    eid = _seed_execution(factory, "succeeded")
    service = ExecutionReviewService(factory)

    with pytest.raises(ExecutionReviewConflict):
        service.record_decision(
            agent_execution_id=eid,
            decision="accepted",
            reason="x",
            actor_type="system",
            actor_id="gateway",
            idempotency_key="review-1",
        )

    assert _review_rows(factory, eid) == []


@pytest.mark.parametrize("status", ["requested", "dispatched", "running", "failed"])
def test_review_of_non_succeeded_execution_rejected(session_factories, status: str) -> None:
    _, _, factory = session_factories
    eid = _seed_execution(factory, status)
    service = ExecutionReviewService(factory)

    with pytest.raises(ExecutionReviewConflict):
        service.record_decision(
            agent_execution_id=eid,
            decision="accepted",
            reason="x",
            actor_type="human",
            actor_id="test-owner",
            idempotency_key="review-1",
        )


def test_invalid_decision_rejected(session_factories) -> None:
    _, _, factory = session_factories
    eid = _seed_execution(factory, "succeeded")
    service = ExecutionReviewService(factory)

    with pytest.raises(ValueError):
        service.record_decision(
            agent_execution_id=eid,
            decision="needs_changes",  # type: ignore[arg-type]
            reason="x",
            actor_type="human",
            actor_id="test-owner",
            idempotency_key="review-1",
        )


def test_duplicate_identical_review_replays(session_factories) -> None:
    _, _, factory = session_factories
    eid = _seed_execution(factory, "succeeded")
    service = ExecutionReviewService(factory)

    first = service.record_decision(
        agent_execution_id=eid,
        decision="accepted",
        reason="ok",
        actor_type="human",
        actor_id="test-owner",
        idempotency_key="review-1",
    )
    second = service.record_decision(
        agent_execution_id=eid,
        decision="accepted",
        reason="ok",
        actor_type="human",
        actor_id="test-owner",
        idempotency_key="review-1",
    )

    assert second.replayed is True
    assert second.id == first.id
    assert len(_review_rows(factory, eid)) == 1


def test_conflicting_second_review_fails_closed(session_factories) -> None:
    _, _, factory = session_factories
    eid = _seed_execution(factory, "succeeded")
    service = ExecutionReviewService(factory)

    service.record_decision(
        agent_execution_id=eid,
        decision="accepted",
        reason="ok",
        actor_type="human",
        actor_id="test-owner",
        idempotency_key="review-1",
    )

    with pytest.raises(ExecutionReviewConflict):
        service.record_decision(
            agent_execution_id=eid,
            decision="rejected",
            reason="changed my mind",
            actor_type="human",
            actor_id="test-owner",
            idempotency_key="review-2",
        )

    # Exactly one final review remains.
    assert len(_review_rows(factory, eid)) == 1
    assert _review_rows(factory, eid)[0].decision == "accepted"


def test_review_emits_audit_and_outbox(session_factories) -> None:
    _, _, factory = session_factories
    eid = _seed_execution(factory, "succeeded")
    service = ExecutionReviewService(factory)

    service.record_decision(
        agent_execution_id=eid,
        decision="accepted",
        reason="ok",
        actor_type="human",
        actor_id="test-owner",
        idempotency_key="review-1",
    )

    with factory() as session:
        audit = session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.operation == "execution.review"
            )
        )
        event = session.scalar(
            select(func.count())
            .select_from(DomainEvent)
            .where(DomainEvent.event_type == "governance.execution.reviewed.v1")
        )
        outbox = session.scalar(
            select(func.count()).select_from(EventOutbox).where(EventOutbox.status == "pending")
        )
    assert audit == 1
    assert event == 1
    assert outbox >= 1


def test_missing_execution_rejected(session_factories) -> None:
    _, _, factory = session_factories
    service = ExecutionReviewService(factory)

    with pytest.raises(LookupError):
        service.record_decision(
            agent_execution_id=uuid4(),
            decision="accepted",
            reason="x",
            actor_type="human",
            actor_id="test-owner",
            idempotency_key="review-1",
        )
