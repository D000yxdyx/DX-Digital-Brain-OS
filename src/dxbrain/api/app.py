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
from dxbrain.api.routes.agents import register_agents_routes
from dxbrain.api.routes.archive import register_archive_routes
from dxbrain.api.routes.governance import register_governance_routes
from dxbrain.api.routes.model_calls import register_model_calls_routes
from dxbrain.api.routes.plan_drafts import register_plan_drafts_routes
from dxbrain.api.routes.system import register_system_routes
from dxbrain.api.routes.tasks import register_tasks_routes
from dxbrain.api.routes.workflow import register_workflow_routes


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
    register_system_routes(app, require_actor=require_actor, get_service=get_service, provider_registry=provider_registry)
    register_tasks_routes(app, require_actor=require_actor, get_service=get_service)
    register_agents_routes(app, require_actor=require_actor, get_agent_service=get_agent_service)
    register_archive_routes(app, require_actor=require_actor, get_archive_service=get_archive_service)
    register_governance_routes(app, require_actor=require_actor, get_governance_service=get_governance_service)
    register_model_calls_routes(app, require_actor=require_actor, get_model_call_service=get_model_call_service)
    register_plan_drafts_routes(app, require_actor=require_actor, get_plan_draft_service=get_plan_draft_service)
    register_workflow_routes(app, require_actor=require_actor, get_workflow_service=get_workflow_service)
    return app


app = create_app()
