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


def register_agents_routes(app: FastAPI, *, require_actor, get_agent_service) -> None:
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
