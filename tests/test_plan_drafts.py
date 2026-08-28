from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from dxbrain.api.app import create_app
from dxbrain.config import Settings
from dxbrain.core.models import (
    AuditEvent,
    DomainEvent,
    PlanDraft,
    PlanDraftRevision,
    WorkflowHandoff,
)
from dxbrain.core.plan_drafts import PlanDraftService, parse_plan_text
from dxbrain.core.workflow import WorkflowService
from dxbrain.providers.adapters import TransportResponse


def test_parse_plan_text_is_deterministic_and_bounded() -> None:
    parsed = parse_plan_text("# Plan\n1. **Inspect state**\n2) Draft `proposal`\n- ignored")
    assert parsed.parser_version == "markdown-list-v1"
    assert len(parsed.steps) == 3
    assert parsed.steps[0]["title"] == "Inspect state"
    assert parsed.steps[1]["title"] == "Draft proposal"
    assert parsed.steps[2]["title"] == "ignored"
    assert parsed.steps[0]["capability"] == "workflow.plan"
    assert parsed.steps[0]["parameters"]["source_line"] == 2
    assert parsed.warnings == []


def test_plan_draft_requires_human_confirmation_and_is_idempotent(
    session_factories, database_urls: tuple[str, str, str], monkeypatch
) -> None:
    _, _, migration_factory = session_factories
    client = TestClient(
        create_app(
            Settings(
                database_url=database_urls[0],
                root_api_key="test-root-key",
                deepseek_api_key="ds-secret",
                llm_live_enabled=True,
                actor_type="system",
            )
        )
    )
    root = {"X-DX-API-Key": "test-root-key"}
    task = client.post(
        "/v1/tasks",
        headers={**root, "Idempotency-Key": "draft-task"},
        json={"title": "plan draft test"},
    )
    task_id = task.json()["id"]
    proposal = client.post(
        f"/v1/tasks/{task_id}/proposals",
        headers={**root, "Idempotency-Key": "draft-proposal"},
        json={
            "objective": "Prepare a safe plan",
            "rationale": "draft parser test",
            "scope": {"side_effects": "none"},
            "requested_capabilities": ["model.call"],
            "plan": [{"title": "Initial", "capability": "model.call", "parameters": {}}],
        },
    )
    proposal_id = proposal.json()["id"]
    assert client.post(
        f"/v1/proposals/{proposal_id}/reviews",
        headers={**root, "Idempotency-Key": "draft-review"},
        json={"decision": "approve", "risk_level": "low", "reason": "test"},
    ).status_code == 201
    assert client.post(
        f"/v1/proposals/{proposal_id}/approvals",
        headers={**root, "Idempotency-Key": "draft-approval"},
        json={
            "scope": {"capabilities": ["model.call"], "side_effects": "none"},
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    ).status_code == 201
    prepared = client.post(
        f"/v1/proposals/{proposal_id}/live-plan/prepare",
        headers={**root, "Idempotency-Key": "draft-prepare"},
    )
    assert prepared.status_code == 201

    class FakeTransport:
        def __call__(self, **_: object) -> TransportResponse:
            return TransportResponse(
                status_code=200,
                payload={
                    "id": "draft-call",
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "1. Inspect state\n2. Record evidence",
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
                },
            )

    monkeypatch.setattr("dxbrain.api.app.HttpxTransport", FakeTransport)
    call_id = prepared.json()["id"]
    assert client.post(
        f"/v1/model-calls/{call_id}/execute",
        headers={**root, "Idempotency-Key": "draft-execute"},
    ).json()["status"] == "succeeded"
    parse_headers = {**root, "Idempotency-Key": "draft-parse"}
    parsed = client.post(f"/v1/model-calls/{call_id}/parse-plan", headers=parse_headers)
    assert parsed.status_code == 201
    assert parsed.json()["status"] == "candidate"
    assert len(parsed.json()["steps"]) == 2
    replay = client.post(f"/v1/model-calls/{call_id}/parse-plan", headers=parse_headers)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    draft_id = parsed.json()["id"]
    non_human = client.post(
        f"/v1/plan-drafts/{draft_id}/review",
        headers={**root, "Idempotency-Key": "draft-human-review"},
        json={"status": "confirmed", "reason": "should be blocked"},
    )
    assert non_human.status_code == 403
    # The configured root actor is not human, so call the service-level edit/review with a human
    # actor after verifying the API correctly blocks non-human confirmation.
    service = PlanDraftService(migration_factory)
    edited = service.edit_draft(
        draft_id=UUID(draft_id),
        steps=[
            {
                "ordinal": 1,
                "title": "Inspect state",
                "capability": "workflow.plan",
                "parameters": {},
            }
        ],
        reason="human removed an ambiguous model step",
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="draft-edit",
    )
    assert edited.revision == 2
    confirmed = service.review_draft(
        draft_id=UUID(draft_id),
        status="confirmed",
        reason="human verified",
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="draft-confirm",
    )
    assert confirmed.status == "confirmed"
    confirmed_replay = service.review_draft(
        draft_id=UUID(draft_id),
        status="confirmed",
        reason="human verified",
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="draft-confirm",
    )
    assert confirmed_replay.replayed is True
    handoff = service.request_workflow_handoff(
        draft_id=UUID(draft_id),
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="draft-handoff",
    )
    assert handoff.status == "requested"
    handoff_replay = service.request_workflow_handoff(
        draft_id=UUID(draft_id),
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="draft-handoff",
    )
    assert handoff_replay.replayed is True
    workflows = WorkflowService(migration_factory)
    run = workflows.accept_handoff(
        handoff_id=handoff.id,
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="workflow-accept",
    )
    assert run.status == "ready"
    assert run.draft_revision == 2
    run_replay = workflows.accept_handoff(
        handoff_id=handoff.id,
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="workflow-accept",
    )
    assert run_replay.replayed is True
    running = workflows.advance_run(
        run_id=run.id,
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="workflow-advance-1",
    )
    assert running.status == "running"
    assert running.steps[0].status == "running"
    completed = workflows.advance_run(
        run_id=run.id,
        actor_type="human",
        actor_id="root-reviewer",
        idempotency_key="workflow-advance-2",
    )
    assert completed.status == "completed"
    assert completed.steps[0].status == "succeeded"
    assert client.get(
        f"/v1/proposals/{proposal_id}/plan-drafts", headers=root
    ).json()[0]["status"] == "confirmed"
    with migration_factory() as session:
        assert session.scalar(select(func.count()).select_from(PlanDraft)) == 1
        assert session.scalar(select(func.count()).select_from(PlanDraftRevision)) == 2
        assert session.scalar(select(func.count()).select_from(WorkflowHandoff)) == 1
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 13
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 13
