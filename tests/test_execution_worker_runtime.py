"""Phase E.3B tests: worker runtime wiring for the real provider (CLI + guard + Gateway).

Two groups, deliberately separated:

1. **wiring** (no database): ``build_execution_handler`` must preserve the absent-
   ``DX_EXECUTION_DATABASE_URL`` behaviour, always inject the pre-dispatch guard, choose
   the Fake provider when the switch is off and the real Codex provider when it is on,
   and fail fast -- rather than fall back -- when real mode is misconfigured. These tests
   never touch PostgreSQL: the guard only stores its session factory, and with the switch
   off it is a pure no-op.

2. **runtime** (integration database): the real Gateway + the real guard + the real
   CodexCLIProvider. Every governance denial must produce ``requested -> failed`` with
   **zero** spawned child, and an allowed execution must drive the full frozen lifecycle.
   ``subprocess.Popen`` is replaced by a factory that spawns a controllable Python child,
   so no Codex prompt is ever run.

Scope discipline: parent rows are seeded locally; no private helper is imported from
another test module.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from dxbrain.config import WorkerSettings
from dxbrain.core.agent_executions import AgentExecutionService
from dxbrain.core.models import (
    Agent,
    AgentExecution,
    ApprovalGrant,
    DomainEvent,
    IntentProposal,
    ModelCall,
    PlanDraft,
    Task,
    WorkflowHandoff,
    WorkflowRun,
    WorkflowStep,
)
from dxbrain.execution import codex_cli
from dxbrain.execution.codex_cli import CodexCLIProvider
from dxbrain.execution.contracts import AGENT_EXECUTION_REQUESTED_EVENT
from dxbrain.execution.fake import FakeExecutionProvider
from dxbrain.execution.gateway import ExecutionGateway
from dxbrain.execution.guard import (
    AGENT_CAPABILITY_NOT_PERMITTED,
    AGENT_IDENTITY_INVALID,
    AGENT_IDENTITY_MISSING,
    APPROVAL_EXPIRED,
    APPROVAL_INACTIVE,
    CAPABILITY_NOT_PERMITTED,
    EXECUTION_DISABLED,
    WORKSPACE_DENIED,
    AgentExecutionGuard,
    build_pre_dispatch_guard,
)
from dxbrain.execution.workspace import WorkspaceResolver
from dxbrain.worker import cli

WORKSPACE_KEY = "ws"
CAPABILITY = "agent.execution"
PROVIDER = "codex"

_CHILD_REPORTS_CWD = (
    "import os, sys; sys.stdin.buffer.read(); "
    "sys.stdout.write('cwd=' + os.getcwd()); sys.stdout.flush()"
)
_CHILD_EXITS_THREE = "import sys; sys.stdin.buffer.read(); sys.exit(3)"


# --------------------------------------------------------------------------------------
# local helpers
# --------------------------------------------------------------------------------------


def _workspace(root: Path) -> Path:
    workspace = root / "ws"
    workspace.mkdir(exist_ok=True)
    return workspace


def _executable(root: Path) -> Path:
    binary_dir = root / "bin"
    binary_dir.mkdir(exist_ok=True)
    binary = binary_dir / "codex.exe"
    binary.write_bytes(b"MZ")
    return binary


def _exploding_session_factory(url: str) -> Any:
    raise AssertionError(f"no database session may be created here: {url}")


def _sentinel_session_factory(url: str) -> Any:
    return object()


def _worker_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    enabled: bool,
    executable: Path | None,
    workspace: Path | None = None,
) -> WorkerSettings:
    monkeypatch.setenv("DX_WORKER_DATABASE_URL", "postgresql+psycopg://worker@127.0.0.1/db")
    monkeypatch.setenv("DX_EXECUTION_DATABASE_URL", "postgresql+psycopg://api@127.0.0.1/db")
    if enabled:
        monkeypatch.setenv("DX_AGENT_EXECUTION_ENABLED", "true")
    else:
        monkeypatch.setenv("DX_AGENT_EXECUTION_ENABLED", "false")
    monkeypatch.setenv(
        "DX_AGENT_WORKSPACES",
        json.dumps({WORKSPACE_KEY: str(workspace if workspace is not None else tmp_path)}),
    )
    if executable is None:
        monkeypatch.delenv("DX_CODEX_EXECUTABLE", raising=False)
    else:
        monkeypatch.setenv("DX_CODEX_EXECUTABLE", str(executable))
    return WorkerSettings()


def _patch_popen(monkeypatch: pytest.MonkeyPatch, script: str, *, must_not_spawn: bool = False):
    observed: dict[str, Any] = {"spawns": 0}
    real_popen = subprocess.Popen

    def _factory(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        observed["spawns"] += 1
        observed["argv"] = list(argv)
        observed["cwd"] = kwargs.get("cwd")
        if must_not_spawn:
            raise AssertionError("the provider must not spawn a child on this path")
        process = real_popen(
            [sys.executable, "-c", script],
            cwd=kwargs.get("cwd"),
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
        )
        observed["process"] = process
        return process

    monkeypatch.setattr(codex_cli.subprocess, "Popen", _factory)
    return observed


# --------------------------------------------------------------------------------------
# local seeding (mirrors the E.2B / E.3A convention)
# --------------------------------------------------------------------------------------


def _seed_parents(
    factory,
    *,
    scope: dict[str, Any],
    status: str = "active",
    expires_in: timedelta | None = None,
) -> dict[str, Any]:
    ids: dict[str, Any] = {
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
        session.add(Task(id=ids["task_id"], title="e3b runtime target"))
        session.flush()
        session.add(
            IntentProposal(
                id=ids["proposal_id"],
                task_id=ids["task_id"],
                objective="o",
                rationale="r",
                scope={"side_effects": "none"},
                requested_capabilities=[CAPABILITY],
                plan=[{"title": "t", "capability": CAPABILITY, "parameters": {}}],
                actor_type="system",
                actor_id="seed",
                idempotency_key=f"seed-proposal-{ids['proposal_id']}",
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
                idempotency_key=f"seed-model-call-{ids['model_call_id']}",
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
                idempotency_key=f"seed-draft-{ids['draft_id']}",
                request_hash="h",
            )
        )
        session.flush()
        session.add(
            ApprovalGrant(
                id=ids["approval_id"],
                proposal_id=ids["proposal_id"],
                task_id=ids["task_id"],
                scope=scope,
                status=status,
                expires_at=datetime.now(UTC)
                + (expires_in if expires_in is not None else timedelta(hours=1)),
                actor_type="system",
                actor_id="seed",
                idempotency_key=f"seed-approval-{ids['approval_id']}",
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
                idempotency_key=f"seed-handoff-{ids['handoff_id']}",
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
                idempotency_key=f"seed-run-{ids['run_id']}",
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
                capability=CAPABILITY,
                parameters={},
            )
        )
        session.flush()
    return ids


def _seed_agent(factory, allowed_capabilities: list[str]) -> UUID:
    """Insert an already-active Agent row directly: seeding must not emit DX events.

    The Agent hosts its own Task row, exactly as the E.3A gateway fixtures do, so the
    agent's identity is independent of the execution's governance chain.
    """

    task_id = uuid4()
    agent_id = uuid4()
    with factory.begin() as session:
        session.add(Task(id=task_id, title="agent host"))
        session.flush()
        session.add(
            Agent(
                id=agent_id,
                task_id=task_id,
                name=f"e3b-agent-{agent_id}",
                version="1",
                role="executor",
                contract={"input_schema": {}, "output_schema": {}, "side_effects": False},
                allowed_capabilities=allowed_capabilities,
                status="active",
                actor_type="system",
                actor_id="seed",
                idempotency_key=f"seed-agent-{agent_id}",
                request_hash="h",
            )
        )
        session.flush()
    return agent_id


def _create_requested(
    factory,
    *,
    workspace_ref: str | None = WORKSPACE_KEY,
    agent_id: UUID | None = None,
    scope: dict[str, Any] | None = None,
    status: str = "active",
    expires_in: timedelta | None = None,
) -> tuple[dict[str, Any], Any, UUID]:
    ids = _seed_parents(
        factory,
        scope=scope if scope is not None else {"capabilities": [CAPABILITY]},
        status=status,
        expires_in=expires_in,
    )
    view = AgentExecutionService(factory).create(
        task_id=ids["task_id"],
        proposal_id=ids["proposal_id"],
        approval_id=ids["approval_id"],
        workflow_run_id=ids["run_id"],
        workflow_step_id=ids["step_id"],
        draft_id=ids["draft_id"],
        draft_revision=1,
        agent_role="executor",
        provider=PROVIDER,
        capability=CAPABILITY,
        request_input={"prompt": "go"},
        actor_type="system",
        actor_id="test",
        idempotency_key=f"exec-{uuid4()}",
        workspace_ref=workspace_ref,
        agent_id=agent_id,
    )
    return ids, view, ids["task_id"]


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


def _event_types(factory) -> set[str]:
    with factory() as session:
        return set(session.scalars(select(DomainEvent.event_type)))


# --------------------------------------------------------------------------------------
# 1. wiring (no database)
# --------------------------------------------------------------------------------------


def _record_gateway(monkeypatch: pytest.MonkeyPatch, session_factory) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, session_factory, provider, guard=None):
            captured["gateway_factory"] = session_factory
            captured["provider"] = provider
            captured["guard"] = guard

        def handle_event(self, event_id: UUID) -> str:
            return "ignored:stub"

    monkeypatch.setattr(cli, "create_session_factory", session_factory)
    monkeypatch.setattr(cli, "ExecutionGateway", _Recorder)
    return captured


def test_switch_off_wires_the_fake_provider_and_an_injected_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _record_gateway(monkeypatch, _sentinel_session_factory)
    settings = _worker_settings(monkeypatch, tmp_path, enabled=False, executable=None)

    handler = cli.build_execution_handler(settings)

    assert callable(handler)
    assert isinstance(captured["provider"], FakeExecutionProvider), (
        "with the switch off the runtime must never hold a real provider"
    )
    assert isinstance(captured["guard"], AgentExecutionGuard)


def test_switch_off_guard_denies_without_touching_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kill-switch proof: with the switch off the guard is a pure no-op.

    The guard is built with a session factory that fails loudly if it is ever used, so a
    denial here proves the switch short-circuits before approval, agent and workspace
    lookups -- i.e. before any database access.
    """

    settings = _worker_settings(monkeypatch, tmp_path, enabled=False, executable=None)
    guard = build_pre_dispatch_guard(settings, _exploding_session_factory)

    decision = guard.check(
        execution_id=uuid4(),
        capability=CAPABILITY,
        agent_id=None,
        approval_id=uuid4(),
        workspace_ref=WORKSPACE_KEY,
    )

    assert decision.allowed is False
    assert decision.error_code == EXECUTION_DISABLED


def test_switch_on_wires_the_real_provider_and_an_injected_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    binary = _executable(tmp_path)
    captured = _record_gateway(monkeypatch, _sentinel_session_factory)
    settings = _worker_settings(
        monkeypatch, tmp_path, enabled=True, executable=binary, workspace=workspace
    )

    cli.build_execution_handler(settings)

    assert isinstance(captured["provider"], CodexCLIProvider)
    assert captured["provider"].name == PROVIDER
    assert isinstance(captured["guard"], AgentExecutionGuard)
    # Construction never spawns anything.
    assert captured["provider"].pending_handles == ()


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("absent", id="absent-executable"),
        pytest.param("missing-file", id="missing-file"),
        pytest.param("cmd-shim", id="npm-cmd-shim"),
        pytest.param("relative", id="relative-path"),
    ],
)
def test_real_mode_fail_fast_and_never_falls_back_to_the_fake_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    workspace = _workspace(tmp_path)
    binary = _executable(tmp_path)
    configured: Path | None = binary
    if case == "absent":
        configured = None
    elif case == "missing-file":
        configured = tmp_path / "absent.exe"
    elif case == "cmd-shim":
        configured = tmp_path / "codex.cmd"
    else:
        configured = Path("codex.exe")

    captured = _record_gateway(monkeypatch, _sentinel_session_factory)
    settings = _worker_settings(
        monkeypatch, tmp_path, enabled=True, executable=configured, workspace=workspace
    )

    with pytest.raises(RuntimeError):
        cli.build_execution_handler(settings)

    # No gateway was built, so no provider -- real or fake -- was ever wired.
    assert captured == {}


def test_execution_url_absent_preserves_the_unwired_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _record_gateway(monkeypatch, _exploding_session_factory)
    _worker_settings(monkeypatch, tmp_path, enabled=True, executable=_executable(tmp_path))
    monkeypatch.delenv("DX_EXECUTION_DATABASE_URL", raising=False)

    assert cli.build_execution_handler(WorkerSettings()) is None
    assert captured == {}


# --------------------------------------------------------------------------------------
# 2. runtime (integration database): the real Gateway + guard + provider
# --------------------------------------------------------------------------------------


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker,
    workspace: Path,
    root: Path,
    *,
    enabled: bool = True,
    script: str = _CHILD_REPORTS_CWD,
    must_not_spawn: bool = False,
    **provider_kwargs: Any,
) -> tuple[ExecutionGateway, CodexCLIProvider, dict[str, Any]]:
    resolver = WorkspaceResolver({WORKSPACE_KEY: str(workspace)})
    guard = AgentExecutionGuard(factory, enabled=enabled, workspace_resolver=resolver)
    provider = CodexCLIProvider(
        executable=str(_executable(root)), workspace_resolver=resolver, **provider_kwargs
    )
    observed = _patch_popen(monkeypatch, script, must_not_spawn=must_not_spawn)
    return ExecutionGateway(factory, provider, guard=guard), provider, observed


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        pytest.param("switch_off", EXECUTION_DISABLED, id="switch-off"),
        pytest.param("invalid_approval", APPROVAL_INACTIVE, id="invalid-approval"),
        pytest.param("expired_approval", APPROVAL_EXPIRED, id="expired-approval"),
        pytest.param("capability_mismatch", CAPABILITY_NOT_PERMITTED, id="capability-mismatch"),
        pytest.param("missing_agent", AGENT_IDENTITY_MISSING, id="missing-agent"),
        pytest.param("invalid_agent", AGENT_IDENTITY_INVALID, id="invalid-agent"),
        pytest.param(
            "permission_denied", AGENT_CAPABILITY_NOT_PERMITTED, id="permission-denied"
        ),
        pytest.param("workspace_denied", WORKSPACE_DENIED, id="workspace-denied"),
    ],
)
def test_gateway_denials_fail_the_execution_with_zero_real_spawn(
    session_factories,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    api_factory, _, migration_factory = session_factories
    workspace = _workspace(tmp_path)

    agent_id: UUID | None = None
    workspace_ref: str | None = WORKSPACE_KEY
    scope: dict[str, Any] | None = None
    status = "active"
    expires_in: timedelta | None = None

    if case == "invalid_approval":
        status = "revoked"
    elif case == "expired_approval":
        expires_in = timedelta(minutes=-5)
    elif case == "capability_mismatch":
        scope = {"capabilities": ["other.capability"]}
    elif case == "invalid_agent":
        agent_id = uuid4()  # a well-formed id that is not registered
    elif case == "permission_denied":
        agent_id = _seed_agent(migration_factory, ["other.capability"])
    elif case == "workspace_denied":
        workspace_ref = "not-configured"
        agent_id = _seed_agent(migration_factory, [CAPABILITY])
    elif case != "missing_agent":
        agent_id = _seed_agent(migration_factory, [CAPABILITY])

    ids, view, _ = _create_requested(
        migration_factory,
        workspace_ref=workspace_ref,
        agent_id=agent_id,
        scope=scope,
        status=status,
        expires_in=expires_in,
    )

    gateway, provider, observed = _runtime(
        monkeypatch,
        api_factory,
        workspace,
        tmp_path,
        enabled=case != "switch_off",
        must_not_spawn=True,
    )

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == f"failed:{expected_code}"
    row = _row(migration_factory, view.id)
    assert row.status == "failed"
    assert row.error_code == expected_code
    assert row.dispatched_at is None
    assert row.started_at is None
    assert provider.pending_handles == ()


def test_gateway_drives_the_full_lifecycle_with_a_real_provider_child(
    session_factories, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api_factory, _, migration_factory = session_factories
    workspace = _workspace(tmp_path)
    ids, view, _ = _create_requested(
        migration_factory, agent_id=_seed_agent(migration_factory, [CAPABILITY])
    )
    gateway, provider, _ = _runtime(monkeypatch, api_factory, workspace, tmp_path)

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == "succeeded"
    row = _row(migration_factory, view.id)
    assert row.status == "succeeded"
    assert row.dispatched_at is not None
    assert row.started_at is not None
    assert row.finished_at is not None
    assert row.exit_code == 0
    assert row.error_code is None
    assert row.external_execution_id == f"codex-{view.id}"
    assert row.result_tail and "cwd=" in row.result_tail
    assert provider.pending_handles == ()
    assert _event_types(migration_factory) == {
        "agent_execution.requested.v1",
        "agent_execution.dispatched.v1",
        "agent_execution.started.v1",
        "agent_execution.succeeded.v1",
    }


def test_gateway_drives_the_failed_lifecycle_with_a_real_provider_child(
    session_factories, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api_factory, _, migration_factory = session_factories
    workspace = _workspace(tmp_path)
    ids, view, _ = _create_requested(
        migration_factory, agent_id=_seed_agent(migration_factory, [CAPABILITY])
    )
    gateway, provider, _ = _runtime(
        monkeypatch, api_factory, workspace, tmp_path, script=_CHILD_EXITS_THREE
    )

    result = gateway.handle_event(_requested_event_id(migration_factory, view.id))

    assert result == "failed:runtime_error"
    row = _row(migration_factory, view.id)
    assert row.status == "failed"
    assert row.error_code == "runtime_error"
    assert row.exit_code == 3
    assert row.dispatched_at is not None
    assert row.started_at is not None
    assert provider.pending_handles == ()
    assert _event_types(migration_factory) == {
        "agent_execution.requested.v1",
        "agent_execution.dispatched.v1",
        "agent_execution.started.v1",
        "agent_execution.failed.v1",
    }
