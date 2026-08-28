from __future__ import annotations

import os
import subprocess
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.api.app import create_app
from dxbrain.config import Settings
from dxbrain.core.database import create_session_factory
from dxbrain.core.models import (
    AuditEvent,
    DomainEvent,
    EntityState,
    EventInbox,
    EventOutbox,
    StateTransition,
    Task,
)
from dxbrain.core.tasks import StaleStateVersion, TaskService
from dxbrain.worker.outbox import OutboxWorker


def _service(factory: sessionmaker[Session]) -> TaskService:
    return TaskService(factory)


def test_transaction_rolls_back_every_kernel_artifact(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> None:
    api_factory, _, migration_factory = session_factories
    service = _service(api_factory)
    with pytest.raises(RuntimeError, match="injected rollback"):
        service.create_task(
            title="rollback target",
            description=None,
            actor_type="human",
            actor_id="dx",
            idempotency_key="rollback-1",
            failure_point="after_state",
        )
    with migration_factory() as session:
        for model in (Task, EntityState, StateTransition, DomainEvent, EventOutbox, AuditEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_stale_version_conflict(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> None:
    api_factory, _, _ = session_factories
    service = _service(api_factory)
    task = service.create_task(
        title="versioned task",
        description=None,
        actor_type="human",
        actor_id="dx",
        idempotency_key="version-1",
    )
    moved = service.transition_task_state(
        task_id=task.id, expected_version=1, next_status="active", actor_type="human", actor_id="dx"
    )
    assert moved.version == 2
    with pytest.raises(StaleStateVersion, match="current 2"):
        service.transition_task_state(
            task_id=task.id,
            expected_version=1,
            next_status="closed",
            actor_type="human",
            actor_id="dx",
        )


def test_api_idempotency_and_task_history(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
    database_urls: tuple[str, str, str],
) -> None:
    api_factory, _, _ = session_factories
    settings = Settings(database_url=database_urls[0], root_api_key="test-root-key")
    client = TestClient(create_app(settings))
    headers = {"X-DX-API-Key": "test-root-key", "Idempotency-Key": "repeat-1"}
    first = client.post("/v1/tasks", headers=headers, json={"title": "same request"})
    second = client.post("/v1/tasks", headers=headers, json={"title": "same request"})
    conflict = client.post("/v1/tasks", headers=headers, json={"title": "different request"})
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert conflict.status_code == 409
    detail = client.get(
        f"/v1/tasks/{first.json()['id']}", headers={"X-DX-API-Key": "test-root-key"}
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "created"
    assert detail.json()["history"] == [
        {"version": 1, "previous_status": None, "next_status": "created"}
    ]


def test_control_read_models_are_authenticated_and_read_only(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
    database_urls: tuple[str, str, str],
) -> None:
    api_factory, _, _ = session_factories
    settings = Settings(database_url=database_urls[0], root_api_key="test-root-key")
    client = TestClient(create_app(settings))
    service = _service(api_factory)
    service.create_task(
        title="older control task",
        description=None,
        actor_type="human",
        actor_id="dx",
        idempotency_key="control-1",
    )
    service.create_task(
        title="newer control task",
        description=None,
        actor_type="human",
        actor_id="dx",
        idempotency_key="control-2",
    )

    assert client.get("/v1/control/overview").status_code == 422
    headers = {"X-DX-API-Key": "test-root-key"}
    overview = client.get("/v1/control/overview", headers=headers)
    tasks = client.get("/v1/control/tasks?limit=1", headers=headers)
    assert overview.status_code == 200
    assert overview.json()["task_count"] == 2
    assert overview.json()["event_count"] == 2
    assert overview.json()["audit_count"] == 2
    assert overview.json()["state_counts"] == {"created": 2}
    assert overview.json()["outbox_counts"] == {"pending": 2}
    assert tasks.status_code == 200
    assert len(tasks.json()) == 1
    assert tasks.json()[0]["title"] in {"older control task", "newer control task"}
    preflight = client.options(
        "/v1/control/overview",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-DX-API-Key",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_application_role_cannot_modify_audit(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> None:
    api_factory, _, _ = session_factories
    service = _service(api_factory)
    service.create_task(
        title="audit target",
        description=None,
        actor_type="human",
        actor_id="dx",
        idempotency_key="audit-1",
    )
    with pytest.raises(ProgrammingError):
        with api_factory.begin() as session:
            session.execute(text("UPDATE dx_audit.audit_events SET operation = 'tampered'"))


def test_application_role_is_not_superuser(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> None:
    api_factory, _, _ = session_factories
    with api_factory() as session:
        assert (
            session.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
            is False
        )


def test_outbox_consumer_records_event_once(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> None:
    api_factory, worker_factory, migration_factory = session_factories
    task = _service(api_factory).create_task(
        title="outbox target",
        description=None,
        actor_type="human",
        actor_id="dx",
        idempotency_key="outbox-1",
    )
    worker = OutboxWorker(worker_factory, consumer_name="test-consumer")
    first = worker.consume_one()
    assert first is not None
    assert worker.consume_one() is None
    with migration_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventInbox)) == 1
        assert (
            session.scalar(
                select(EventOutbox.status)
                .join(DomainEvent)
                .where(DomainEvent.aggregate_id == task.id)
            )
            == "published"
        )


def test_process_restart_persistence(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
    database_urls: tuple[str, str, str],
) -> None:
    api_factory, _, _ = session_factories
    original = _service(api_factory).create_task(
        title="restart target",
        description=None,
        actor_type="human",
        actor_id="dx",
        idempotency_key="restart-1",
    )
    restarted_service = TaskService(create_session_factory(database_urls[0]))
    queried, history = restarted_service.get_task(original.id)
    assert queried.id == original.id
    assert queried.status == "created"
    assert len(history) == 1


@pytest.mark.skipif(
    os.getenv("DX_RUN_CONTAINER_RESTART_TEST") != "1",
    reason="set DX_RUN_CONTAINER_RESTART_TEST=1 to restart the Compose database",
)
def test_container_restart_persistence(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
    database_urls: tuple[str, str, str],
) -> None:
    api_factory, _, _ = session_factories
    original = _service(api_factory).create_task(
        title=f"container restart {uuid4()}",
        description=None,
        actor_type="human",
        actor_id="dx",
        idempotency_key="container-restart-1",
    )
    subprocess.run(["docker", "compose", "restart", "postgres"], check=True)
    for _ in range(20):
        try:
            queried, _ = _service(api_factory).get_task(original.id)
            assert queried.id == original.id
            migration_factory = create_session_factory(database_urls[2])
            with migration_factory() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(EntityState)
                        .where(EntityState.entity_id == original.id)
                    )
                    == 1
                )
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(DomainEvent)
                        .where(DomainEvent.aggregate_id == original.id)
                    )
                    == 1
                )
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(AuditEvent)
                        .where(AuditEvent.task_id == original.id)
                    )
                    == 1
                )
            return
        except Exception:  # PostgreSQL is expected to be temporarily unavailable after restart.
            time.sleep(1)
    pytest.fail("task was not queryable after PostgreSQL container restart")
