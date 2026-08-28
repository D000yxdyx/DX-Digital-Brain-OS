from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import (
    AuditEvent,
    DomainEvent,
    EventOutbox,
    IntentProposal,
    ModelCall,
    PlanDraft,
    PlanDraftRevision,
    WorkflowHandoff,
)

PARSER_VERSION = "markdown-list-v1"
_NUMBERED = re.compile(r"^\s*\d+[\.\)、]\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


class PlanDraftPayloadMismatch(ValueError):
    """A plan-draft idempotency key was reused with another request."""


class PlanDraftConflict(ValueError):
    """A plan-draft lifecycle transition is not currently legal."""


@dataclass(frozen=True)
class ParsedPlan:
    parser_version: str
    source_hash: str
    steps: list[dict[str, Any]]
    warnings: list[str]


@dataclass(frozen=True)
class PlanDraftView:
    id: UUID
    task_id: UUID
    proposal_id: UUID
    model_call_id: UUID
    status: str
    revision: int
    parser_version: str
    source_hash: str
    steps: list[dict[str, Any]]
    warnings: list[str]
    created_at: Any
    reviewed_at: Any
    reviewer_actor_type: str | None
    reviewer_actor_id: str | None
    review_reason: str | None
    replayed: bool = False


@dataclass(frozen=True)
class PlanDraftRevisionView:
    id: UUID
    draft_id: UUID
    revision: int
    steps: list[dict[str, Any]]
    reason: str
    actor_type: str
    actor_id: str
    created_at: Any


@dataclass(frozen=True)
class WorkflowHandoffView:
    id: UUID
    task_id: UUID
    proposal_id: UUID
    draft_id: UUID
    draft_revision: int
    status: str
    actor_type: str
    actor_id: str
    created_at: Any
    replayed: bool = False


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _clean_title(value: str) -> str:
    value = re.sub(r"(```|```|\*\*|__|`)", "", value).strip()
    value = re.sub(r"\s+", " ", value)
    return value.rstrip("。；;，,")[:500]


def parse_plan_text(text_value: str) -> ParsedPlan:
    """Parse a model's plain-text list without inferring executable actions."""

    source_hash = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    for line_number, line in enumerate(text_value.splitlines(), start=1):
        match = _NUMBERED.match(line) or _BULLET.match(line)
        if match is None:
            continue
        title = _clean_title(match.group(1))
        if not title:
            continue
        if len(steps) >= 32:
            warnings.append("plan contains more than 32 list items; remaining items were ignored")
            break
        steps.append(
            {
                "ordinal": len(steps) + 1,
                "title": title,
                "capability": "workflow.plan",
                "parameters": {"source_line": line_number},
            }
        )
    if not steps:
        warnings.append("no numbered or bullet list items were found")
    return ParsedPlan(
        parser_version=PARSER_VERSION,
        source_hash=source_hash,
        steps=steps,
        warnings=warnings,
    )


class PlanDraftService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lock(session: Session, *, actor_type: str, actor_id: str, key: str) -> None:
        lock_key = f"{actor_type}:{actor_id}:plan-draft:{key}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _view(row: PlanDraft, *, replayed: bool = False) -> PlanDraftView:
        return PlanDraftView(
            id=row.id,
            task_id=row.task_id,
            proposal_id=row.proposal_id,
            model_call_id=row.model_call_id,
            status=row.status,
            revision=row.revision,
            parser_version=row.parser_version,
            source_hash=row.source_hash,
            steps=[dict(step) for step in row.steps],
            warnings=list(row.warnings),
            created_at=row.created_at,
            reviewed_at=row.reviewed_at,
            reviewer_actor_type=row.reviewer_actor_type,
            reviewer_actor_id=row.reviewer_actor_id,
            review_reason=row.review_reason,
            replayed=replayed,
        )

    @staticmethod
    def _revision_view(row: PlanDraftRevision) -> PlanDraftRevisionView:
        return PlanDraftRevisionView(
            id=row.id,
            draft_id=row.draft_id,
            revision=row.revision,
            steps=[dict(step) for step in row.steps],
            reason=row.reason,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            created_at=row.created_at,
        )

    @staticmethod
    def _handoff_view(row: WorkflowHandoff, *, replayed: bool = False) -> WorkflowHandoffView:
        return WorkflowHandoffView(
            id=row.id,
            task_id=row.task_id,
            proposal_id=row.proposal_id,
            draft_id=row.draft_id,
            draft_revision=row.draft_revision,
            status=row.status,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            created_at=row.created_at,
            replayed=replayed,
        )

    def parse_model_call(
        self,
        *,
        model_call_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> PlanDraftView:
        with self._session_factory.begin() as session:
            self._lock(session, actor_type=actor_type, actor_id=actor_id, key=idempotency_key)
            call = session.scalar(
                select(ModelCall).where(ModelCall.id == model_call_id).with_for_update()
            )
            if call is None:
                raise LookupError("model call not found")
            text_value = call.response_summary.get("text")
            if call.status != "succeeded":
                raise PlanDraftConflict(f"cannot parse model call in status {call.status}")
            if not isinstance(text_value, str):
                raise PlanDraftConflict("succeeded model call has no textual response")
            parsed = parse_plan_text(text_value)
            payload = {
                "model_call_id": str(model_call_id),
                "parser_version": parsed.parser_version,
                "source_hash": parsed.source_hash,
            }
            request_hash = _payload_hash(payload)
            replay = session.scalar(
                select(PlanDraft).where(
                    PlanDraft.actor_type == actor_type,
                    PlanDraft.actor_id == actor_id,
                    PlanDraft.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise PlanDraftPayloadMismatch(
                        "plan-draft idempotency key was reused with another payload"
                    )
                return self._view(replay, replayed=True)
            if call.proposal_id is None:
                raise PlanDraftConflict("model call has no proposal")
            draft = PlanDraft(
                task_id=call.task_id,
                proposal_id=call.proposal_id,
                model_call_id=call.id,
                status="candidate",
                parser_version=parsed.parser_version,
                source_hash=parsed.source_hash,
                steps=parsed.steps,
                warnings=parsed.warnings,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(draft)
            session.flush()
            session.add(
                PlanDraftRevision(
                    draft_id=draft.id,
                    revision=1,
                    steps=parsed.steps,
                    reason="initial deterministic parse",
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=f"parse:{idempotency_key}",
                    request_hash=request_hash,
                )
            )
            detail = {
                "draft_id": str(draft.id),
                "model_call_id": str(call.id),
                "proposal_id": str(call.proposal_id),
                "step_count": len(parsed.steps),
                "source_hash": parsed.source_hash,
                "status": "candidate",
            }
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=call.task_id,
                event_type="plan.draft.parsed.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="plan.draft.parse",
                    task_id=call.task_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._view(draft)

    def review_draft(
        self,
        *,
        draft_id: UUID,
        status: str,
        reason: str,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> PlanDraftView:
        if status not in ("confirmed", "rejected"):
            raise ValueError("plan-draft review status must be confirmed or rejected")
        if actor_type != "human":
            raise PlanDraftConflict("only a human actor may review a plan draft")
        payload = {"draft_id": str(draft_id), "status": status, "reason": reason}
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                key=f"review:{draft_id}:{idempotency_key}",
            )
            draft = session.scalar(
                select(PlanDraft).where(PlanDraft.id == draft_id).with_for_update()
            )
            if draft is None:
                raise LookupError("plan draft not found")
            if draft.review_idempotency_key is not None:
                if (
                    draft.review_idempotency_key != idempotency_key
                    or draft.review_request_hash != request_hash
                ):
                    raise PlanDraftPayloadMismatch(
                        "plan-draft review key was reused with another payload"
                    )
                return self._view(draft, replayed=True)
            if draft.status != "candidate":
                raise PlanDraftConflict(f"cannot review plan draft in status {draft.status}")
            draft.status = status
            draft.reviewed_at = datetime.now(UTC)
            draft.reviewer_actor_type = actor_type
            draft.reviewer_actor_id = actor_id
            draft.review_reason = reason
            draft.review_idempotency_key = idempotency_key
            draft.review_request_hash = request_hash
            detail = {
                "draft_id": str(draft.id),
                "proposal_id": str(draft.proposal_id),
                "status": status,
                "reason": reason,
            }
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=draft.task_id,
                event_type="plan.draft.reviewed.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="plan.draft.review",
                    task_id=draft.task_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._view(draft)

    def get_draft(self, draft_id: UUID) -> PlanDraftView:
        with self._session_factory() as session:
            row = session.get(PlanDraft, draft_id)
            if row is None:
                raise LookupError("plan draft not found")
            return self._view(row)

    def edit_draft(
        self,
        *,
        draft_id: UUID,
        steps: list[dict[str, Any]],
        reason: str,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> PlanDraftView:
        if actor_type != "human":
            raise PlanDraftConflict("only a human actor may edit a plan draft")
        if not steps or len(steps) > 64:
            raise ValueError("plan draft steps must contain between 1 and 64 items")
        payload = {"draft_id": str(draft_id), "steps": steps, "reason": reason}
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                key=f"edit:{draft_id}:{idempotency_key}",
            )
            replay = session.scalar(
                select(PlanDraftRevision).where(
                    PlanDraftRevision.actor_type == actor_type,
                    PlanDraftRevision.actor_id == actor_id,
                    PlanDraftRevision.idempotency_key == idempotency_key,
                )
            )
            draft = session.scalar(
                select(PlanDraft).where(PlanDraft.id == draft_id).with_for_update()
            )
            if draft is None:
                raise LookupError("plan draft not found")
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise PlanDraftPayloadMismatch(
                        "plan-draft revision key was reused with another payload"
                    )
                return self._view(draft, replayed=True)
            if draft.status != "candidate":
                raise PlanDraftConflict(f"cannot edit plan draft in status {draft.status}")
            next_revision = draft.revision + 1
            revision = PlanDraftRevision(
                draft_id=draft.id,
                revision=next_revision,
                steps=steps,
                reason=reason,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(revision)
            draft.revision = next_revision
            draft.steps = steps
            session.flush()
            detail = {
                "draft_id": str(draft.id),
                "proposal_id": str(draft.proposal_id),
                "revision": next_revision,
                "step_count": len(steps),
                "reason": reason,
            }
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=draft.task_id,
                event_type="plan.draft.edited.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="plan.draft.edit",
                    task_id=draft.task_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._view(draft)

    def list_revisions(self, draft_id: UUID) -> list[PlanDraftRevisionView]:
        with self._session_factory() as session:
            if session.get(PlanDraft, draft_id) is None:
                raise LookupError("plan draft not found")
            rows = session.scalars(
                select(PlanDraftRevision)
                .where(PlanDraftRevision.draft_id == draft_id)
                .order_by(PlanDraftRevision.revision)
            ).all()
            return [self._revision_view(row) for row in rows]

    def request_workflow_handoff(
        self,
        *,
        draft_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkflowHandoffView:
        if actor_type != "human":
            raise PlanDraftConflict("only a human actor may request a workflow handoff")
        payload = {"draft_id": str(draft_id), "operation": "workflow_handoff.request"}
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                key=f"handoff:{draft_id}:{idempotency_key}",
            )
            replay = session.scalar(
                select(WorkflowHandoff).where(
                    WorkflowHandoff.actor_type == actor_type,
                    WorkflowHandoff.actor_id == actor_id,
                    WorkflowHandoff.idempotency_key == idempotency_key,
                )
            )
            draft = session.scalar(
                select(PlanDraft).where(PlanDraft.id == draft_id).with_for_update()
            )
            if draft is None:
                raise LookupError("plan draft not found")
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise PlanDraftPayloadMismatch(
                        "workflow handoff key was reused with another payload"
                    )
                return self._handoff_view(replay, replayed=True)
            if draft.status != "confirmed":
                raise PlanDraftConflict("only a confirmed plan draft may request workflow handoff")
            existing = session.scalar(
                select(WorkflowHandoff).where(
                    WorkflowHandoff.draft_id == draft.id,
                    WorkflowHandoff.draft_revision == draft.revision,
                )
            )
            if existing is not None:
                raise PlanDraftConflict("workflow handoff already exists for this draft revision")
            handoff = WorkflowHandoff(
                task_id=draft.task_id,
                proposal_id=draft.proposal_id,
                draft_id=draft.id,
                draft_revision=draft.revision,
                status="requested",
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(handoff)
            session.flush()
            detail = {
                "handoff_id": str(handoff.id),
                "draft_id": str(draft.id),
                "proposal_id": str(draft.proposal_id),
                "draft_revision": draft.revision,
                "status": "requested",
            }
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=draft.task_id,
                event_type="workflow.handoff.requested.v1",
                payload=detail,
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="workflow.handoff.request",
                    task_id=draft.task_id,
                    event_id=event.id,
                    detail=detail,
                )
            )
            return self._handoff_view(handoff)

    def list_handoffs(self, proposal_id: UUID) -> list[WorkflowHandoffView]:
        with self._session_factory() as session:
            if session.get(IntentProposal, proposal_id) is None:
                raise LookupError("proposal not found")
            rows = session.scalars(
                select(WorkflowHandoff)
                .where(WorkflowHandoff.proposal_id == proposal_id)
                .order_by(WorkflowHandoff.created_at, WorkflowHandoff.id)
            ).all()
            return [self._handoff_view(row) for row in rows]

    def list_drafts(self, proposal_id: UUID) -> list[PlanDraftView]:
        with self._session_factory() as session:
            if session.get(IntentProposal, proposal_id) is None:
                raise LookupError("proposal not found")
            rows = session.scalars(
                select(PlanDraft)
                .where(PlanDraft.proposal_id == proposal_id)
                .order_by(PlanDraft.created_at, PlanDraft.id)
            ).all()
            return [self._view(row) for row in rows]
