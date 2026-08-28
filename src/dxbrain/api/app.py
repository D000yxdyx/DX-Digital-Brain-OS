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


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()  # type: ignore[call-arg]
    session_factory = create_session_factory(config.database_url)
    service = TaskService(session_factory)
    governance_service = GovernanceService(session_factory)
    archive_service = ArchiveService(session_factory)
    provider_registry = ModelProviderRegistry(
        deepseek_api_key=config.deepseek_api_key,
        deepseek_base_url=config.deepseek_base_url,
        deepseek_model=config.deepseek_model,
        anthropic_api_key=config.anthropic_api_key,
        anthropic_base_url=config.anthropic_base_url,
        anthropic_model=config.anthropic_model,
        planner_provider=config.llm_planner_provider,  # type: ignore[arg-type]
        reviewer_provider=config.llm_reviewer_provider,  # type: ignore[arg-type]
        fallback_provider=config.llm_fallback_provider,  # type: ignore[arg-type]
    )
    model_call_service = ModelCallService(
        session_factory, provider_registry, live_enabled=config.llm_live_enabled
    )
    plan_draft_service = PlanDraftService(session_factory)
    workflow_service = WorkflowService(session_factory)
    agent_service = AgentService(session_factory)
    app = FastAPI(title="DX Digital Brain OS", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.control_ui_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-DX-API-Key", "Idempotency-Key", "Content-Type"],
    )

    def require_actor(x_dx_api_key: str = Header(...)) -> tuple[str, str]:
        if not hmac.compare_digest(x_dx_api_key, config.root_api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
        return config.actor_type, config.actor_id

    def get_service() -> Generator[TaskService, None, None]:
        yield service

    def get_governance_service() -> Generator[GovernanceService, None, None]:
        yield governance_service

    def get_archive_service() -> Generator[ArchiveService, None, None]:
        yield archive_service

    def get_model_call_service() -> Generator[ModelCallService, None, None]:
        yield model_call_service

    def get_plan_draft_service() -> Generator[PlanDraftService, None, None]:
        yield plan_draft_service

    def get_workflow_service() -> Generator[WorkflowService, None, None]:
        yield workflow_service

    def get_agent_service() -> Generator[AgentService, None, None]:
        yield agent_service

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/tasks/{task_id}/agents",
        response_model=AgentResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def register_agent(
        task_id: UUID,
        command: RegisterAgentRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        agents: AgentService = Depends(get_agent_service),
    ) -> AgentResponse:
        try:
            result = agents.register_agent(
                task_id=task_id,
                **command.model_dump(),
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except AgentPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except AgentConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
            ) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _agent_response(result)

    @app.get("/v1/agents", response_model=list[AgentResponse])
    def list_agents(
        _: tuple[str, str] = Depends(require_actor),
        agents: AgentService = Depends(get_agent_service),
    ) -> list[AgentResponse]:
        return [_agent_response(item) for item in agents.list_agents()]

    @app.get("/v1/agents/{agent_id}", response_model=AgentResponse)
    def get_agent(
        agent_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        agents: AgentService = Depends(get_agent_service),
    ) -> AgentResponse:
        try:
            return _agent_response(agents.get_agent(agent_id))
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/agents/{agent_id}/lifecycle",
        response_model=AgentResponse,
        status_code=status.HTTP_200_OK,
    )
    def transition_agent(
        agent_id: UUID,
        command: AgentLifecycleRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        agents: AgentService = Depends(get_agent_service),
    ) -> AgentResponse:
        try:
            result = agents.transition(
                agent_id=agent_id,
                **command.model_dump(),
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except AgentPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except AgentConflict as error:
            code = (
                status.HTTP_403_FORBIDDEN
                if "only a human" in str(error)
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=code, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _agent_response(result)

    @app.get(
        "/v1/agents/{agent_id}/permission-check",
        response_model=AgentPermissionResponse,
    )
    def check_agent_permission(
        agent_id: UUID,
        capability: str = Query(min_length=1, max_length=128),
        _: tuple[str, str] = Depends(require_actor),
        agents: AgentService = Depends(get_agent_service),
    ) -> AgentPermissionResponse:
        try:
            return _agent_permission_response(
                agents.check_permission(agent_id=agent_id, capability=capability)
            )
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post("/v1/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
    def create_task(
        command: CreateTaskRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        task_service: TaskService = Depends(get_service),
    ) -> TaskResponse:
        try:
            result = task_service.create_task(
                title=command.title,
                description=command.description,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except IdempotencyPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _response(result)

    @app.get("/v1/tasks/{task_id}", response_model=TaskDetailResponse)
    def get_task(
        task_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        task_service: TaskService = Depends(get_service),
    ) -> TaskDetailResponse:
        try:
            task, history = task_service.get_task(task_id)
        except LookupError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="task not found"
            ) from error
        return TaskDetailResponse(
            **_response(task).model_dump(),
            history=[
                StateTransitionResponse(
                    version=item.version,
                    previous_status=item.previous_status,
                    next_status=item.next_status,
                )
                for item in history
            ],
        )

    @app.get("/v1/control/overview", response_model=ControlOverviewResponse)
    def get_control_overview(
        _: tuple[str, str] = Depends(require_actor),
        task_service: TaskService = Depends(get_service),
    ) -> ControlOverviewResponse:
        overview = task_service.get_control_overview()
        return ControlOverviewResponse(
            task_count=overview.task_count,
            event_count=overview.event_count,
            audit_count=overview.audit_count,
            state_counts=overview.state_counts,
            outbox_counts=overview.outbox_counts,
        )

    @app.get("/v1/control/model-providers", response_model=list[ModelProviderResponse])
    def list_model_providers(
        _: tuple[str, str] = Depends(require_actor),
    ) -> list[ModelProviderResponse]:
        """Expose provider readiness only; API keys never leave the server process."""

        return [
            ModelProviderResponse(
                name=item.name,
                configured=item.configured,
                base_url=item.base_url,
                default_model=item.default_model,
                transport=item.transport,
            )
            for item in provider_registry.list()
        ]

    @app.post(
        "/v1/tasks/{task_id}/dossier",
        response_model=DossierResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_dossier(
        task_id: UUID,
        command: CreateDossierRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        archive: ArchiveService = Depends(get_archive_service),
    ) -> DossierResponse:
        try:
            result = archive.create_dossier(
                task_id=task_id,
                status=command.status,
                objective=command.objective,
                plan_snapshot=command.plan_snapshot,
                result_summary=command.result_summary,
                artifacts=command.artifacts,
                evidence=command.evidence,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except ArchivePayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ArchiveConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _dossier_response(result)

    @app.get("/v1/tasks/{task_id}/dossier", response_model=DossierResponse)
    def get_dossier(
        task_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        archive: ArchiveService = Depends(get_archive_service),
    ) -> DossierResponse:
        try:
            return _dossier_response(archive.get_dossier(task_id))
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/dossiers/{dossier_id}/retrospectives",
        response_model=RetrospectiveResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_retrospective(
        dossier_id: UUID,
        command: CreateRetrospectiveRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        archive: ArchiveService = Depends(get_archive_service),
    ) -> RetrospectiveResponse:
        try:
            result = archive.create_retrospective(
                dossier_id=dossier_id,
                **command.model_dump(),
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except ArchivePayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ArchiveConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _retrospective_response(result)

    @app.get(
        "/v1/dossiers/{dossier_id}/retrospectives",
        response_model=list[RetrospectiveResponse],
    )
    def list_retrospectives(
        dossier_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        archive: ArchiveService = Depends(get_archive_service),
    ) -> list[RetrospectiveResponse]:
        try:
            return [
                _retrospective_response(item) for item in archive.list_retrospectives(dossier_id)
            ]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/retrospectives/{retrospective_id}/lessons",
        response_model=LessonResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_lesson(
        retrospective_id: UUID,
        command: CreateLessonRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        archive: ArchiveService = Depends(get_archive_service),
    ) -> LessonResponse:
        try:
            result = archive.create_lesson(
                retrospective_id=retrospective_id,
                **command.model_dump(),
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except ArchivePayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ArchiveConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _lesson_response(result)

    @app.get(
        "/v1/retrospectives/{retrospective_id}/lessons",
        response_model=list[LessonResponse],
    )
    def list_lessons(
        retrospective_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        archive: ArchiveService = Depends(get_archive_service),
    ) -> list[LessonResponse]:
        try:
            return [_lesson_response(item) for item in archive.list_lessons(retrospective_id)]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/lessons/{lesson_id}/review",
        response_model=LessonResponse,
        status_code=status.HTTP_200_OK,
    )
    def review_lesson(
        lesson_id: UUID,
        command: LessonReviewRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        archive: ArchiveService = Depends(get_archive_service),
    ) -> LessonResponse:
        try:
            result = archive.review_lesson(
                lesson_id=lesson_id,
                status=command.status,
                reason=command.reason,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except ArchivePayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ArchiveConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _lesson_response(result)

    @app.get("/v1/control/tasks", response_model=list[TaskListItemResponse])
    def list_control_tasks(
        limit: int = Query(default=50, ge=1, le=100),
        _: tuple[str, str] = Depends(require_actor),
        task_service: TaskService = Depends(get_service),
    ) -> list[TaskListItemResponse]:
        return [
            TaskListItemResponse(
                id=item.id,
                title=item.title,
                status=item.status,
                version=item.version,
                created_at=item.created_at,
            )
            for item in task_service.list_tasks(limit=limit)
        ]

    @app.post(
        "/v1/tasks/{task_id}/proposals",
        response_model=IntentProposalResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_proposal(
        task_id: UUID,
        command: CreateProposalRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> IntentProposalResponse:
        try:
            result = governance.create_proposal(
                task_id=task_id,
                objective=command.objective,
                rationale=command.rationale,
                scope=command.scope,
                requested_capabilities=command.requested_capabilities,
                plan=[step.model_dump() for step in command.plan],
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except GovernancePayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _proposal_response(result)

    @app.get("/v1/tasks/{task_id}/proposals", response_model=list[IntentProposalResponse])
    def list_task_proposals(
        task_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> list[IntentProposalResponse]:
        return [_proposal_response(item) for item in governance.list_proposals(task_id)]

    @app.get("/v1/proposals/{proposal_id}", response_model=IntentProposalResponse)
    def get_proposal(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> IntentProposalResponse:
        try:
            return _proposal_response(governance.get_proposal(proposal_id))
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/proposals/{proposal_id}/dry-run-plan",
        response_model=ModelCallResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def dry_run_plan(
        proposal_id: UUID,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        model_calls: ModelCallService = Depends(get_model_call_service),
    ) -> ModelCallResponse:
        try:
            result = model_calls.dry_run_plan(
                proposal_id=proposal_id,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except ModelCallPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _model_call_response(result)

    @app.post(
        "/v1/proposals/{proposal_id}/live-plan/prepare",
        response_model=ModelCallResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def prepare_live_plan(
        proposal_id: UUID,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        model_calls: ModelCallService = Depends(get_model_call_service),
    ) -> ModelCallResponse:
        try:
            result = model_calls.prepare_live_plan(
                proposal_id=proposal_id,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except ModelCallPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ModelCallConflict as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _model_call_response(result)

    @app.post(
        "/v1/model-calls/{model_call_id}/execute",
        response_model=ModelCallResponse,
        status_code=status.HTTP_200_OK,
    )
    def execute_model_call(
        model_call_id: UUID,
        response: Response,
        execution_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        model_calls: ModelCallService = Depends(get_model_call_service),
    ) -> ModelCallResponse:
        try:
            result = model_calls.execute_prepared(
                model_call_id=model_call_id,
                actor_type=actor[0],
                actor_id=actor[1],
                execution_key=execution_key,
                transport=HttpxTransport(),
            )
        except ModelCallConflict as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _model_call_response(result)

    @app.post(
        "/v1/model-calls/{model_call_id}/finalize",
        response_model=ModelCallResponse,
        status_code=status.HTTP_200_OK,
    )
    def finalize_model_call(
        model_call_id: UUID,
        command: FinalizeModelCallRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        model_calls: ModelCallService = Depends(get_model_call_service),
    ) -> ModelCallResponse:
        try:
            result = model_calls.finalize_call(
                model_call_id=model_call_id,
                status=command.status,
                response_summary=command.response_summary,
                input_tokens=command.input_tokens,
                output_tokens=command.output_tokens,
                total_tokens=command.total_tokens,
                cost_usd=command.cost_usd,
                latency_ms=command.latency_ms,
                error_code=command.error_code,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except ModelCallPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ModelCallConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _model_call_response(result)

    @app.get(
        "/v1/proposals/{proposal_id}/model-calls",
        response_model=list[ModelCallResponse],
    )
    def list_model_calls(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        model_calls: ModelCallService = Depends(get_model_call_service),
    ) -> list[ModelCallResponse]:
        try:
            return [_model_call_response(item) for item in model_calls.list_calls(proposal_id)]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/model-calls/{model_call_id}/parse-plan",
        response_model=PlanDraftResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def parse_model_call_plan(
        model_call_id: UUID,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        drafts: PlanDraftService = Depends(get_plan_draft_service),
    ) -> PlanDraftResponse:
        try:
            result = drafts.parse_model_call(
                model_call_id=model_call_id,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except PlanDraftPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except PlanDraftConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _plan_draft_response(result)

    @app.get(
        "/v1/proposals/{proposal_id}/plan-drafts",
        response_model=list[PlanDraftResponse],
    )
    def list_plan_drafts(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        drafts: PlanDraftService = Depends(get_plan_draft_service),
    ) -> list[PlanDraftResponse]:
        try:
            return [_plan_draft_response(item) for item in drafts.list_drafts(proposal_id)]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.get("/v1/plan-drafts/{draft_id}", response_model=PlanDraftResponse)
    def get_plan_draft(
        draft_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        drafts: PlanDraftService = Depends(get_plan_draft_service),
    ) -> PlanDraftResponse:
        try:
            return _plan_draft_response(drafts.get_draft(draft_id))
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/plan-drafts/{draft_id}/review",
        response_model=PlanDraftResponse,
        status_code=status.HTTP_200_OK,
    )
    def review_plan_draft(
        draft_id: UUID,
        command: ReviewPlanDraftRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        drafts: PlanDraftService = Depends(get_plan_draft_service),
    ) -> PlanDraftResponse:
        try:
            result = drafts.review_draft(
                draft_id=draft_id,
                status=command.status,
                reason=command.reason,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except PlanDraftPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except PlanDraftConflict as error:
            code = (
                status.HTTP_403_FORBIDDEN
                if "only a human" in str(error)
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=code, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _plan_draft_response(result)

    @app.post(
        "/v1/plan-drafts/{draft_id}/edit",
        response_model=PlanDraftResponse,
        status_code=status.HTTP_200_OK,
    )
    def edit_plan_draft(
        draft_id: UUID,
        command: EditPlanDraftRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        drafts: PlanDraftService = Depends(get_plan_draft_service),
    ) -> PlanDraftResponse:
        try:
            result = drafts.edit_draft(
                draft_id=draft_id,
                steps=command.steps,
                reason=command.reason,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except PlanDraftPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except PlanDraftConflict as error:
            code = (
                status.HTTP_403_FORBIDDEN
                if "only a human" in str(error)
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=code, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _plan_draft_response(result)

    @app.get(
        "/v1/plan-drafts/{draft_id}/revisions",
        response_model=list[PlanDraftRevisionResponse],
    )
    def list_plan_draft_revisions(
        draft_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        drafts: PlanDraftService = Depends(get_plan_draft_service),
    ) -> list[PlanDraftRevisionResponse]:
        try:
            return [
                _plan_draft_revision_response(item) for item in drafts.list_revisions(draft_id)
            ]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/plan-drafts/{draft_id}/workflow-handoff",
        response_model=WorkflowHandoffResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def request_workflow_handoff(
        draft_id: UUID,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        drafts: PlanDraftService = Depends(get_plan_draft_service),
    ) -> WorkflowHandoffResponse:
        try:
            result = drafts.request_workflow_handoff(
                draft_id=draft_id,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except PlanDraftPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except PlanDraftConflict as error:
            code = (
                status.HTTP_403_FORBIDDEN
                if "only a human" in str(error)
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=code, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _workflow_handoff_response(result)

    @app.get(
        "/v1/proposals/{proposal_id}/workflow-handoffs",
        response_model=list[WorkflowHandoffResponse],
    )
    def list_workflow_handoffs(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        drafts: PlanDraftService = Depends(get_plan_draft_service),
    ) -> list[WorkflowHandoffResponse]:
        try:
            return [_workflow_handoff_response(item) for item in drafts.list_handoffs(proposal_id)]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/workflow-handoffs/{handoff_id}/accept",
        response_model=WorkflowRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def accept_workflow_handoff(
        handoff_id: UUID,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        workflows: WorkflowService = Depends(get_workflow_service),
    ) -> WorkflowRunResponse:
        try:
            result = workflows.accept_handoff(
                handoff_id=handoff_id,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except WorkflowPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except WorkflowConflict as error:
            code = (
                status.HTTP_403_FORBIDDEN
                if "only a human" in str(error)
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=code, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _workflow_run_response(result)

    @app.get("/v1/workflow-runs/{run_id}", response_model=WorkflowRunResponse)
    def get_workflow_run(
        run_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        workflows: WorkflowService = Depends(get_workflow_service),
    ) -> WorkflowRunResponse:
        try:
            return _workflow_run_response(workflows.get_run(run_id))
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.get(
        "/v1/proposals/{proposal_id}/workflow-runs",
        response_model=list[WorkflowRunResponse],
    )
    def list_workflow_runs(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        workflows: WorkflowService = Depends(get_workflow_service),
    ) -> list[WorkflowRunResponse]:
        return [_workflow_run_response(item) for item in workflows.list_runs(proposal_id)]

    def _workflow_transition(
        run_id: UUID,
        response: Response,
        operation: str,
        idempotency_key: str,
        actor: tuple[str, str],
        workflows: WorkflowService,
    ) -> WorkflowRunResponse:
        try:
            if operation == "advance":
                result = workflows.advance_run(
                    run_id=run_id,
                    actor_type=actor[0],
                    actor_id=actor[1],
                    idempotency_key=idempotency_key,
                )
            elif operation == "pause":
                result = workflows.pause_run(
                    run_id=run_id,
                    actor_type=actor[0],
                    actor_id=actor[1],
                    idempotency_key=idempotency_key,
                )
            else:
                result = workflows.cancel_run(
                    run_id=run_id,
                    actor_type=actor[0],
                    actor_id=actor[1],
                    idempotency_key=idempotency_key,
                )
        except WorkflowPayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except WorkflowConflict as error:
            code = (
                status.HTTP_403_FORBIDDEN
                if "only a human" in str(error)
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=code, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _workflow_run_response(result)

    @app.post(
        "/v1/workflow-runs/{run_id}/advance",
        response_model=WorkflowRunResponse,
        status_code=status.HTTP_200_OK,
    )
    def advance_workflow_run(
        run_id: UUID,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        workflows: WorkflowService = Depends(get_workflow_service),
    ) -> WorkflowRunResponse:
        return _workflow_transition(run_id, response, "advance", idempotency_key, actor, workflows)

    @app.post(
        "/v1/workflow-runs/{run_id}/pause",
        response_model=WorkflowRunResponse,
        status_code=status.HTTP_200_OK,
    )
    def pause_workflow_run(
        run_id: UUID,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        workflows: WorkflowService = Depends(get_workflow_service),
    ) -> WorkflowRunResponse:
        return _workflow_transition(run_id, response, "pause", idempotency_key, actor, workflows)

    @app.post(
        "/v1/workflow-runs/{run_id}/cancel",
        response_model=WorkflowRunResponse,
        status_code=status.HTTP_200_OK,
    )
    def cancel_workflow_run(
        run_id: UUID,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        workflows: WorkflowService = Depends(get_workflow_service),
    ) -> WorkflowRunResponse:
        return _workflow_transition(run_id, response, "cancel", idempotency_key, actor, workflows)

    @app.get(
        "/v1/proposals/{proposal_id}/model-readiness",
        response_model=ModelReadinessResponse,
    )
    def model_readiness(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        model_calls: ModelCallService = Depends(get_model_call_service),
    ) -> ModelReadinessResponse:
        try:
            return _model_readiness_response(model_calls.live_readiness(proposal_id))
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/proposals/{proposal_id}/reviews",
        response_model=ReviewDecisionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def review_proposal(
        proposal_id: UUID,
        command: ReviewRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> ReviewDecisionResponse:
        try:
            result = governance.review_proposal(
                proposal_id=proposal_id,
                decision=command.decision,
                risk_level=command.risk_level,
                reason=command.reason,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except GovernancePayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except GovernanceConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _review_response(result)

    @app.get("/v1/proposals/{proposal_id}/reviews", response_model=list[ReviewDecisionResponse])
    def list_proposal_reviews(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> list[ReviewDecisionResponse]:
        try:
            return [_review_response(item) for item in governance.list_reviews(proposal_id)]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/proposals/{proposal_id}/approvals",
        response_model=ApprovalGrantResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def grant_approval(
        proposal_id: UUID,
        command: ApprovalRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> ApprovalGrantResponse:
        try:
            result = governance.grant_approval(
                proposal_id=proposal_id,
                scope=command.scope,
                expires_at=command.expires_at,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except GovernancePayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except GovernanceConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
            ) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _approval_response(result)

    @app.get("/v1/proposals/{proposal_id}/approvals", response_model=list[ApprovalGrantResponse])
    def list_proposal_approvals(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> list[ApprovalGrantResponse]:
        try:
            return [_approval_response(item) for item in governance.list_approvals(proposal_id)]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post(
        "/v1/proposals/{proposal_id}/execution-reports",
        response_model=ExecutionReportResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def report_execution(
        proposal_id: UUID,
        command: ExecutionReportRequest,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
        actor: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> ExecutionReportResponse:
        try:
            result = governance.report_execution(
                proposal_id=proposal_id,
                status=command.status,
                summary=command.summary,
                artifacts=command.artifacts,
                actor_type=actor[0],
                actor_id=actor[1],
                idempotency_key=idempotency_key,
            )
        except GovernancePayloadMismatch as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except GovernanceConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return _report_response(result)

    @app.get(
        "/v1/proposals/{proposal_id}/execution-reports",
        response_model=list[ExecutionReportResponse],
    )
    def list_proposal_reports(
        proposal_id: UUID,
        _: tuple[str, str] = Depends(require_actor),
        governance: GovernanceService = Depends(get_governance_service),
    ) -> list[ExecutionReportResponse]:
        try:
            return [_report_response(item) for item in governance.list_reports(proposal_id)]
        except LookupError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    return app


app = create_app()
