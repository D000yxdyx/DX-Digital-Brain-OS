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


def register_archive_routes(app: FastAPI, *, require_actor, get_archive_service) -> None:
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
