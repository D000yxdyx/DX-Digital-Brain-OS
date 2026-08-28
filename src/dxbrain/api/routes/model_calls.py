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


def register_model_calls_routes(app: FastAPI, *, require_actor, get_model_call_service) -> None:
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
