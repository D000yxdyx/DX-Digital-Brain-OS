from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.api.app import create_app
from dxbrain.config import Settings
from dxbrain.core.archive import ArchiveConflict, ArchivePayloadMismatch, ArchiveService
from dxbrain.core.models import AuditEvent, ProjectDossier, Task
from dxbrain.core.tasks import TaskService


def _task(factory: sessionmaker[Session]) -> Task:
    created = TaskService(factory).create_task(
        title="archive target",
        description="test target",
        actor_type="human",
        actor_id="dx",
        idempotency_key=f"task-{uuid4()}",
    )
    with factory() as session:
        return session.get(Task, created.id)  # type: ignore[return-value]


def _retrospective_payload() -> dict[str, object]:
    return {
        "expected_outcome": "A verified archive flow",
        "actual_outcome": "The flow completed with an explicit review",
        "deviations": [{"item": "none", "evidence": "test"}],
        "root_causes": [],
        "keep_items": ["transactional audit"],
        "change_items": ["add retrieval later"],
        "stop_items": [],
        "follow_up_items": ["benchmark memory reuse"],
        "evidence_refs": [{"kind": "test", "ref": "test_archive.py"}],
    }


def test_archive_retro_lesson_lifecycle_is_idempotent_and_audited(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> None:
    api_factory, _, migration_factory = session_factories
    task = _task(api_factory)
    archive = ArchiveService(api_factory)
    dossier = archive.create_dossier(
        task_id=task.id,
        status="closed",
        objective="Preserve a completed project",
        plan_snapshot=[{"title": "run tests"}],
        result_summary="All checks passed",
        artifacts={"report": "reports/archive.md"},
        evidence=[{"kind": "test", "name": "pytest"}],
        actor_type="human",
        actor_id="dx",
        idempotency_key="dossier-1",
    )
    replay = archive.create_dossier(
        task_id=task.id,
        status="closed",
        objective="Preserve a completed project",
        plan_snapshot=[{"title": "run tests"}],
        result_summary="All checks passed",
        artifacts={"report": "reports/archive.md"},
        evidence=[{"kind": "test", "name": "pytest"}],
        actor_type="human",
        actor_id="dx",
        idempotency_key="dossier-1",
    )
    assert replay.replayed is True
    with pytest.raises(ArchivePayloadMismatch):
        archive.create_dossier(
            task_id=task.id,
            status="closed",
            objective="Different objective",
            plan_snapshot=[],
            result_summary=None,
            artifacts={},
            evidence=[],
            actor_type="human",
            actor_id="dx",
            idempotency_key="dossier-1",
        )

    retrospective = archive.create_retrospective(
        dossier_id=dossier.id,
        **_retrospective_payload(),
        actor_type="human",
        actor_id="dx",
        idempotency_key="retro-1",
    )
    lesson = archive.create_lesson(
        retrospective_id=retrospective.id,
        memory_type="procedural",
        statement="Close the dossier before publishing lessons.",
        applicability={"when": "project completes"},
        evidence_refs=[{"kind": "dossier", "id": str(dossier.id)}],
        confidence=0.9,
        actor_type="human",
        actor_id="dx",
        idempotency_key="lesson-1",
    )
    assert lesson.status == "candidate"
    validated = archive.review_lesson(
        lesson_id=lesson.id,
        status="validated",
        reason="Evidence points to the closed dossier and test run.",
        actor_type="human",
        actor_id="dx",
        idempotency_key="lesson-review-1",
    )
    assert validated.status == "validated"
    with migration_factory() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 5
        assert session.get(ProjectDossier, dossier.id).status == "closed"  # type: ignore[union-attr]

    with pytest.raises(ArchiveConflict):
        archive.review_lesson(
            lesson_id=lesson.id,
            status="retracted",
            reason="attempt second review",
            actor_type="human",
            actor_id="dx",
            idempotency_key="lesson-review-2",
        )


def test_archive_api_lifecycle(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
    database_urls: tuple[str, str, str],
) -> None:
    api_factory, _, _ = session_factories
    settings = Settings(database_url=database_urls[0], root_api_key="test-root-key")
    client = TestClient(create_app(settings))
    headers = {"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-task-archive"}
    task = client.post("/v1/tasks", headers=headers, json={"title": "API archive task"})
    assert task.status_code == 201
    task_id = task.json()["id"]
    dossier = client.post(
        f"/v1/tasks/{task_id}/dossier",
        headers={"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-dossier-1"},
        json={
            "objective": "Archive API task",
            "plan_snapshot": [{"title": "verify"}],
            "result_summary": "done",
            "artifacts": {},
            "evidence": [{"kind": "test"}],
        },
    )
    assert dossier.status_code == 201
    dossier_id = dossier.json()["id"]
    retro = client.post(
        f"/v1/dossiers/{dossier_id}/retrospectives",
        headers={"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-retro-1"},
        json=_retrospective_payload(),
    )
    assert retro.status_code == 201
    retro_id = retro.json()["id"]
    lesson = client.post(
        f"/v1/retrospectives/{retro_id}/lessons",
        headers={"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-lesson-1"},
        json={
            "memory_type": "semantic",
            "statement": "Evidence must remain linked.",
            "applicability": {"scope": "archive"},
            "evidence_refs": [{"kind": "test"}],
            "confidence": 0.8,
        },
    )
    assert lesson.status_code == 201
    reviewed = client.post(
        f"/v1/lessons/{lesson.json()['id']}/review",
        headers={"X-DX-API-Key": "test-root-key", "Idempotency-Key": "api-lesson-review-1"},
        json={"status": "validated", "reason": "confirmed by API test"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "validated"
