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


def register_system_routes(app: FastAPI, *, require_actor, get_service, provider_registry) -> None:
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
