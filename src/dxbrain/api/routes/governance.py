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

from dxbrain.api._shared import *  # noqa: F401,F403


def register_governance_routes(app: FastAPI, *, require_actor, get_governance_service) -> None:
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
