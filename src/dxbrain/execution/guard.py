"""Pre-dispatch safety guard for the Agent Execution Bridge (V0.2, Phase E.3A).

The guard answers exactly one question: *may this execution be handed to a
provider at all?* It is the last DX-side authority check before ``provider.dispatch``
and it is deliberately the narrowest possible component:

* **read-only** -- two ``SELECT``-only lookups (the exact ``ApprovalGrant`` row and
  ``AgentService.check_permission``) plus pure filesystem stat calls;
* **no network, no subprocess, no writes, no new tables, no migrations**;
* **no session left open when dispatch begins** -- every session is closed before the
  guard returns, and the Gateway owns every lifecycle write.

Frozen E.3A guard order (evaluated top to bottom, first failure wins):

1. ``DX_AGENT_EXECUTION_ENABLED``  -> ``execution_disabled``
2. exact ``AgentExecution.approval_id`` lookup -> ``governance_integrity_error``
3. ``grant.status == "active"``   -> ``approval_inactive``
4. ``grant.expires_at > now``     -> ``approval_expired``
5. ``execution.capability`` in ``grant.scope["capabilities"]`` -> ``capability_not_permitted``
6. ``execution.agent_id`` present -> ``agent_identity_missing``
7. ``AgentService.check_permission`` -> ``agent_identity_invalid`` /
   ``agent_capability_not_permitted``
8. ``WorkspaceResolver.resolve``  -> ``workspace_denied``

Owner decision for E.3A is **Option A, Agent binding**: identity is the explicit
``execution.agent_id`` column only. There is intentionally no fallback to
``agent_role``, no lookup by role, no second ``allowed_capabilities`` parser, and no
re-implementation of ``AgentService.check_permission`` -- the guard *calls* that
service so there is exactly one permission implementation in the codebase.

Approval semantics are equally literal. The guard reads the *exact* grant named by
``AgentExecution.approval_id``; it never searches for "any matching grant" and never
matches by role, principal, proposal, or capability. The only supported
``ApprovalGrant.scope`` shape is the existing production shape
``{"capabilities": [...]}``. Historical shapes such as ``{"capability": "..."}`` are
**not** supported alternatives, historical fixtures are not modified, and unknown or
malformed scope fails closed into ``capability_not_permitted``.

A missing grant row is a **defensive invariant failure** (``AgentExecution.approval_id``
is a hard FK), not the ordinary "the human did not approve" path, so it is classified
separately as ``governance_integrity_error``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from dxbrain.config import Settings, WorkerSettings
from dxbrain.core.agents import AgentService
from dxbrain.core.models import ApprovalGrant
from dxbrain.execution.workspace import WorkspaceDenied, WorkspaceResolver

# Frozen E.3A classifications. Adding one is an ADR-level change, not a local edit.
EXECUTION_DISABLED = "execution_disabled"
GOVERNANCE_INTEGRITY_ERROR = "governance_integrity_error"
APPROVAL_INACTIVE = "approval_inactive"
APPROVAL_EXPIRED = "approval_expired"
CAPABILITY_NOT_PERMITTED = "capability_not_permitted"
AGENT_IDENTITY_MISSING = "agent_identity_missing"
AGENT_IDENTITY_INVALID = "agent_identity_invalid"
AGENT_CAPABILITY_NOT_PERMITTED = "agent_capability_not_permitted"
WORKSPACE_DENIED = "workspace_denied"

# The one supported ApprovalGrant.scope shape.
SCOPE_CAPABILITIES_KEY = "capabilities"


@dataclass(frozen=True)
class GuardDecision:
    """The guard's verdict. ``allowed=True`` always carries no error fields."""

    allowed: bool
    error_code: str | None = None
    error_detail: str | None = None


ALLOWED = GuardDecision(allowed=True)


def _denied(error_code: str, error_detail: str) -> GuardDecision:
    return GuardDecision(allowed=False, error_code=error_code, error_detail=error_detail)


class PreDispatchGuard(Protocol):
    """The narrow contract the Gateway depends on; it knows nothing else about us."""

    def check(
        self,
        *,
        execution_id: UUID,
        capability: str,
        agent_id: UUID | None,
        approval_id: UUID,
        workspace_ref: str | None,
    ) -> GuardDecision:
        """Decide whether this execution may reach ``provider.dispatch``."""


class AgentExecutionGuard:
    """The Phase E.3A guard implementation (``PreDispatchGuard``)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        enabled: bool,
        workspace_resolver: WorkspaceResolver,
        agent_service: AgentService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._enabled = bool(enabled)
        self._workspace_resolver = workspace_resolver
        # Reuse the real service on the same principal, so there is exactly one
        # implementation of the capability rule. Never reimplemented here.
        self._agent_service = agent_service or AgentService(session_factory)

    def check(
        self,
        *,
        execution_id: UUID,
        capability: str,
        agent_id: UUID | None,
        approval_id: UUID,
        workspace_ref: str | None,
    ) -> GuardDecision:
        # 1. Master switch. Checked first and without touching the database: with the
        # switch off the guard must be a pure no-op.
        if not self._enabled:
            return _denied(
                EXECUTION_DISABLED,
                "agent execution is disabled (DX_AGENT_EXECUTION_ENABLED is false)",
            )

        decision = self._check_approval(
            execution_id=execution_id, capability=capability, approval_id=approval_id
        )
        if decision is not None:
            return decision

        decision = self._check_agent_identity(capability=capability, agent_id=agent_id)
        if decision is not None:
            return decision

        try:
            # E.3B calls the SAME resolver again immediately before Popen; this call is
            # the authority check, that one is the last-moment re-validation.
            self._workspace_resolver.resolve(workspace_ref)
        except WorkspaceDenied as error:
            return _denied(WORKSPACE_DENIED, str(error))

        return ALLOWED

    def _check_approval(
        self, *, execution_id: UUID, capability: str, approval_id: UUID
    ) -> GuardDecision | None:
        """Return a denial, or ``None`` when every approval check passes."""

        # Exact identity lookup with the existing dx_api session. No search, no
        # matching by role / principal / proposal / capability.
        with self._session_factory() as session:
            grant = session.get(ApprovalGrant, approval_id)
            if grant is None:
                # Defensive invariant: approval_id is a hard FK, so an absent row means
                # the governance record was destroyed behind DX's back.
                return _denied(
                    GOVERNANCE_INTEGRITY_ERROR,
                    f"approval grant {approval_id} referenced by agent execution "
                    f"{execution_id} does not exist",
                )
            grant_status = grant.status
            grant_expires_at = grant.expires_at
            grant_scope = grant.scope

        if grant_status != "active":
            return _denied(
                APPROVAL_INACTIVE,
                f"approval grant {approval_id} status is {grant_status!r}, not 'active'",
            )

        # Repository precedent for "now": datetime.now(UTC). No other normalization.
        if grant_expires_at <= datetime.now(UTC):
            return _denied(
                APPROVAL_EXPIRED,
                f"approval grant {approval_id} expired at {grant_expires_at.isoformat()}",
            )

        if not isinstance(grant_scope, dict) or SCOPE_CAPABILITIES_KEY not in grant_scope:
            return _denied(
                CAPABILITY_NOT_PERMITTED,
                f"approval grant {approval_id} scope does not declare a "
                f"{SCOPE_CAPABILITIES_KEY!r} list",
            )

        capabilities = grant_scope[SCOPE_CAPABILITIES_KEY]
        if not isinstance(capabilities, list) or capability not in capabilities:
            return _denied(
                CAPABILITY_NOT_PERMITTED,
                f"capability {capability!r} is not within approval grant {approval_id}",
            )

        return None

    def _check_agent_identity(
        self, *, capability: str, agent_id: UUID | None
    ) -> GuardDecision | None:
        """Return a denial, or ``None`` when the agent binding is satisfied."""

        if agent_id is None:
            # Option A: no role fallback. An unbound execution can never be dispatched.
            return _denied(
                AGENT_IDENTITY_MISSING,
                "agent execution has no agent_id; role-based fallback is not supported",
            )

        try:
            permission = self._agent_service.check_permission(
                agent_id=agent_id, capability=capability
            )
        except LookupError as error:
            return _denied(
                AGENT_IDENTITY_INVALID,
                f"agent {agent_id} is not registered: {error}",
            )

        if not permission.allowed:
            reasons = "; ".join(permission.reasons) or "agent permission was not granted"
            return _denied(
                AGENT_CAPABILITY_NOT_PERMITTED,
                f"agent {agent_id} may not perform {capability!r}: {reasons}",
            )

        return None


def build_pre_dispatch_guard(
    settings: Settings | WorkerSettings, session_factory: sessionmaker[Session]
) -> AgentExecutionGuard:
    """Compose the guard from configuration. The E.3B wiring point.

    Accepts either settings object: the API process composes the guard from ``Settings``,
    the worker process from ``WorkerSettings``. Both expose the same three fields under the
    same environment aliases, so this is a type-only widening -- not a new configuration
    abstraction, and not a second interpretation of the switch.
    """

    return AgentExecutionGuard(
        session_factory,
        enabled=settings.agent_execution_enabled,
        workspace_resolver=WorkspaceResolver(settings.agent_workspaces),
    )
