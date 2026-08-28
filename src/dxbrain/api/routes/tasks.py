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


def register_tasks_routes(app: FastAPI, *, require_actor, get_service) -> None:
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
