from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from dxbrain.api.app import create_app
from dxbrain.config import Settings
from dxbrain.core.models import AuditEvent, DomainEvent, ModelCall
from dxbrain.providers.adapters import TransportResponse


def test_dry_run_planner_is_idempotent_and_audited(
    session_factories, database_urls: tuple[str, str, str]
) -> None:
    _, _, migration_factory = session_factories
    client = TestClient(
        create_app(
            Settings(
                database_url=database_urls[0],
                root_api_key="test-root-key",
                deepseek_api_key=None,
            )
        )
    )
    headers = {"X-DX-API-Key": "test-root-key", "Idempotency-Key": "model-task-1"}
    task = client.post("/v1/tasks", headers=headers, json={"title": "model dry-run target"})
    assert task.status_code == 201
    task_id = task.json()["id"]
    proposal = client.post(
        f"/v1/tasks/{task_id}/proposals",
        headers={**headers, "Idempotency-Key": "model-proposal-1"},
        json={
            "objective": "Produce a safe plan",
            "rationale": "Contract test only",
            "scope": {"side_effects": "none"},
            "requested_capabilities": ["kernel.read"],
            "plan": [
                {
                    "title": "Inspect state",
                    "capability": "kernel.read",
                    "parameters": {"local": True},
                }
            ],
        },
    )
    assert proposal.status_code == 201
    proposal_id = proposal.json()["id"]

    readiness = client.get(
        f"/v1/proposals/{proposal_id}/model-readiness",
        headers={"X-DX-API-Key": "test-root-key"},
    )
    assert readiness.status_code == 200
    assert readiness.json()["live_enabled"] is False
    assert readiness.json()["allowed"] is False
    assert "live model calls are disabled by configuration" in readiness.json()["reasons"]
    assert "planner provider has no configured API key" in readiness.json()["reasons"]
    assert "proposal has no active approval grant" in readiness.json()["reasons"]

    dry_run_headers = {**headers, "Idempotency-Key": "model-dry-run-1"}
    first = client.post(
        f"/v1/proposals/{proposal_id}/dry-run-plan",
        headers=dry_run_headers,
    )
    replay = client.post(
        f"/v1/proposals/{proposal_id}/dry-run-plan",
        headers=dry_run_headers,
    )
    assert first.status_code == 201
    assert first.json()["status"] == "dry_run"
    assert first.json()["mode"] == "dry_run"
    assert first.json()["provider"] == "deepseek"
    assert first.json()["total_tokens"] == 0
    assert first.json()["response_summary"]["plan"] == proposal.json()["plan"]
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == first.json()["id"]

    calls = client.get(
        f"/v1/proposals/{proposal_id}/model-calls",
        headers={"X-DX-API-Key": "test-root-key"},
    )
    assert calls.status_code == 200
    assert len(calls.json()) == 1

    with migration_factory() as session:
        assert session.scalar(select(func.count()).select_from(ModelCall)) == 1
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 3
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 3


def test_live_call_ledger_requires_gate_and_supports_finalize(
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
            )
        )
    )
    root = {"X-DX-API-Key": "test-root-key"}
    task = client.post(
        "/v1/tasks",
        headers={**root, "Idempotency-Key": "two-phase-task"},
        json={"title": "two-phase model call"},
    )
    task_id = task.json()["id"]
    proposal = client.post(
        f"/v1/tasks/{task_id}/proposals",
        headers={**root, "Idempotency-Key": "two-phase-proposal"},
        json={
            "objective": "Prepare a reviewed plan",
            "rationale": "Two-phase ledger contract",
            "scope": {"side_effects": "none"},
            "requested_capabilities": ["model.call"],
            "plan": [
                {"title": "Plan", "capability": "model.call", "parameters": {}}
            ],
        },
    )
    proposal_id = proposal.json()["id"]
    assert client.post(
        f"/v1/proposals/{proposal_id}/reviews",
        headers={**root, "Idempotency-Key": "two-phase-review"},
        json={"decision": "approve", "risk_level": "low", "reason": "sandbox contract"},
    ).status_code == 201
    assert client.post(
        f"/v1/proposals/{proposal_id}/approvals",
        headers={**root, "Idempotency-Key": "two-phase-approval"},
        json={
            "scope": {"capabilities": ["model.call"], "side_effects": "none"},
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    ).status_code == 201

    prepare_headers = {**root, "Idempotency-Key": "two-phase-prepare"}
    prepared = client.post(
        f"/v1/proposals/{proposal_id}/live-plan/prepare", headers=prepare_headers
    )
    assert prepared.status_code == 201
    assert prepared.json()["status"] == "prepared"
    call_id = prepared.json()["id"]
    replay = client.post(
        f"/v1/proposals/{proposal_id}/live-plan/prepare", headers=prepare_headers
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    class FakeTransport:
        def __call__(self, **_: object) -> TransportResponse:
            return TransportResponse(
                status_code=200,
                payload={
                    "id": "ds-live-1",
                    "model": "deepseek-chat",
                    "choices": [{"message": {"role": "assistant", "content": "plan"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                },
            )

    monkeypatch.setattr("dxbrain.api.app.HttpxTransport", FakeTransport)
    executed = client.post(
        f"/v1/model-calls/{call_id}/execute",
        headers={**root, "Idempotency-Key": "two-phase-execute"},
    )
    assert executed.status_code == 200
    assert executed.json()["status"] == "succeeded"
    assert executed.json()["total_tokens"] == 14
    executed_replay = client.post(
        f"/v1/model-calls/{call_id}/execute",
        headers={**root, "Idempotency-Key": "two-phase-execute"},
    )
    assert executed_replay.status_code == 200
    assert executed_replay.json()["replayed"] is True

    prepared_direct = client.post(
        f"/v1/proposals/{proposal_id}/live-plan/prepare",
        headers={**root, "Idempotency-Key": "two-phase-prepare-direct"},
    )
    assert prepared_direct.status_code == 201
    direct_call_id = prepared_direct.json()["id"]

    finalize_headers = {**root, "Idempotency-Key": "two-phase-finalize"}
    finalized = client.post(
        f"/v1/model-calls/{direct_call_id}/finalize",
        headers=finalize_headers,
        json={
            "status": "succeeded",
            "response_summary": {"text_preview": "safe plan", "text_sha256": "abc"},
            "input_tokens": 10,
            "output_tokens": 6,
            "total_tokens": 16,
            "cost_usd": 0.001,
            "latency_ms": 250,
        },
    )
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "succeeded"
    finalized_replay = client.post(
        f"/v1/model-calls/{direct_call_id}/finalize",
        headers=finalize_headers,
        json={
            "status": "succeeded",
            "response_summary": {"text_preview": "safe plan", "text_sha256": "abc"},
            "input_tokens": 10,
            "output_tokens": 6,
            "total_tokens": 16,
            "cost_usd": 0.001,
            "latency_ms": 250,
        },
    )
    assert finalized_replay.status_code == 200
    assert finalized_replay.json()["replayed"] is True
    conflict = client.post(
        f"/v1/model-calls/{direct_call_id}/finalize",
        headers={**root, "Idempotency-Key": "two-phase-finalize-again"},
        json={"status": "failed", "error_code": "late"},
    )
    assert conflict.status_code == 409

    with migration_factory() as session:
        row = session.get(ModelCall, direct_call_id)
        assert row is not None
        assert row.status == "succeeded"
        assert row.started_at is not None
        assert row.finished_at is not None
        assert session.scalar(select(func.count()).select_from(ModelCall)) == 2
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 8
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 8
