from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.api.app import create_app
from dxbrain.config import Settings
from dxbrain.core.governance import GovernanceConflict, GovernancePayloadMismatch, GovernanceService
from dxbrain.core.models import AuditEvent, DomainEvent, Task
from dxbrain.core.tasks import TaskService


def _task(factory: sessionmaker[Session]) -> Task:
    created = TaskService(factory).create_task(
        title="governance target",
        description="test target",
        actor_type="human",
        actor_id="dx",
        idempotency_key=f"task-{uuid4()}",
    )
    with factory() as session:
        return session.get(Task, created.id)  # type: ignore[return-value]


def _proposal_payload() -> dict[str, object]:
    return {
        "objective": "Prepare a governed report",
        "rationale": "The plan must be reviewed before execution.",
        "scope": {"workspace": "dx"},
        "requested_capabilities": ["research.read"],
        "plan": [{"title": "Collect evidence", "capability": "research.read", "parameters": {}}],
    }


def test_governance_lifecycle_is_idempotent_and_audited(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> None:
    api_factory, _, migration_factory = session_factories
    task = _task(api_factory)
    governance = GovernanceService(api_factory)
    payload = _proposal_payload()
    proposal = governance.create_proposal(
        task_id=task.id,
        **payload,
        actor_type="human",
        actor_id="dx",
        idempotency_key="proposal-1",
    )
    replay = governance.create_proposal(
        task_id=task.id,
        **payload,
        actor_type="human",
        actor_id="dx",
        idempotency_key="proposal-1",
    )
    assert proposal.status == "proposed"
    assert replay.id == proposal.id
    assert replay.replayed is True
    with pytest.raises(GovernancePayloadMismatch):
        governance.create_proposal(
            task_id=task.id,
            objective="different",
            rationale=payload["rationale"],
            scope=payload["scope"],
            requested_capabilities=payload["requested_capabilities"],
            plan=payload["plan"],
            actor_type="human",
            actor_id="dx",
            idempotency_key="proposal-1",
        )

    review = governance.review_proposal(
        proposal_id=proposal.id,
        decision="approve",
        risk_level="medium",
        reason="Plan is bounded and reviewable.",
        actor_type="human",
        actor_id="dx",
        idempotency_key="review-1",
    )
    approval = governance.grant_approval(
        proposal_id=proposal.id,
        scope={"capability": "research.read"},
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        actor_type="human",
        actor_id="dx",
        idempotency_key="approval-1",
    )
    started = governance.report_execution(
        proposal_id=proposal.id,
        status="started",
        summary="Worker accepted the approved plan.",
        artifacts={},
        actor_type="worker",
        actor_id="research-1",
        idempotency_key="report-started-1",
    )
    completed = governance.report_execution(
        proposal_id=proposal.id,
        status="succeeded",
        summary="Report generated.",
        artifacts={"path": "reports/demo.md"},
        actor_type="worker",
        actor_id="research-1",
        idempotency_key="report-completed-1",
    )
    assert review.proposal_status == "approved"
    assert approval.status == "active"
    with migration_factory.begin() as session:
        session.execute(
            text(
                "UPDATE dx_governance.approval_grants "
                "SET expires_at = now() - interval '1 second' WHERE id = :approval_id"
            ),
            {"approval_id": approval.id},
        )
    assert governance.list_approvals(proposal.id)[0].status == "expired"
    assert started.proposal_status == "executing"
    assert completed.proposal_status == "completed"
    assert governance.get_proposal(proposal.id).status == "completed"
    assert len(governance.list_reviews(proposal.id)) == 1
    assert len(governance.list_approvals(proposal.id)) == 1
    assert len(governance.list_reports(proposal.id)) == 2

    with migration_factory() as session:
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 6
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 6


def test_governance_rejects_illegal_transitions_and_expired_approval(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> None:
    api_factory, _, _ = session_factories
    task = _task(api_factory)
    governance = GovernanceService(api_factory)
    payload = _proposal_payload()
    proposal = governance.create_proposal(
        task_id=task.id,
        **payload,
        actor_type="human",
        actor_id="dx",
        idempotency_key="proposal-reject-1",
    )
    with pytest.raises(GovernanceConflict, match="cannot start execution"):
        governance.report_execution(
            proposal_id=proposal.id,
            status="started",
            summary="not approved",
            artifacts={},
            actor_type="worker",
            actor_id="worker-1",
            idempotency_key="report-illegal-1",
        )
    governance.review_proposal(
        proposal_id=proposal.id,
        decision="reject",
        risk_level="high",
        reason="Missing safety boundary.",
        actor_type="human",
        actor_id="dx",
        idempotency_key="review-reject-1",
    )
    with pytest.raises(GovernanceConflict, match="cannot grant approval"):
        governance.grant_approval(
            proposal_id=proposal.id,
            scope={"capability": "unsafe"},
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            actor_type="human",
            actor_id="dx",
            idempotency_key="approval-rejected-1",
        )


def test_governance_api_lifecycle(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
    database_urls: tuple[str, str, str],
) -> None:
    api_factory, _, _ = session_factories
    settings = Settings(database_url=database_urls[0], root_api_key="test-root-key")
    client = TestClient(create_app(settings))
    headers = {"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-task-1"}
    task = client.post("/v1/tasks", headers=headers, json={"title": "API governance task"})
    assert task.status_code == 201
    task_id = task.json()["id"]
    proposal = client.post(
        f"/v1/tasks/{task_id}/proposals",
        headers={"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-proposal-1"},
        json=_proposal_payload(),
    )
    assert proposal.status_code == 201
    proposal_id = proposal.json()["id"]
    review = client.post(
        f"/v1/proposals/{proposal_id}/reviews",
        headers={"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-review-1"},
        json={"decision": "approve", "risk_level": "low", "reason": "approved for test"},
    )
    assert review.status_code == 201
    approval = client.post(
        f"/v1/proposals/{proposal_id}/approvals",
        headers={"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-approval-1"},
        json={
            "scope": {"capability": "research.read"},
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
    )
    assert approval.status_code == 201
    report = client.post(
        f"/v1/proposals/{proposal_id}/execution-reports",
        headers={"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-report-1"},
        json={"status": "started", "summary": "started", "artifacts": {}},
    )
    assert report.status_code == 201
    assert report.json()["proposal_status"] == "executing"
    assert (
        client.get(
            f"/v1/proposals/{proposal_id}", headers={"X-DX-API-Key": "test-root-key"}
        ).json()["status"]
        == "executing"
    )
