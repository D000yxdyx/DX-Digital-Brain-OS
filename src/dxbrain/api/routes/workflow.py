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


def register_workflow_routes(app: FastAPI, *, require_actor, get_workflow_service) -> None:
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
