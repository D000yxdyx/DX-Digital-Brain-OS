from __future__ import annotations

import hmac
from collections.abc import Generator
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from dxbrain.config import Settings
from dxbrain.core.agents import (
    AgentConflict,
    AgentPayloadMismatch,
    AgentPermissionView,
    AgentService,
    AgentView,
)
from dxbrain.core.archive import (
    ArchiveConflict,
    ArchivePayloadMismatch,
    ArchiveService,
    DossierView,
    LessonView,
    RetrospectiveView,
)
from dxbrain.core.database import create_session_factory
from dxbrain.core.governance import (
    ApprovalGrantView,
    ExecutionReportView,
    GovernanceConflict,
    GovernancePayloadMismatch,
    GovernanceService,
    IntentProposalView,
    ReviewDecisionView,
)
from dxbrain.core.model_calls import (
    ModelCallConflict,
    ModelCallPayloadMismatch,
    ModelCallService,
    ModelCallView,
    ModelReadinessView,
)
from dxbrain.core.plan_drafts import (
    PlanDraftConflict,
    PlanDraftPayloadMismatch,
    PlanDraftRevisionView,
    PlanDraftService,
    PlanDraftView,
    WorkflowHandoffView,
)
from dxbrain.core.tasks import (
    IdempotencyPayloadMismatch,
    TaskService,
    TaskView,
)
from dxbrain.core.workflow import (
    WorkflowConflict,
    WorkflowPayloadMismatch,
    WorkflowRunView,
    WorkflowService,
)
from dxbrain.providers.adapters import HttpxTransport
from dxbrain.providers.registry import ModelProviderRegistry

__all__ = ['CreateTaskRequest', 'TaskResponse', 'StateTransitionResponse', 'TaskDetailResponse', 'TaskListItemResponse', 'ControlOverviewResponse', 'ModelProviderResponse', 'ModelCallResponse', 'FinalizeModelCallRequest', 'ModelReadinessResponse', 'PlanDraftResponse', 'ReviewPlanDraftRequest', 'EditPlanDraftRequest', 'PlanDraftRevisionResponse', 'WorkflowHandoffResponse', 'WorkflowStepResponse', 'WorkflowRunResponse', 'RegisterAgentRequest', 'AgentResponse', 'AgentLifecycleRequest', 'AgentPermissionResponse', 'CreateDossierRequest', 'DossierResponse', 'CreateRetrospectiveRequest', 'RetrospectiveResponse', 'CreateLessonRequest', 'LessonReviewRequest', 'LessonResponse', 'PlanStepRequest', 'CreateProposalRequest', 'IntentProposalResponse', 'ReviewRequest', 'ReviewDecisionResponse', 'ApprovalRequest', 'ApprovalGrantResponse', 'ExecutionReportRequest', 'ExecutionReportResponse', '_response', '_proposal_response', '_review_response', '_approval_response', '_report_response', '_dossier_response', '_retrospective_response', '_lesson_response', '_model_call_response', '_model_readiness_response', '_plan_draft_response', '_plan_draft_revision_response', '_workflow_handoff_response', '_workflow_run_response', '_agent_response', '_agent_permission_response']

class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=10_000)


class TaskResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    status: str
    version: int


class StateTransitionResponse(BaseModel):
    version: int
    previous_status: str | None
    next_status: str


class TaskDetailResponse(TaskResponse):
    history: list[StateTransitionResponse]


class TaskListItemResponse(BaseModel):
    id: UUID
    title: str
    status: str
    version: int
    created_at: datetime


class ControlOverviewResponse(BaseModel):
    task_count: int
    event_count: int
    audit_count: int
    state_counts: dict[str, int]
    outbox_counts: dict[str, int]


class ModelProviderResponse(BaseModel):
    name: str
    configured: bool
    base_url: str
    default_model: str
    transport: str


class ModelCallResponse(BaseModel):
    id: UUID
    task_id: UUID
    proposal_id: UUID | None
    role: str
    mode: str
    status: str
    provider: str
    model: str
    request_summary: dict[str, Any]
    response_summary: dict[str, Any]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None
    latency_ms: int | None
    error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    replayed: bool = False


class FinalizeModelCallRequest(BaseModel):
    status: Literal["succeeded", "failed"]
    response_summary: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=64)


class ModelReadinessResponse(BaseModel):
    proposal_id: UUID
    role: str
    provider: str
    model: str
    provider_configured: bool
    live_enabled: bool
    approval_present: bool
    approval_allows_model_call: bool
    allowed: bool
    reasons: list[str]


class PlanDraftResponse(BaseModel):
    id: UUID
    task_id: UUID
    proposal_id: UUID
    model_call_id: UUID
    status: str
    revision: int
    parser_version: str
    source_hash: str
    steps: list[dict[str, Any]]
    warnings: list[str]
    created_at: datetime
    reviewed_at: datetime | None
    reviewer_actor_type: str | None
    reviewer_actor_id: str | None
    review_reason: str | None
    replayed: bool = False


class ReviewPlanDraftRequest(BaseModel):
    status: Literal["confirmed", "rejected"]
    reason: str = Field(min_length=1, max_length=20_000)


class EditPlanDraftRequest(BaseModel):
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=20_000)


class PlanDraftRevisionResponse(BaseModel):
    id: UUID
    draft_id: UUID
    revision: int
    steps: list[dict[str, Any]]
    reason: str
    actor_type: str
    actor_id: str
    created_at: datetime


class WorkflowHandoffResponse(BaseModel):
    id: UUID
    task_id: UUID
    proposal_id: UUID
    draft_id: UUID
    draft_revision: int
    status: str
    actor_type: str
    actor_id: str
    created_at: datetime
    replayed: bool = False


class WorkflowStepResponse(BaseModel):
    id: UUID
    ordinal: int
    title: str
    capability: str
    parameters: dict[str, Any]
    status: str
    result_summary: str | None


class WorkflowRunResponse(BaseModel):
    id: UUID
    task_id: UUID
    proposal_id: UUID
    handoff_id: UUID
    draft_id: UUID
    draft_revision: int
    status: str
    steps: list[WorkflowStepResponse]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    replayed: bool = False


class RegisterAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    role: str = Field(min_length=1, max_length=64)
    contract: dict[str, Any] = Field(min_length=1)
    allowed_capabilities: list[str] = Field(min_length=1, max_length=64)


class AgentResponse(BaseModel):
    id: UUID
    task_id: UUID
    name: str
    version: str
    role: str
    contract: dict[str, Any]
    allowed_capabilities: list[str]
    status: str
    created_at: datetime
    updated_at: datetime
    replayed: bool = False


class AgentLifecycleRequest(BaseModel):
    target_status: Literal["active", "paused", "retired"]
    reason: str = Field(min_length=1, max_length=20_000)


class AgentPermissionResponse(BaseModel):
    agent_id: UUID
    capability: str
    allowed: bool
    requires_approval: bool
    reasons: list[str]


class CreateDossierRequest(BaseModel):
    status: Literal["open", "closed", "cancelled"] = "closed"
    objective: str = Field(min_length=1, max_length=20_000)
    plan_snapshot: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    result_summary: str | None = Field(default=None, max_length=20_000)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=256)


class DossierResponse(BaseModel):
    id: UUID
    task_id: UUID
    status: str
    objective: str
    plan_snapshot: list[dict[str, Any]]
    result_summary: str | None
    artifacts: dict[str, Any]
    evidence: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    replayed: bool = False


class CreateRetrospectiveRequest(BaseModel):
    expected_outcome: str = Field(min_length=1, max_length=20_000)
    actual_outcome: str = Field(min_length=1, max_length=20_000)
    deviations: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    root_causes: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    keep_items: list[str] = Field(default_factory=list, max_length=128)
    change_items: list[str] = Field(default_factory=list, max_length=128)
    stop_items: list[str] = Field(default_factory=list, max_length=128)
    follow_up_items: list[str] = Field(default_factory=list, max_length=128)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=256)


class RetrospectiveResponse(BaseModel):
    id: UUID
    dossier_id: UUID
    task_id: UUID
    status: str
    expected_outcome: str
    actual_outcome: str
    deviations: list[dict[str, Any]]
    root_causes: list[dict[str, Any]]
    keep_items: list[str]
    change_items: list[str]
    stop_items: list[str]
    follow_up_items: list[str]
    evidence_refs: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    replayed: bool = False


class CreateLessonRequest(BaseModel):
    memory_type: Literal["episodic", "semantic", "procedural"]
    statement: str = Field(min_length=1, max_length=20_000)
    applicability: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    confidence: float = Field(ge=0, le=1)


class LessonReviewRequest(BaseModel):
    status: Literal["validated", "retracted"]
    reason: str = Field(min_length=1, max_length=20_000)


class LessonResponse(BaseModel):
    id: UUID
    retrospective_id: UUID
    task_id: UUID
    status: str
    memory_type: str
    statement: str
    applicability: dict[str, Any]
    evidence_refs: list[dict[str, Any]]
    confidence: float
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None
    reviewer_actor_type: str | None
    reviewer_actor_id: str | None
    review_reason: str | None
    replayed: bool = False


class PlanStepRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    capability: str = Field(min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)


class CreateProposalRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=10_000)
    scope: dict[str, Any] = Field(default_factory=dict)
    requested_capabilities: list[str] = Field(default_factory=list, max_length=32)
    plan: list[PlanStepRequest] = Field(min_length=1, max_length=64)


class IntentProposalResponse(BaseModel):
    id: UUID
    task_id: UUID
    revision: int
    status: str
    objective: str
    rationale: str
    scope: dict[str, Any]
    requested_capabilities: list[str]
    plan: list[dict[str, Any]]
    created_at: datetime
    replayed: bool = False


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject", "request_input"]
    risk_level: Literal["low", "medium", "high"]
    reason: str = Field(min_length=1, max_length=10_000)


class ReviewDecisionResponse(BaseModel):
    id: UUID
    proposal_id: UUID
    task_id: UUID
    decision: str
    risk_level: str
    reason: str
    created_at: datetime
    proposal_status: str
    replayed: bool = False


class ApprovalRequest(BaseModel):
    scope: dict[str, Any] = Field(min_length=1)
    expires_at: datetime


class ApprovalGrantResponse(BaseModel):
    id: UUID
    proposal_id: UUID
    task_id: UUID
    scope: dict[str, Any]
    status: str
    expires_at: datetime
    created_at: datetime
    replayed: bool = False


class ExecutionReportRequest(BaseModel):
    status: Literal["started", "succeeded", "failed", "cancelled"]
    summary: str = Field(min_length=1, max_length=20_000)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class ExecutionReportResponse(BaseModel):
    id: UUID
    proposal_id: UUID
    task_id: UUID
    status: str
    summary: str
    artifacts: dict[str, Any]
    created_at: datetime
    proposal_status: str
    replayed: bool = False


def _response(view: TaskView) -> TaskResponse:
    return TaskResponse(
        id=view.id,
        title=view.title,
        description=view.description,
        status=view.status,
        version=view.version,
    )


def _proposal_response(view: IntentProposalView) -> IntentProposalResponse:
    return IntentProposalResponse(
        id=view.id,
        task_id=view.task_id,
        revision=view.revision,
        status=view.status,
        objective=view.objective,
        rationale=view.rationale,
        scope=view.scope,
        requested_capabilities=view.requested_capabilities,
        plan=view.plan,
        created_at=view.created_at,
        replayed=view.replayed,
    )


def _review_response(view: ReviewDecisionView) -> ReviewDecisionResponse:
    return ReviewDecisionResponse(
        id=view.id,
        proposal_id=view.proposal_id,
        task_id=view.task_id,
        decision=view.decision,
        risk_level=view.risk_level,
        reason=view.reason,
        created_at=view.created_at,
        proposal_status=view.proposal_status,
        replayed=view.replayed,
    )


def _approval_response(view: ApprovalGrantView) -> ApprovalGrantResponse:
    return ApprovalGrantResponse(
        id=view.id,
        proposal_id=view.proposal_id,
        task_id=view.task_id,
        scope=view.scope,
        status=view.status,
        expires_at=view.expires_at,
        created_at=view.created_at,
        replayed=view.replayed,
    )


def _report_response(view: ExecutionReportView) -> ExecutionReportResponse:
    return ExecutionReportResponse(
        id=view.id,
        proposal_id=view.proposal_id,
        task_id=view.task_id,
        status=view.status,
        summary=view.summary,
        artifacts=view.artifacts,
        created_at=view.created_at,
        proposal_status=view.proposal_status,
        replayed=view.replayed,
    )


def _dossier_response(view: DossierView) -> DossierResponse:
    return DossierResponse(
        id=view.id,
        task_id=view.task_id,
        status=view.status,
        objective=view.objective,
        plan_snapshot=view.plan_snapshot,
        result_summary=view.result_summary,
        artifacts=view.artifacts,
        evidence=view.evidence,
        created_at=view.created_at,
        updated_at=view.updated_at,
        closed_at=view.closed_at,
        replayed=view.replayed,
    )


def _retrospective_response(view: RetrospectiveView) -> RetrospectiveResponse:
    return RetrospectiveResponse(
        id=view.id,
        dossier_id=view.dossier_id,
        task_id=view.task_id,
        status=view.status,
        expected_outcome=view.expected_outcome,
        actual_outcome=view.actual_outcome,
        deviations=view.deviations,
        root_causes=view.root_causes,
        keep_items=view.keep_items,
        change_items=view.change_items,
        stop_items=view.stop_items,
        follow_up_items=view.follow_up_items,
        evidence_refs=view.evidence_refs,
        created_at=view.created_at,
        updated_at=view.updated_at,
        replayed=view.replayed,
    )


def _lesson_response(view: LessonView) -> LessonResponse:
    return LessonResponse(
        id=view.id,
        retrospective_id=view.retrospective_id,
        task_id=view.task_id,
        status=view.status,
        memory_type=view.memory_type,
        statement=view.statement,
        applicability=view.applicability,
        evidence_refs=view.evidence_refs,
        confidence=view.confidence,
        created_at=view.created_at,
        updated_at=view.updated_at,
        reviewed_at=view.reviewed_at,
        reviewer_actor_type=view.reviewer_actor_type,
        reviewer_actor_id=view.reviewer_actor_id,
        review_reason=view.review_reason,
        replayed=view.replayed,
    )


def _model_call_response(view: ModelCallView) -> ModelCallResponse:
    return ModelCallResponse(
        id=view.id,
        task_id=view.task_id,
        proposal_id=view.proposal_id,
        role=view.role,
        mode=view.mode,
        status=view.status,
        provider=view.provider,
        model=view.model,
        request_summary=view.request_summary,
        response_summary=view.response_summary,
        input_tokens=view.input_tokens,
        output_tokens=view.output_tokens,
        total_tokens=view.total_tokens,
        cost_usd=view.cost_usd,
        latency_ms=view.latency_ms,
        error_code=view.error_code,
        started_at=view.started_at,
        finished_at=view.finished_at,
        created_at=view.created_at,
        replayed=view.replayed,
    )


def _model_readiness_response(view: ModelReadinessView) -> ModelReadinessResponse:
    return ModelReadinessResponse(
        proposal_id=view.proposal_id,
        role=view.role,
        provider=view.provider,
        model=view.model,
        provider_configured=view.provider_configured,
        live_enabled=view.live_enabled,
        approval_present=view.approval_present,
        approval_allows_model_call=view.approval_allows_model_call,
        allowed=view.allowed,
        reasons=view.reasons,
    )


def _plan_draft_response(view: PlanDraftView) -> PlanDraftResponse:
    return PlanDraftResponse(
        id=view.id,
        task_id=view.task_id,
        proposal_id=view.proposal_id,
        model_call_id=view.model_call_id,
        status=view.status,
        revision=view.revision,
        parser_version=view.parser_version,
        source_hash=view.source_hash,
        steps=view.steps,
        warnings=view.warnings,
        created_at=view.created_at,
        reviewed_at=view.reviewed_at,
        reviewer_actor_type=view.reviewer_actor_type,
        reviewer_actor_id=view.reviewer_actor_id,
        review_reason=view.review_reason,
        replayed=view.replayed,
    )


def _plan_draft_revision_response(view: PlanDraftRevisionView) -> PlanDraftRevisionResponse:
    return PlanDraftRevisionResponse(
        id=view.id,
        draft_id=view.draft_id,
        revision=view.revision,
        steps=view.steps,
        reason=view.reason,
        actor_type=view.actor_type,
        actor_id=view.actor_id,
        created_at=view.created_at,
    )


def _workflow_handoff_response(view: WorkflowHandoffView) -> WorkflowHandoffResponse:
    return WorkflowHandoffResponse(
        id=view.id,
        task_id=view.task_id,
        proposal_id=view.proposal_id,
        draft_id=view.draft_id,
        draft_revision=view.draft_revision,
        status=view.status,
        actor_type=view.actor_type,
        actor_id=view.actor_id,
        created_at=view.created_at,
        replayed=view.replayed,
    )


def _workflow_run_response(view: WorkflowRunView) -> WorkflowRunResponse:
    return WorkflowRunResponse(
        id=view.id,
        task_id=view.task_id,
        proposal_id=view.proposal_id,
        handoff_id=view.handoff_id,
        draft_id=view.draft_id,
        draft_revision=view.draft_revision,
        status=view.status,
        steps=[
            WorkflowStepResponse(
                id=step.id,
                ordinal=step.ordinal,
                title=step.title,
                capability=step.capability,
                parameters=step.parameters,
                status=step.status,
                result_summary=step.result_summary,
            )
            for step in view.steps
        ],
        created_at=view.created_at,
        updated_at=view.updated_at,
        completed_at=view.completed_at,
        replayed=view.replayed,
    )


def _agent_response(view: AgentView) -> AgentResponse:
    return AgentResponse(
        id=view.id,
        task_id=view.task_id,
        name=view.name,
        version=view.version,
        role=view.role,
        contract=view.contract,
        allowed_capabilities=view.allowed_capabilities,
        status=view.status,
        created_at=view.created_at,
        updated_at=view.updated_at,
        replayed=view.replayed,
    )


def _agent_permission_response(view: AgentPermissionView) -> AgentPermissionResponse:
    return AgentPermissionResponse(
        agent_id=view.agent_id,
        capability=view.capability,
        allowed=view.allowed,
        requires_approval=view.requires_approval,
        reasons=view.reasons,
    )
