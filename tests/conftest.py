from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

# `dxbrain.api.app` exposes a Uvicorn-friendly module-level app. Collection still needs
# placeholder configuration when integration credentials have not been supplied.
os.environ.setdefault(
    "DX_DATABASE_URL", "postgresql+psycopg://unconfigured:unconfigured@127.0.0.1:5432/unconfigured"
)
os.environ.setdefault("DX_ROOT_API_KEY", "unconfigured-test-key")


def _integration_urls() -> tuple[str, str, str] | None:
    api_url = os.getenv("DX_TEST_DATABASE_URL")
    worker_url = os.getenv("DX_TEST_WORKER_DATABASE_URL")
    migration_url = os.getenv("DX_TEST_MIGRATION_DATABASE_URL")
    if not api_url or not worker_url or not migration_url:
        return None
    return api_url, worker_url, migration_url


@pytest.fixture(scope="session")
def database_urls() -> tuple[str, str, str]:
    urls = _integration_urls()
    if urls is None:
        pytest.skip(
            "integration database not configured; set all three DX_TEST_*_DATABASE_URL variables"
        )
    try:
        with create_engine(urls[2]).connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as error:
        pytest.skip(f"integration PostgreSQL unavailable: {error}")
    return urls


@pytest.fixture(scope="session", autouse=True)
def migrate_from_empty_database(database_urls: tuple[str, str, str]) -> None:
    """Exercise Alembic's empty-schema upgrade only on the disposable test database."""

    _, _, migration_url = database_urls
    with create_engine(migration_url).connect() as connection:
        database_name = connection.scalar(text("SELECT current_database()"))
    if database_name != "dxbrain_test":
        pytest.fail("refusing migration test: target database must be named dxbrain_test")

    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    os.environ["DX_DATABASE_URL"] = migration_url
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture()
def session_factories(
    database_urls: tuple[str, str, str],
) -> tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]]:
    api_url, worker_url, migration_url = database_urls
    return (
        sessionmaker(create_engine(api_url), expire_on_commit=False),
        sessionmaker(create_engine(worker_url), expire_on_commit=False),
        sessionmaker(create_engine(migration_url), expire_on_commit=False),
    )


@pytest.fixture(autouse=True)
def clean_database(
    session_factories: tuple[sessionmaker[Session], sessionmaker[Session], sessionmaker[Session]],
) -> Iterator[None]:
    _, _, migration_factory = session_factories
    with migration_factory.begin() as session:
        session.execute(
            text(
                "TRUNCATE dx_archive.lessons, dx_archive.retrospectives, "
                "dx_archive.project_dossiers, dx_workflow.workflow_commands, "
                "dx_workflow.agent_executions, "
                "dx_workflow.workflow_steps, dx_workflow.workflow_runs, "
                "dx_governance.workflow_handoffs, "
                "dx_governance.plan_draft_revisions, dx_governance.plan_drafts, "
                "dx_agent.agent_lifecycle_commands, dx_agent.agents, "
                "dx_model.model_calls, "
                "dx_governance.execution_reports, dx_governance.approval_grants, "
                "dx_governance.review_decisions, dx_governance.intent_proposals, "
                "dx_audit.audit_events, dx_events.event_inbox, dx_events.event_outbox, "
                "dx_state.state_transitions, dx_state.entity_states, dx_events.domain_events, "
                "dx_core.idempotency_requests, dx_core.tasks"
            )
        )
    yield
