"""Operator / Task Card MVP (Phase E.4) — local CLI.

A minimal human-operated DX path that composes the *existing* production services
only. It never issues raw SQL, never calls a provider or the Gateway directly, never
mutates E.3B code, and never auto-accepts a result.

Command surface (no aliases, no interactive menus):

    create  — Task + IntentProposal, persist E.4 intent in proposal.scope (no approval)
    approve — review_proposal(approve) + grant_approval (no execution)
    plan    — prepare_live_plan -> execute_prepared -> parse_model_call (real call)
    confirm — review_draft(confirmed) (explicit human gate)
    execute — single-step handoff -> accept -> AgentExecution -> bounded outbox drain
    status  — read-only report of task / proposal / draft / execution / verdict
    accept  — ExecutionReview(decision="accepted") on a succeeded execution
    reject  — ExecutionReview(decision="rejected") on a succeeded execution

The E.4 execution intent is a narrow JSON contract persisted in
``IntentProposal.scope``: ``workspace_ref``, ``provider``, ``capability``, ``agent_id``.
``IntentProposal.requested_capabilities`` holds the capabilities requiring approval.
Loading is fail-closed: every field is required, type-checked, and validated; a
malformed stored intent raises instead of silently substituting a default.

Network environment is inherited from the process; no proxy address is hardcoded.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from dxbrain.config import Settings, WorkerSettings
from dxbrain.core.agent_executions import AgentExecutionService
from dxbrain.core.database import create_session_factory
from dxbrain.core.execution_reviews import ExecutionReviewService
from dxbrain.core.governance import GovernanceService
from dxbrain.core.model_calls import ModelCallService
from dxbrain.core.plan_drafts import PlanDraftService
from dxbrain.core.tasks import TaskService
from dxbrain.core.workflow import WorkflowService
from dxbrain.operator.outbox_loop import (
    OutboxLoopError,
    drain_until_terminal,
    read_status,
)
from dxbrain.providers.adapters import HttpxTransport
from dxbrain.providers.registry import ModelProviderRegistry
from dxbrain.worker.cli import build_execution_handler
from dxbrain.worker.outbox import OutboxWorker

# --- E.4 task-card intent contract -------------------------------------------------

SCOPE_KEYS = ("workspace_ref", "provider", "capability", "agent_id")

_WORKSPACE_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Capabilities the stored intent / planning path requires (approval + planning).
PLANNING_CAPABILITY = "model.call"
EXECUTION_CAPABILITY = "agent.execution"

# The provider the ExecutionGateway is wired to in real mode (frozen E.3B).
CODEX_PROVIDER = "codex"


@dataclass(frozen=True)
class StoredIntent:
    workspace_ref: str
    provider: str
    capability: str
    agent_id: UUID


class OperatorError(RuntimeError):
    """A fail-closed operator error that should be reported and exit non-zero."""


def _idempotency_key() -> str:
    return str(uuid4())


def _human_actor(actor_id: str) -> tuple[str, str]:
    if not actor_id:
        raise OperatorError("human actor id must not be empty")
    return "human", actor_id


# --- fail-closed scope parsing -----------------------------------------------------

def parse_intent(scope: dict[str, Any]) -> StoredIntent:
    """Load the E.4 intent from proposal.scope, failing closed on any deviation."""

    if not isinstance(scope, dict):
        raise OperatorError("proposal.scope is not an object; stored intent is malformed")
    missing = [key for key in SCOPE_KEYS if key not in scope]
    if missing:
        raise OperatorError(
            f"proposal.scope is missing required E.4 fields: {', '.join(missing)}"
        )

    workspace_ref = scope["workspace_ref"]
    if not isinstance(workspace_ref, str) or not _WORKSPACE_REF_RE.match(workspace_ref):
        raise OperatorError(
            "proposal.scope workspace_ref must match '^[a-z0-9][a-z0-9_-]*$'"
        )

    provider = scope["provider"]
    if not isinstance(provider, str) or not provider:
        raise OperatorError("proposal.scope provider must be a non-empty string")

    capability = scope["capability"]
    if not isinstance(capability, str) or not capability:
        raise OperatorError("proposal.scope capability must be a non-empty string")

    raw_agent_id = scope["agent_id"]
    try:
        agent_id = UUID(str(raw_agent_id))
    except (ValueError, TypeError) as error:
        raise OperatorError(
            f"proposal.scope agent_id is not a valid UUID: {raw_agent_id!r}"
        ) from error

    return StoredIntent(
        workspace_ref=workspace_ref,
        provider=provider,
        capability=capability,
        agent_id=agent_id,
    )


def build_scope(intent: StoredIntent) -> dict[str, Any]:
    return {
        "workspace_ref": intent.workspace_ref,
        "provider": intent.provider,
        "capability": intent.capability,
        "agent_id": str(intent.agent_id),
    }


def require_single_step(steps: list[Any]) -> Any:
    """Enforce the E.4 MVP single-step boundary.

    A confirmed plan may produce more than one workflow step; the MVP must refuse to
    orchestrate multiple steps rather than silently auto-orchestrate them.
    """

    if len(steps) != 1:
        raise OperatorError(
            "E.4 MVP supports exactly one workflow step; "
            f"accepted run has {len(steps)} steps"
        )
    return steps[0]


def _resolve_uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise OperatorError(f"{label} is not a valid UUID: {value!r}") from error


@dataclass(frozen=True)
class ExecutionDetail:
    status: str
    provider: str
    capability: str
    workspace_ref: str | None
    exit_code: int | None
    error_code: str | None
    summary: str | None
    result_tail: str | None


def read_execution_detail(session_factory, execution_id: UUID) -> ExecutionDetail:
    """Read the execution's technical fields the service View does not expose.

    The operator runs as dx_api and may read its own table; this is a read-only ORM
    lookup, not a lifecycle write and not raw SQL.
    """

    from sqlalchemy import select

    from dxbrain.core.models import AgentExecution

    with session_factory() as session:
        row = session.scalar(
            select(AgentExecution).where(AgentExecution.id == execution_id)
        )
        if row is None:
            raise OperatorError(f"agent execution {execution_id} not found")
        return ExecutionDetail(
            status=row.status,
            provider=row.provider,
            capability=row.capability,
            workspace_ref=row.workspace_ref,
            exit_code=row.exit_code,
            error_code=row.error_code,
            summary=row.summary,
            result_tail=row.result_tail,
        )


# --- service wiring ----------------------------------------------------------------

class OperatorContext:
    """All existing production services composed onto the operator's own principal."""

    def __init__(self, session_factory=None) -> None:
        # ``session_factory`` injection exists for tests; production reads Settings.
        if session_factory is not None:
            self.session_factory = session_factory
            self.settings = None
        else:
            settings = Settings()
            self.settings = settings
            self.session_factory = create_session_factory(settings.database_url)
        settings = self.settings
        self.tasks = TaskService(self.session_factory)
        self.governance = GovernanceService(self.session_factory)
        self.plan_drafts = PlanDraftService(self.session_factory)
        self.workflow = WorkflowService(self.session_factory)
        self.agent_executions = AgentExecutionService(self.session_factory)
        self.execution_reviews = ExecutionReviewService(self.session_factory)
        if settings is not None:
            registry = ModelProviderRegistry(
                deepseek_api_key=settings.deepseek_api_key,
                deepseek_base_url=settings.deepseek_base_url,
                deepseek_model=settings.deepseek_model,
                anthropic_api_key=settings.anthropic_api_key,
                anthropic_base_url=settings.anthropic_base_url,
                anthropic_model=settings.anthropic_model,
                planner_provider=settings.llm_planner_provider,  # type: ignore[arg-type]
                reviewer_provider=settings.llm_reviewer_provider,  # type: ignore[arg-type]
                fallback_provider=settings.llm_fallback_provider,  # type: ignore[arg-type]
            )
            self.model_calls = ModelCallService(
                self.session_factory, registry, live_enabled=settings.llm_live_enabled
            )
        else:
            # Test injection: no model registry; plan command is not exercised here.
            self.model_calls = None

    def _load_proposal(self, proposal_id: str):
        pid = _resolve_uuid(proposal_id, "proposal_id")
        return self.governance.get_proposal(pid)

    def _load_intent(self, proposal_id: str) -> tuple[StoredIntent, Any]:
        proposal = self._load_proposal(proposal_id)
        return parse_intent(proposal.scope), proposal


# --- commands ----------------------------------------------------------------------

def cmd_create(ctx: OperatorContext, args: argparse.Namespace) -> int:
    intent = StoredIntent(
        workspace_ref=args.workspace_ref,
        provider=args.provider,
        capability=args.capability,
        agent_id=_resolve_uuid(args.agent_id, "agent_id"),
    )
    if not _WORKSPACE_REF_RE.match(intent.workspace_ref):
        raise OperatorError("workspace_ref must match '^[a-z0-9][a-z0-9_-]*$'")

    actor_type, actor_id = _human_actor(args.actor_id)
    task = ctx.tasks.create_task(
        title=args.title,
        description=args.instruction,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    # The E.4 intent plus the capabilities required by the planning + execution path.
    requested_capabilities = [PLANNING_CAPABILITY, EXECUTION_CAPABILITY]
    proposal = ctx.governance.create_proposal(
        task_id=task.id,
        objective=args.title,
        rationale=args.instruction,
        scope=build_scope(intent),
        requested_capabilities=requested_capabilities,
        # The planning chain replaces this placeholder plan via ModelCall -> PlanDraft.
        plan=[{"title": args.title, "capability": intent.capability, "parameters": {}}],
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    print(json.dumps({
        "task_id": str(task.id),
        "proposal_id": str(proposal.id),
        "stored_intent": build_scope(intent),
        "requested_capabilities": requested_capabilities,
    }, indent=2))
    return 0


def cmd_approve(ctx: OperatorContext, args: argparse.Namespace) -> int:
    pid = _resolve_uuid(args.proposal_id, "proposal_id")
    intent, proposal = ctx._load_intent(args.proposal_id)
    actor_type, actor_id = _human_actor(args.actor_id)

    ctx.governance.review_proposal(
        proposal_id=pid,
        decision="approve",
        risk_level=args.risk_level,
        reason=args.reason or "operator approval",
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    grant = ctx.governance.grant_approval(
        proposal_id=pid,
        # ApprovalGrant.scope stays compatible with {"capabilities": [...]}; the
        # E.4 intent (workspace/provider/agent) remains in proposal.scope.
        scope={"capabilities": proposal.requested_capabilities},
        expires_at=datetime.now(UTC) + timedelta(hours=args.validity_hours),
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    print(json.dumps({
        "approval_id": str(grant.id),
        "expires_at": grant.expires_at.isoformat(),
        "approved_capabilities": proposal.requested_capabilities,
    }, indent=2))
    return 0


def cmd_plan(ctx: OperatorContext, args: argparse.Namespace) -> int:
    pid = _resolve_uuid(args.proposal_id, "proposal_id")
    ctx._load_proposal(args.proposal_id)  # existence check
    actor_type, actor_id = _human_actor(args.actor_id)

    prepared = ctx.model_calls.prepare_live_plan(
        proposal_id=pid,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    finalized = ctx.model_calls.execute_prepared(
        model_call_id=prepared.id,
        actor_type=actor_type,
        actor_id=actor_id,
        execution_key=_idempotency_key(),
        transport=HttpxTransport(),
    )
    if finalized.status != "succeeded":
        raise OperatorError(
            f"planning model call did not succeed (status={finalized.status}); "
            "no plan draft was produced"
        )
    draft = ctx.plan_drafts.parse_model_call(
        model_call_id=finalized.id,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    print(json.dumps({
        "model_call_id": str(finalized.id),
        "draft_id": str(draft.id),
        "draft_status": draft.status,
        "steps": draft.steps,
        "warnings": draft.warnings,
    }, indent=2))
    return 0


def cmd_confirm(ctx: OperatorContext, args: argparse.Namespace) -> int:
    draft_id = _resolve_uuid(args.draft_id, "draft_id")
    actor_type, actor_id = _human_actor(args.actor_id)
    draft = ctx.plan_drafts.review_draft(
        draft_id=draft_id,
        status="confirmed",
        reason=args.reason or "operator confirmation",
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    print(json.dumps({
        "draft_id": str(draft.id),
        "revision": draft.revision,
        "status": draft.status,
    }, indent=2))
    return 0


def cmd_execute(ctx: OperatorContext, args: argparse.Namespace) -> int:
    intent, proposal = ctx._load_intent(args.proposal_id)
    pid = _resolve_uuid(args.proposal_id, "proposal_id")
    actor_type, actor_id = _human_actor(args.actor_id)

    # 2. Require an active valid ApprovalGrant.
    grants = ctx.governance.list_approvals(pid)
    active = [
        g for g in grants
        if g.status == "active" and g.expires_at > datetime.now(UTC)
    ]
    if not active:
        raise OperatorError("proposal has no active approval grant")

    # 3. Require a confirmed PlanDraft.
    drafts = ctx.plan_drafts.list_drafts(pid)
    confirmed = [d for d in drafts if d.status == "confirmed"]
    if not confirmed:
        raise OperatorError("proposal has no confirmed plan draft")
    draft = confirmed[-1]

    # 4/5. Handoff -> accept via existing production services.
    handoff = ctx.plan_drafts.request_workflow_handoff(
        draft_id=draft.id,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    run = ctx.workflow.accept_handoff(
        handoff_id=handoff.id,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )

    # 7. E.4 MVP requires exactly one executable workflow step.
    step = require_single_step(run.steps)

    # 8. Create the AgentExecution through the existing service.
    execution = ctx.agent_executions.create(
        task_id=proposal.task_id,
        proposal_id=pid,
        approval_id=active[0].id,
        workflow_run_id=run.id,
        workflow_step_id=step.id,
        draft_id=draft.id,
        draft_revision=draft.revision,
        agent_role="operator",
        agent_id=intent.agent_id,
        provider=intent.provider,
        capability=intent.capability,
        workspace_ref=intent.workspace_ref,
        request_input={
            "title": step.title,
            "capability": step.capability,
            "parameters": step.parameters,
        },
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )

    # 9. Bounded outbox drain through the existing worker composition. The worker and
    # its gateway handler are wired exactly as ``dxbrain-worker`` wires them, from
    # ``WorkerSettings`` (DX_WORKER_DATABASE_URL for the lease principal, optional
    # DX_EXECUTION_DATABASE_URL for the gateway principal) — never from the operator's
    # own dx_api URL, so the dx_api / dx_worker split is preserved.
    worker_settings = WorkerSettings()
    worker = OutboxWorker(
        create_session_factory(worker_settings.database_url),
        handler=build_execution_handler(worker_settings),
    )
    try:
        result = drain_until_terminal(
            worker=worker,
            session_factory=ctx.session_factory,
            execution_id=execution.id,
        )
    except OutboxLoopError as error:
        raise OperatorError(str(error)) from error

    final = read_status(ctx.session_factory, execution.id)
    detail = read_execution_detail(ctx.session_factory, execution.id)
    print(json.dumps({
        "execution_id": str(execution.id),
        "status": final,
        "exit_code": detail.exit_code,
        "error_code": detail.error_code,
        "summary": detail.summary,
        "result_tail": detail.result_tail,
        "consumed_events": result.consumed_events,
    }, indent=2))
    return 0


def cmd_status(ctx: OperatorContext, args: argparse.Namespace) -> int:
    out: dict[str, Any] = {}

    if args.proposal_id:
        _, proposal = ctx._load_intent(args.proposal_id)
        drafts = ctx.plan_drafts.list_drafts(proposal.id)
        draft_state = drafts[-1].status if drafts else None
        out.update({
            "task_id": str(proposal.task_id),
            "proposal_id": str(proposal.id),
            "proposal_status": proposal.status,
            "draft_status": draft_state,
        })
    elif args.execution_id:
        eid = _resolve_uuid(args.execution_id, "execution_id")
        detail = read_execution_detail(ctx.session_factory, eid)
        out["execution"] = {
            "execution_id": str(eid),
            "status": detail.status,
            "provider": detail.provider,
            "workspace_ref": detail.workspace_ref,
            "exit_code": detail.exit_code,
            "error_code": detail.error_code,
            "summary": detail.summary,
        }
        review = ctx.execution_reviews.get(eid)
        out["execution"]["human_review"] = (
            {"decision": review.decision, "reason": review.reason,
             "actor_id": review.actor_id, "created_at": str(review.created_at)}
            if review else None
        )
    else:
        raise OperatorError("status requires --proposal-id or --execution-id")

    print(json.dumps(out, indent=2))
    return 0


def _record_verdict(ctx: OperatorContext, args: argparse.Namespace, decision: str) -> int:
    eid = _resolve_uuid(args.execution_id, "execution_id")
    actor_type, actor_id = _human_actor(args.actor_id)
    review = ctx.execution_reviews.record_decision(
        agent_execution_id=eid,
        decision=decision,  # type: ignore[arg-type]
        reason=args.reason or f"human {decision}",
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=_idempotency_key(),
    )
    # The technical result is untouched; report both facts.
    view = ctx.agent_executions.get(eid)
    print(json.dumps({
        "execution_id": str(eid),
        "agent_execution_status": view.status if view else None,
        "human_review": {
            "decision": review.decision,
            "reason": review.reason,
            "actor_id": review.actor_id,
        },
    }, indent=2))
    return 0


def cmd_accept(ctx: OperatorContext, args: argparse.Namespace) -> int:
    return _record_verdict(ctx, args, "accepted")


def cmd_reject(ctx: OperatorContext, args: argparse.Namespace) -> int:
    return _record_verdict(ctx, args, "rejected")


# --- argument parsing --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dxbrain-operator", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="create Task + IntentProposal with E.4 intent")
    p.add_argument("--title", required=True)
    p.add_argument("--instruction", required=True)
    p.add_argument("--workspace-ref", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--capability", required=True)
    p.add_argument("--agent-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("approve", help="review + grant approval (no execution)")
    p.add_argument("--proposal-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.add_argument("--risk-level", default="low", choices=["low", "medium", "high"])
    p.add_argument("--validity-hours", type=float, default=24.0)
    p.add_argument("--reason")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("plan", help="real planning call -> PlanDraft")
    p.add_argument("--proposal-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("confirm", help="confirm plan draft (explicit human gate)")
    p.add_argument("--draft-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.add_argument("--reason")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("execute", help="single-step execution via bounded outbox drain")
    p.add_argument("--proposal-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.set_defaults(func=cmd_execute)

    p = sub.add_parser("status", help="read-only report")
    p.add_argument("--proposal-id")
    p.add_argument("--execution-id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("accept", help="ExecutionReview accepted (succeeded execution)")
    p.add_argument("--execution-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.add_argument("--reason")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("reject", help="ExecutionReview rejected (succeeded execution)")
    p.add_argument("--execution-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_reject)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = OperatorContext()
    try:
        return args.func(ctx, args)
    except OperatorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
