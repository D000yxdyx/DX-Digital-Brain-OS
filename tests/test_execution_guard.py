"""Phase E.3A tests: the pre-dispatch guard (master switch, approval, agent binding).

Scope discipline: this module exercises the guard directly against a real database and
a real ``AgentService``. It never touches the OutboxWorker, the worker CLI, the E.1
service internals, migrations or the API, and it imports no private helper from another
test module -- parent rows are seeded locally, mirroring the E.2A/E.2B convention.

What must be proven here:

A. the master switch defaults to off, is never silently coerced, and short-circuits
   before any database work;
B. approval is read by the *exact* ``AgentExecution.approval_id``, never by searching
   for a matching grant, and every scope failure fails closed;
C. agent identity follows Owner Option A -- the explicit ``agent_id`` column only, with
   the capability rule delegated to ``AgentService.check_permission``;
D. the guard itself is read-only.

The end-to-end gateway consequences (``requested -> failed``, zero provider interaction,
no ``dispatched`` event) live in ``tests/test_execution_gateway.py``.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from dxbrain.config import Settings
from dxbrain.core.agents import AgentService
from dxbrain.core.models import (
    Agent,
    AgentExecution,
    ApprovalGrant,
    AuditEvent,
    DomainEvent,
    EventOutbox,
    IntentProposal,
    Task,
)
from dxbrain.execution.guard import (
    AGENT_CAPABILITY_NOT_PERMITTED,
    AGENT_IDENTITY_INVALID,
    AGENT_IDENTITY_MISSING,
    APPROVAL_EXPIRED,
    APPROVAL_INACTIVE,
    CAPABILITY_NOT_PERMITTED,
    EXECUTION_DISABLED,
    GOVERNANCE_INTEGRITY_ERROR,
    WORKSPACE_DENIED,
    AgentExecutionGuard,
    GuardDecision,
    build_pre_dispatch_guard,
)
from dxbrain.execution.workspace import WorkspaceResolver

_EXECUTION_DIR = Path(__file__).parents[1] / "src" / "dxbrain" / "execution"

CAPABILITY = "agent.execution"


# --------------------------------------------------------------------------------------
# local seeding helpers
# --------------------------------------------------------------------------------------


def _seed_task(factory) -> UUID:
    task_id = uuid4()
    with factory.begin() as session:
        session.add(Task(id=task_id, title="guard target"))
        session.flush()
    return task_id


def _seed_approval(
    factory,
    task_id: UUID,
    *,
    scope: dict[str, Any],
    status: str = "active",
    expires_in: timedelta = timedelta(hours=1),
) -> UUID:
    proposal_id = uuid4()
    approval_id = uuid4()
    with factory.begin() as session:
        session.add(
            IntentProposal(
                id=proposal_id,
                task_id=task_id,
                objective="o",
                rationale="r",
                scope={"side_effects": "none"},
                requested_capabilities=[CAPABILITY],
                plan=[{"title": "t", "capability": CAPABILITY, "parameters": {}}],
                actor_type="system",
                actor_id="seed",
                idempotency_key=f"seed-proposal-{proposal_id}",
                request_hash="h",
            )
        )
        session.flush()
        session.add(
            ApprovalGrant(
                id=approval_id,
                proposal_id=proposal_id,
                task_id=task_id,
                scope=scope,
                status=status,
                expires_at=datetime.now(UTC) + expires_in,
                actor_type="system",
                actor_id="seed",
                idempotency_key=f"seed-approval-{approval_id}",
                request_hash="h",
            )
        )
        session.flush()
    return approval_id


def _seed_agent(
    factory,
    task_id: UUID,
    *,
    activate: bool,
    allowed_capabilities: list[str],
) -> UUID:
    """Register an Agent through the real service, optionally activating it."""

    service = AgentService(factory)
    view = service.register_agent(
        task_id=task_id,
        name=f"guard-agent-{uuid4()}",
        version="1",
        role="executor",
        contract={"input_schema": {}, "output_schema": {}, "side_effects": False},
        allowed_capabilities=allowed_capabilities,
        actor_type="system",
        actor_id="seed",
        idempotency_key=f"seed-agent-{uuid4()}",
    )
    if activate:
        service.transition(
            agent_id=view.id,
            target_status="active",
            reason="guard test activation",
            actor_type="human",
            actor_id="daryl",
            idempotency_key=f"seed-lifecycle-{uuid4()}",
        )
    return view.id


def _guard(
    factory,
    *,
    enabled: bool = True,
    workspace_dir: Path | None = None,
    agent_service: AgentService | None = None,
) -> AgentExecutionGuard:
    mapping = {"default": str(workspace_dir)} if workspace_dir is not None else {}
    return AgentExecutionGuard(
        factory,
        enabled=enabled,
        workspace_resolver=WorkspaceResolver(mapping),
        agent_service=agent_service,
    )


def _check(guard: AgentExecutionGuard, **overrides: Any) -> GuardDecision:
    kwargs: dict[str, Any] = {
        "execution_id": uuid4(),
        "capability": CAPABILITY,
        "agent_id": None,
        "approval_id": uuid4(),
        "workspace_ref": "default",
    }
    kwargs.update(overrides)
    return guard.check(**kwargs)


def _counts(factory) -> dict[str, int]:
    with factory() as session:
        return {
            "domain": session.scalar(select(func.count()).select_from(DomainEvent)),
            "audit": session.scalar(select(func.count()).select_from(AuditEvent)),
            "outbox": session.scalar(select(func.count()).select_from(EventOutbox)),
        }


def _assert_denied(decision: GuardDecision, error_code: str) -> None:
    assert decision.allowed is False
    assert decision.error_code == error_code
    assert decision.error_detail


# --------------------------------------------------------------------------------------
# A. master switch
# --------------------------------------------------------------------------------------


def test_master_switch_defaults_to_false_when_env_absent(monkeypatch) -> None:
    monkeypatch.delenv("DX_AGENT_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("DX_AGENT_WORKSPACES", raising=False)

    settings = Settings()

    assert settings.agent_execution_enabled is False
    assert settings.agent_workspaces == {}


def test_master_switch_reads_explicit_booleans(monkeypatch) -> None:
    monkeypatch.setenv("DX_AGENT_EXECUTION_ENABLED", "true")
    assert Settings().agent_execution_enabled is True

    monkeypatch.setenv("DX_AGENT_EXECUTION_ENABLED", "false")
    assert Settings().agent_execution_enabled is False


def test_master_switch_rejects_a_malformed_boolean(monkeypatch) -> None:
    """A malformed value must be a configuration error, never a silent False."""

    monkeypatch.setenv("DX_AGENT_EXECUTION_ENABLED", "not-a-bool")

    with pytest.raises(ValidationError):
        Settings()


def test_master_switch_denies_without_touching_the_database(session_factories) -> None:
    """With the switch off the guard short-circuits: no approval, no agent, no path."""

    api_factory, _, _ = session_factories
    guard = _guard(api_factory, enabled=False)

    decision = _check(guard)

    _assert_denied(decision, EXECUTION_DISABLED)


def test_disabled_guard_is_computed_from_settings(session_factories, monkeypatch) -> None:
    """The composed guard honours the master switch, so config actually drives it."""

    api_factory, _, _ = session_factories
    monkeypatch.delenv("DX_AGENT_EXECUTION_ENABLED", raising=False)

    guard = build_pre_dispatch_guard(Settings(), api_factory)

    _assert_denied(_check(guard), EXECUTION_DISABLED)


# --------------------------------------------------------------------------------------
# B. approval
# --------------------------------------------------------------------------------------


def test_valid_active_unexpired_grant_allows_continuation(
    session_factories, tmp_path: Path
) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])
    guard = _guard(api_factory, workspace_dir=tmp_path)

    decision = _check(guard, approval_id=approval_id, agent_id=agent_id)

    assert decision.allowed is True
    assert decision.error_code is None
    assert decision.error_detail is None


def test_capability_list_may_contain_extra_entries(session_factories, tmp_path: Path) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(
        api_factory, task_id, scope={"capabilities": ["other.read", CAPABILITY]}
    )
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    assert decision.allowed is True


def test_missing_grant_row_is_a_governance_integrity_error(
    session_factories, tmp_path: Path
) -> None:
    """A dangling approval_id is a destroyed FK invariant, not an ordinary refusal."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=uuid4(), agent_id=agent_id
    )

    _assert_denied(decision, GOVERNANCE_INTEGRITY_ERROR)


def test_revoked_grant_is_inactive(session_factories, tmp_path: Path) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(
        api_factory, task_id, scope={"capabilities": [CAPABILITY]}, status="revoked"
    )
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    _assert_denied(decision, APPROVAL_INACTIVE)


def test_expired_grant_is_expired(session_factories, tmp_path: Path) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(
        api_factory,
        task_id,
        scope={"capabilities": [CAPABILITY]},
        expires_in=timedelta(minutes=-5),
    )
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    _assert_denied(decision, APPROVAL_EXPIRED)


def test_capability_absent_from_scope_is_denied(session_factories, tmp_path: Path) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(
        api_factory, task_id, scope={"capabilities": ["other.write"]}
    )
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    _assert_denied(decision, CAPABILITY_NOT_PERMITTED)


def test_historical_singular_scope_shape_is_not_supported(
    session_factories, tmp_path: Path
) -> None:
    """``{"capability": ...}`` is a historical shape, not an alternative to support."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(
        api_factory, task_id, scope={"capability": CAPABILITY}
    )
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    _assert_denied(decision, CAPABILITY_NOT_PERMITTED)


def test_empty_scope_is_denied(session_factories, tmp_path: Path) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"side_effects": "none"})
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    _assert_denied(decision, CAPABILITY_NOT_PERMITTED)


@pytest.mark.parametrize(
    "capabilities",
    [
        pytest.param(CAPABILITY, id="str"),
        pytest.param({"name": CAPABILITY}, id="dict"),
        pytest.param(7, id="int"),
        pytest.param(None, id="null"),
        pytest.param(True, id="bool"),
    ],
)
def test_malformed_capabilities_type_fails_closed(
    session_factories, tmp_path: Path, capabilities: Any
) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(
        api_factory, task_id, scope={"capabilities": capabilities}
    )
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    _assert_denied(decision, CAPABILITY_NOT_PERMITTED)


def test_approval_is_read_by_exact_id_and_never_searched(
    session_factories, tmp_path: Path
) -> None:
    """An active grant for the same proposal must not rescue a revoked exact grant."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    revoked_id = _seed_approval(
        api_factory, task_id, scope={"capabilities": [CAPABILITY]}, status="revoked"
    )
    active_id = _seed_approval(
        api_factory, task_id, scope={"capabilities": [CAPABILITY]}, status="active"
    )
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])
    guard = _guard(api_factory, workspace_dir=tmp_path)

    _assert_denied(
        _check(guard, approval_id=revoked_id, agent_id=agent_id), APPROVAL_INACTIVE
    )
    # The other grant really is usable -- so the denial above came from identity, not
    # from the approval table being empty.
    assert _check(guard, approval_id=active_id, agent_id=agent_id).allowed is True


# --------------------------------------------------------------------------------------
# C. agent identity and capability (Owner Option A)
# --------------------------------------------------------------------------------------


def test_missing_agent_identity_is_denied(session_factories, tmp_path: Path) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=None
    )

    _assert_denied(decision, AGENT_IDENTITY_MISSING)


def test_unknown_agent_identity_is_denied(session_factories, tmp_path: Path) -> None:
    """``AgentService.check_permission`` raises LookupError -> agent_identity_invalid."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=uuid4()
    )

    _assert_denied(decision, AGENT_IDENTITY_INVALID)


def test_inactive_agent_is_denied(session_factories, tmp_path: Path) -> None:
    """Registered but not active: the decision must come from AgentService itself."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})
    agent_id = _seed_agent(
        api_factory, task_id, activate=False, allowed_capabilities=[CAPABILITY]
    )

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    _assert_denied(decision, AGENT_CAPABILITY_NOT_PERMITTED)
    with api_factory() as session:
        assert session.get(Agent, agent_id).status == "registered"


def test_active_agent_without_the_capability_is_denied(
    session_factories, tmp_path: Path
) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})
    agent_id = _seed_agent(
        api_factory, task_id, activate=True, allowed_capabilities=["other.capability"]
    )

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    _assert_denied(decision, AGENT_CAPABILITY_NOT_PERMITTED)


def test_active_agent_with_the_capability_continues(session_factories, tmp_path: Path) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    assert decision.allowed is True


def test_guard_delegates_the_capability_rule_to_agent_service(
    session_factories, tmp_path: Path, monkeypatch
) -> None:
    """Proof of reuse: the production path really calls AgentService.check_permission."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    calls: list[tuple[UUID, str]] = []
    real = AgentService.check_permission

    def _spy(self, *, agent_id: UUID, capability: str):  # type: ignore[no-untyped-def]
        calls.append((agent_id, capability))
        return real(self, agent_id=agent_id, capability=capability)

    monkeypatch.setattr(AgentService, "check_permission", _spy)

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    assert decision.allowed is True
    assert calls == [(agent_id, CAPABILITY)]


def test_guard_reuses_the_service_it_was_given(session_factories, tmp_path: Path) -> None:
    """An explicitly supplied AgentService is the one that decides."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    observed: list[str] = []
    service = AgentService(api_factory)
    real = service.check_permission

    def _spy(*, agent_id: UUID, capability: str):  # type: ignore[no-untyped-def]
        observed.append(capability)
        return real(agent_id=agent_id, capability=capability)

    service.check_permission = _spy  # type: ignore[method-assign]
    guard = _guard(api_factory, workspace_dir=tmp_path, agent_service=service)

    decision = _check(guard, approval_id=approval_id, agent_id=agent_id)

    assert decision.allowed is True
    assert observed == [CAPABILITY]


# --------------------------------------------------------------------------------------
# D. workspace, as seen through the guard
# --------------------------------------------------------------------------------------


def test_workspace_denial_is_reported_as_workspace_denied(
    session_factories, tmp_path: Path
) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path),
        approval_id=approval_id,
        agent_id=agent_id,
        workspace_ref="not-configured",
    )

    _assert_denied(decision, WORKSPACE_DENIED)


def test_workspace_is_checked_last(session_factories, tmp_path: Path) -> None:
    """An earlier failure wins, so a bad workspace never masks a governance problem."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(
        api_factory, task_id, scope={"capabilities": [CAPABILITY]}, status="revoked"
    )

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path),
        approval_id=approval_id,
        agent_id=None,
        workspace_ref="not-configured",
    )

    _assert_denied(decision, APPROVAL_INACTIVE)


# --------------------------------------------------------------------------------------
# E. the guard is read-only
# --------------------------------------------------------------------------------------


def test_guard_writes_nothing_on_any_path(session_factories, tmp_path: Path) -> None:
    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    good_approval = _seed_approval(
        api_factory, task_id, scope={"capabilities": [CAPABILITY]}
    )
    bad_approval = _seed_approval(
        api_factory, task_id, scope={"capabilities": [CAPABILITY]}, status="revoked"
    )
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])
    guard = _guard(api_factory, workspace_dir=tmp_path)
    before = _counts(api_factory)

    assert _check(guard, approval_id=good_approval, agent_id=agent_id).allowed is True
    _assert_denied(_check(guard, approval_id=bad_approval, agent_id=agent_id), APPROVAL_INACTIVE)
    _assert_denied(_check(guard, approval_id=good_approval, agent_id=None), AGENT_IDENTITY_MISSING)
    _assert_denied(
        _check(guard, approval_id=good_approval, agent_id=agent_id, workspace_ref="nope"),
        WORKSPACE_DENIED,
    )
    _assert_denied(_check(_guard(api_factory, enabled=False)), EXECUTION_DISABLED)

    assert _counts(api_factory) == before


def _guard_executable_source() -> str:
    """The guard's code with every docstring removed.

    The static guard-rails below are about what the guard *does*, so they must not be
    tripped by the module's own prose -- which legitimately names the very things
    (``subprocess``, ``allowed_capabilities``) that the code must not contain.
    """

    tree = ast.parse((_EXECUTION_DIR / "guard.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = body[1:]
    return ast.unparse(tree)


def test_guard_module_has_no_network_or_subprocess_surface() -> None:
    """Static guard-rail: the guard is read-only, offline, and spawns nothing."""

    source = _guard_executable_source()

    for forbidden in ("subprocess", "socket", "requests", "httpx", "urllib", "os.system"):
        assert forbidden not in source


def test_guard_does_not_reimplement_the_agent_permission_rule() -> None:
    """The guard must consult the Agent, not the contract's capability list."""

    source = _guard_executable_source()

    assert "AgentService" in source
    assert "allowed_capabilities" not in source


def test_guard_never_reads_a_second_agent_execution_row(session_factories, tmp_path: Path) -> None:
    """The guard consumes the Gateway's snapshot; it does not re-query the execution."""

    api_factory, _, _ = session_factories
    task_id = _seed_task(api_factory)
    approval_id = _seed_approval(api_factory, task_id, scope={"capabilities": [CAPABILITY]})
    agent_id = _seed_agent(api_factory, task_id, activate=True, allowed_capabilities=[CAPABILITY])

    # No AgentExecution row exists at all, yet the guard still returns a decision --
    # which it can only do from the arguments the Gateway handed it.
    with api_factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution)) == 0

    decision = _check(
        _guard(api_factory, workspace_dir=tmp_path), approval_id=approval_id, agent_id=agent_id
    )

    assert decision.allowed is True
