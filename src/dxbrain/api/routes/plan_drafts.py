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


def register_plan_drafts_routes(app: FastAPI, *, require_actor, get_plan_draft_service) -> None:
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
