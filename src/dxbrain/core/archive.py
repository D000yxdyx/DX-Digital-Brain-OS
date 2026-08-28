from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import (
    AuditEvent,
    DomainEvent,
    EventOutbox,
    Lesson,
    ProjectDossier,
    Retrospective,
    Task,
)

DossierStatus = Literal["open", "closed", "cancelled"]
LessonMemoryType = Literal["episodic", "semantic", "procedural"]
LessonReviewStatus = Literal["validated", "retracted"]


class ArchivePayloadMismatch(ValueError):
    """An idempotency key was reused with another archive payload."""


class ArchiveConflict(ValueError):
    """An archive command is not legal in the current lifecycle state."""


@dataclass(frozen=True)
class DossierView:
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


@dataclass(frozen=True)
class RetrospectiveView:
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


@dataclass(frozen=True)
class LessonView:
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


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ArchiveService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lock(
        session: Session, *, actor_type: str, actor_id: str, operation: str, key: str
    ) -> None:
        lock_key = f"{actor_type}:{actor_id}:archive:{operation}:{key}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _record_event(
        session: Session,
        *,
        task_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        actor_type: str,
        actor_id: str,
        operation: str,
    ) -> DomainEvent:
        event = DomainEvent(
            aggregate_type="task", aggregate_id=task_id, event_type=event_type, payload=payload
        )
        session.add(event)
        session.flush()
        session.add(EventOutbox(event_id=event.id, status="pending"))
        session.add(
            AuditEvent(
                actor_type=actor_type,
                actor_id=actor_id,
                operation=operation,
                task_id=task_id,
                event_id=event.id,
                detail=payload,
            )
        )
        return event

    @staticmethod
    def _dossier_view(dossier: ProjectDossier, *, replayed: bool = False) -> DossierView:
        return DossierView(
            id=dossier.id,
            task_id=dossier.task_id,
            status=dossier.status,
            objective=dossier.objective,
            plan_snapshot=[dict(item) for item in dossier.plan_snapshot],
            result_summary=dossier.result_summary,
            artifacts=dict(dossier.artifacts),
            evidence=[dict(item) for item in dossier.evidence],
            created_at=dossier.created_at,
            updated_at=dossier.updated_at,
            closed_at=dossier.closed_at,
            replayed=replayed,
        )

    @staticmethod
    def _retrospective_view(
        retrospective: Retrospective, *, replayed: bool = False
    ) -> RetrospectiveView:
        return RetrospectiveView(
            id=retrospective.id,
            dossier_id=retrospective.dossier_id,
            task_id=retrospective.task_id,
            status=retrospective.status,
            expected_outcome=retrospective.expected_outcome,
            actual_outcome=retrospective.actual_outcome,
            deviations=[dict(item) for item in retrospective.deviations],
            root_causes=[dict(item) for item in retrospective.root_causes],
            keep_items=list(retrospective.keep_items),
            change_items=list(retrospective.change_items),
            stop_items=list(retrospective.stop_items),
            follow_up_items=list(retrospective.follow_up_items),
            evidence_refs=[dict(item) for item in retrospective.evidence_refs],
            created_at=retrospective.created_at,
            updated_at=retrospective.updated_at,
            replayed=replayed,
        )

    @staticmethod
    def _lesson_view(lesson: Lesson, *, replayed: bool = False) -> LessonView:
        return LessonView(
            id=lesson.id,
            retrospective_id=lesson.retrospective_id,
            task_id=lesson.task_id,
            status=lesson.status,
            memory_type=lesson.memory_type,
            statement=lesson.statement,
            applicability=dict(lesson.applicability),
            evidence_refs=[dict(item) for item in lesson.evidence_refs],
            confidence=lesson.confidence,
            created_at=lesson.created_at,
            updated_at=lesson.updated_at,
            reviewed_at=lesson.reviewed_at,
            reviewer_actor_type=lesson.reviewer_actor_type,
            reviewer_actor_id=lesson.reviewer_actor_id,
            review_reason=lesson.review_reason,
            replayed=replayed,
        )

    def create_dossier(
        self,
        *,
        task_id: UUID,
        status: DossierStatus,
        objective: str,
        plan_snapshot: list[dict[str, Any]],
        result_summary: str | None,
        artifacts: dict[str, Any],
        evidence: list[dict[str, Any]],
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> DossierView:
        payload = {
            "task_id": str(task_id),
            "status": status,
            "objective": objective,
            "plan_snapshot": plan_snapshot,
            "result_summary": result_summary,
            "artifacts": artifacts,
            "evidence": evidence,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                operation="dossier.create",
                key=idempotency_key,
            )
            replay = session.scalar(
                select(ProjectDossier).where(
                    ProjectDossier.actor_type == actor_type,
                    ProjectDossier.actor_id == actor_id,
                    ProjectDossier.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise ArchivePayloadMismatch(
                        "dossier idempotency key was reused with another payload"
                    )
                return self._dossier_view(replay, replayed=True)
            if session.get(Task, task_id) is None:
                raise LookupError("task not found")
            existing = session.scalar(
                select(ProjectDossier).where(ProjectDossier.task_id == task_id).with_for_update()
            )
            if existing is not None:
                raise ArchiveConflict("task already has a project dossier")
            now = datetime.now(UTC)
            dossier = ProjectDossier(
                task_id=task_id,
                status=status,
                objective=objective,
                plan_snapshot=plan_snapshot,
                result_summary=result_summary,
                artifacts=artifacts,
                evidence=evidence,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                closed_at=None if status == "open" else now,
            )
            session.add(dossier)
            session.flush()
            self._record_event(
                session,
                task_id=task_id,
                event_type="archive.dossier.created.v1",
                payload={"dossier_id": str(dossier.id), "status": status},
                actor_type=actor_type,
                actor_id=actor_id,
                operation="archive.dossier.create",
            )
            return self._dossier_view(dossier)

    def get_dossier(self, task_id: UUID) -> DossierView:
        with self._session_factory() as session:
            dossier = session.scalar(
                select(ProjectDossier).where(ProjectDossier.task_id == task_id)
            )
            if dossier is None:
                raise LookupError("project dossier not found")
            return self._dossier_view(dossier)

    def create_retrospective(
        self,
        *,
        dossier_id: UUID,
        expected_outcome: str,
        actual_outcome: str,
        deviations: list[dict[str, Any]],
        root_causes: list[dict[str, Any]],
        keep_items: list[str],
        change_items: list[str],
        stop_items: list[str],
        follow_up_items: list[str],
        evidence_refs: list[dict[str, Any]],
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> RetrospectiveView:
        payload = {
            "dossier_id": str(dossier_id),
            "expected_outcome": expected_outcome,
            "actual_outcome": actual_outcome,
            "deviations": deviations,
            "root_causes": root_causes,
            "keep_items": keep_items,
            "change_items": change_items,
            "stop_items": stop_items,
            "follow_up_items": follow_up_items,
            "evidence_refs": evidence_refs,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                operation="retrospective.create",
                key=f"{dossier_id}:{idempotency_key}",
            )
            replay = session.scalar(
                select(Retrospective).where(
                    Retrospective.dossier_id == dossier_id,
                    Retrospective.actor_type == actor_type,
                    Retrospective.actor_id == actor_id,
                    Retrospective.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise ArchivePayloadMismatch(
                        "retrospective idempotency key was reused with another payload"
                    )
                return self._retrospective_view(replay, replayed=True)
            dossier = session.scalar(
                select(ProjectDossier).where(ProjectDossier.id == dossier_id).with_for_update()
            )
            if dossier is None:
                raise LookupError("project dossier not found")
            if dossier.status not in ("closed", "cancelled"):
                raise ArchiveConflict("dossier must be closed before retrospective")
            retrospective = Retrospective(
                dossier_id=dossier.id,
                task_id=dossier.task_id,
                status="draft",
                expected_outcome=expected_outcome,
                actual_outcome=actual_outcome,
                deviations=deviations,
                root_causes=root_causes,
                keep_items=keep_items,
                change_items=change_items,
                stop_items=stop_items,
                follow_up_items=follow_up_items,
                evidence_refs=evidence_refs,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(retrospective)
            session.flush()
            self._record_event(
                session,
                task_id=dossier.task_id,
                event_type="archive.retrospective.created.v1",
                payload={
                    "dossier_id": str(dossier.id),
                    "retrospective_id": str(retrospective.id),
                    "status": retrospective.status,
                },
                actor_type=actor_type,
                actor_id=actor_id,
                operation="archive.retrospective.create",
            )
            return self._retrospective_view(retrospective)

    def list_retrospectives(self, dossier_id: UUID) -> list[RetrospectiveView]:
        with self._session_factory() as session:
            if session.get(ProjectDossier, dossier_id) is None:
                raise LookupError("project dossier not found")
            rows = session.scalars(
                select(Retrospective)
                .where(Retrospective.dossier_id == dossier_id)
                .order_by(Retrospective.created_at, Retrospective.id)
            ).all()
            return [self._retrospective_view(row) for row in rows]

    def create_lesson(
        self,
        *,
        retrospective_id: UUID,
        memory_type: LessonMemoryType,
        statement: str,
        applicability: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
        confidence: float,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> LessonView:
        payload = {
            "retrospective_id": str(retrospective_id),
            "memory_type": memory_type,
            "statement": statement,
            "applicability": applicability,
            "evidence_refs": evidence_refs,
            "confidence": confidence,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                operation="lesson.create",
                key=f"{retrospective_id}:{idempotency_key}",
            )
            replay = session.scalar(
                select(Lesson).where(
                    Lesson.retrospective_id == retrospective_id,
                    Lesson.actor_type == actor_type,
                    Lesson.actor_id == actor_id,
                    Lesson.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise ArchivePayloadMismatch(
                        "lesson idempotency key was reused with another payload"
                    )
                return self._lesson_view(replay, replayed=True)
            retrospective = session.scalar(
                select(Retrospective).where(Retrospective.id == retrospective_id).with_for_update()
            )
            if retrospective is None:
                raise LookupError("retrospective not found")
            if retrospective.status == "retracted":
                raise ArchiveConflict("cannot create lesson from a retracted retrospective")
            lesson = Lesson(
                retrospective_id=retrospective.id,
                task_id=retrospective.task_id,
                status="candidate",
                memory_type=memory_type,
                statement=statement,
                applicability=applicability,
                evidence_refs=evidence_refs,
                confidence=confidence,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(lesson)
            session.flush()
            self._record_event(
                session,
                task_id=retrospective.task_id,
                event_type="archive.lesson.candidate_created.v1",
                payload={
                    "retrospective_id": str(retrospective.id),
                    "lesson_id": str(lesson.id),
                    "status": lesson.status,
                },
                actor_type=actor_type,
                actor_id=actor_id,
                operation="archive.lesson.create",
            )
            return self._lesson_view(lesson)

    def list_lessons(self, retrospective_id: UUID) -> list[LessonView]:
        with self._session_factory() as session:
            if session.get(Retrospective, retrospective_id) is None:
                raise LookupError("retrospective not found")
            rows = session.scalars(
                select(Lesson)
                .where(Lesson.retrospective_id == retrospective_id)
                .order_by(Lesson.created_at, Lesson.id)
            ).all()
            return [self._lesson_view(row) for row in rows]

    def review_lesson(
        self,
        *,
        lesson_id: UUID,
        status: LessonReviewStatus,
        reason: str,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> LessonView:
        if actor_type != "human":
            raise ArchiveConflict("only a human actor may validate or retract a lesson")
        payload = {"lesson_id": str(lesson_id), "status": status, "reason": reason}
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                operation="lesson.review",
                key=f"{lesson_id}:{idempotency_key}",
            )
            lesson = session.scalar(select(Lesson).where(Lesson.id == lesson_id).with_for_update())
            if lesson is None:
                raise LookupError("lesson not found")
            if lesson.review_idempotency_key is not None:
                if (
                    lesson.review_idempotency_key != idempotency_key
                    or lesson.review_request_hash != request_hash
                ):
                    raise ArchiveConflict("lesson has already been reviewed")
                return self._lesson_view(lesson, replayed=True)
            if lesson.status != "candidate":
                raise ArchiveConflict(f"cannot review lesson in status {lesson.status}")
            lesson.status = status
            lesson.reviewed_at = datetime.now(UTC)
            lesson.reviewer_actor_type = actor_type
            lesson.reviewer_actor_id = actor_id
            lesson.review_reason = reason
            lesson.review_idempotency_key = idempotency_key
            lesson.review_request_hash = request_hash
            session.flush()
            self._record_event(
                session,
                task_id=lesson.task_id,
                event_type="archive.lesson.reviewed.v1",
                payload={"lesson_id": str(lesson.id), "status": status},
                actor_type=actor_type,
                actor_id=actor_id,
                operation="archive.lesson.review",
            )
            return self._lesson_view(lesson)
