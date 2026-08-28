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
    ApprovalGrant,
    AuditEvent,
    DomainEvent,
    EventOutbox,
    ExecutionReport,
    IntentProposal,
    ReviewDecision,
    Task,
)

ProposalStatus = Literal[
    "proposed",
    "awaiting_input",
    "approved",
    "rejected",
    "executing",
    "completed",
    "failed",
    "cancelled",
]
ReviewChoice = Literal["approve", "reject", "request_input"]
RiskLevel = Literal["low", "medium", "high"]
ReportStatus = Literal["started", "succeeded", "failed", "cancelled"]


class GovernancePayloadMismatch(ValueError):
    """An idempotency key was reused with another governance payload."""


class GovernanceConflict(ValueError):
    """A governance command is not legal in the proposal's current state."""


@dataclass(frozen=True)
class IntentProposalView:
    id: UUID
    task_id: UUID
    revision: int
    status: str
    objective: str
    rationale: str
    scope: dict[str, Any]
    requested_capabilities: list[str]
    plan: list[dict[str, Any]]
    created_at: datetime
    replayed: bool = False


@dataclass(frozen=True)
class ReviewDecisionView:
    id: UUID
    proposal_id: UUID
    task_id: UUID
    decision: str
    risk_level: str
    reason: str
    created_at: datetime
    proposal_status: str
    replayed: bool = False


@dataclass(frozen=True)
class ApprovalGrantView:
    id: UUID
    proposal_id: UUID
    task_id: UUID
    scope: dict[str, Any]
    status: str
    expires_at: datetime
    created_at: datetime
    replayed: bool = False


@dataclass(frozen=True)
class ExecutionReportView:
    id: UUID
    proposal_id: UUID
    task_id: UUID
    status: str
    summary: str
    artifacts: dict[str, Any]
    created_at: datetime
    proposal_status: str
    replayed: bool = False


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC)


class GovernanceService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lock(
        session: Session, *, actor_type: str, actor_id: str, operation: str, key: str
    ) -> None:
        lock_key = f"{actor_type}:{actor_id}:governance:{operation}:{key}"
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
            aggregate_type="task",
            aggregate_id=task_id,
            event_type=event_type,
            payload=payload,
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
    def _proposal_view(proposal: IntentProposal, *, replayed: bool = False) -> IntentProposalView:
        return IntentProposalView(
            id=proposal.id,
            task_id=proposal.task_id,
            revision=proposal.revision,
            status=proposal.status,
            objective=proposal.objective,
            rationale=proposal.rationale,
            scope=dict(proposal.scope),
            requested_capabilities=list(proposal.requested_capabilities),
            plan=[dict(step) for step in proposal.plan],
            created_at=proposal.created_at,
            replayed=replayed,
        )

    def create_proposal(
        self,
        *,
        task_id: UUID,
        objective: str,
        rationale: str,
        scope: dict[str, Any],
        requested_capabilities: list[str],
        plan: list[dict[str, Any]],
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> IntentProposalView:
        payload = {
            "task_id": str(task_id),
            "objective": objective,
            "rationale": rationale,
            "scope": scope,
            "requested_capabilities": requested_capabilities,
            "plan": plan,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                operation="proposal.create",
                key=idempotency_key,
            )
            replay = session.scalar(
                select(IntentProposal).where(
                    IntentProposal.actor_type == actor_type,
                    IntentProposal.actor_id == actor_id,
                    IntentProposal.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise GovernancePayloadMismatch(
                        "proposal idempotency key was reused with another payload"
                    )
                return self._proposal_view(replay, replayed=True)

            if session.get(Task, task_id) is None:
                raise LookupError("task not found")
            proposal = IntentProposal(
                task_id=task_id,
                revision=1,
                status="proposed",
                objective=objective,
                rationale=rationale,
                scope=scope,
                requested_capabilities=requested_capabilities,
                plan=plan,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(proposal)
            session.flush()
            self._record_event(
                session,
                task_id=task_id,
                event_type="governance.proposal.created.v1",
                payload={"proposal_id": str(proposal.id), "revision": proposal.revision},
                actor_type=actor_type,
                actor_id=actor_id,
                operation="governance.proposal.create",
            )
            return self._proposal_view(proposal)

    def review_proposal(
        self,
        *,
        proposal_id: UUID,
        decision: ReviewChoice,
        risk_level: RiskLevel,
        reason: str,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ReviewDecisionView:
        payload = {
            "proposal_id": str(proposal_id),
            "decision": decision,
            "risk_level": risk_level,
            "reason": reason,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                operation="proposal.review",
                key=f"{proposal_id}:{idempotency_key}",
            )
            replay = session.scalar(
                select(ReviewDecision).where(
                    ReviewDecision.proposal_id == proposal_id,
                    ReviewDecision.actor_type == actor_type,
                    ReviewDecision.actor_id == actor_id,
                    ReviewDecision.idempotency_key == idempotency_key,
                )
            )
            proposal = session.scalar(
                select(IntentProposal).where(IntentProposal.id == proposal_id).with_for_update()
            )
            if proposal is None:
                raise LookupError("proposal not found")
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise GovernancePayloadMismatch(
                        "review idempotency key was reused with another payload"
                    )
                return ReviewDecisionView(
                    replay.id,
                    replay.proposal_id,
                    replay.task_id,
                    replay.decision,
                    replay.risk_level,
                    replay.reason,
                    replay.created_at,
                    proposal.status,
                    replayed=True,
                )
            if proposal.status not in ("proposed", "awaiting_input"):
                raise GovernanceConflict(f"cannot review proposal in status {proposal.status}")

            next_status: ProposalStatus = {
                "approve": "approved",
                "reject": "rejected",
                "request_input": "awaiting_input",
            }[decision]
            proposal.status = next_status
            review = ReviewDecision(
                proposal_id=proposal.id,
                task_id=proposal.task_id,
                decision=decision,
                risk_level=risk_level,
                reason=reason,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(review)
            session.flush()
            self._record_event(
                session,
                task_id=proposal.task_id,
                event_type="governance.review.decided.v1",
                payload={
                    "proposal_id": str(proposal.id),
                    "review_id": str(review.id),
                    "decision": decision,
                    "risk_level": risk_level,
                    "proposal_status": next_status,
                },
                actor_type=actor_type,
                actor_id=actor_id,
                operation="governance.proposal.review",
            )
            return ReviewDecisionView(
                review.id,
                review.proposal_id,
                review.task_id,
                review.decision,
                review.risk_level,
                review.reason,
                review.created_at,
                next_status,
            )

    def grant_approval(
        self,
        *,
        proposal_id: UUID,
        scope: dict[str, Any],
        expires_at: datetime,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ApprovalGrantView:
        expires_at = _require_aware(expires_at)
        if expires_at <= datetime.now(UTC):
            raise ValueError("approval expiry must be in the future")
        if not scope:
            raise ValueError("approval scope must not be empty")
        payload = {
            "proposal_id": str(proposal_id),
            "scope": scope,
            "expires_at": expires_at.isoformat(),
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                operation="approval.grant",
                key=f"{proposal_id}:{idempotency_key}",
            )
            replay = session.scalar(
                select(ApprovalGrant).where(
                    ApprovalGrant.proposal_id == proposal_id,
                    ApprovalGrant.actor_type == actor_type,
                    ApprovalGrant.actor_id == actor_id,
                    ApprovalGrant.idempotency_key == idempotency_key,
                )
            )
            proposal = session.scalar(
                select(IntentProposal).where(IntentProposal.id == proposal_id).with_for_update()
            )
            if proposal is None:
                raise LookupError("proposal not found")
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise GovernancePayloadMismatch(
                        "approval idempotency key was reused with another payload"
                    )
                return self._approval_view(replay, replayed=True)
            if proposal.status != "approved":
                raise GovernanceConflict(
                    f"cannot grant approval for proposal in status {proposal.status}"
                )
            grant = ApprovalGrant(
                proposal_id=proposal.id,
                task_id=proposal.task_id,
                scope=scope,
                status="active",
                expires_at=expires_at,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(grant)
            session.flush()
            self._record_event(
                session,
                task_id=proposal.task_id,
                event_type="governance.approval.granted.v1",
                payload={
                    "proposal_id": str(proposal.id),
                    "approval_id": str(grant.id),
                    "expires_at": expires_at.isoformat(),
                },
                actor_type=actor_type,
                actor_id=actor_id,
                operation="governance.approval.grant",
            )
            return self._approval_view(grant)

    def report_execution(
        self,
        *,
        proposal_id: UUID,
        status: ReportStatus,
        summary: str,
        artifacts: dict[str, Any],
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ExecutionReportView:
        payload = {
            "proposal_id": str(proposal_id),
            "status": status,
            "summary": summary,
            "artifacts": artifacts,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                operation="execution.report",
                key=f"{proposal_id}:{idempotency_key}",
            )
            replay = session.scalar(
                select(ExecutionReport).where(
                    ExecutionReport.proposal_id == proposal_id,
                    ExecutionReport.actor_type == actor_type,
                    ExecutionReport.actor_id == actor_id,
                    ExecutionReport.idempotency_key == idempotency_key,
                )
            )
            proposal = session.scalar(
                select(IntentProposal).where(IntentProposal.id == proposal_id).with_for_update()
            )
            if proposal is None:
                raise LookupError("proposal not found")
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise GovernancePayloadMismatch(
                        "execution idempotency key was reused with another payload"
                    )
                return ExecutionReportView(
                    replay.id,
                    replay.proposal_id,
                    replay.task_id,
                    replay.status,
                    replay.summary,
                    dict(replay.artifacts),
                    replay.created_at,
                    proposal.status,
                    replayed=True,
                )
            if status == "started":
                if proposal.status not in ("approved", "executing"):
                    raise GovernanceConflict(f"cannot start execution in status {proposal.status}")
                proposal.status = "executing"
            else:
                if proposal.status not in ("approved", "executing"):
                    raise GovernanceConflict(f"cannot finish execution in status {proposal.status}")
                proposal.status = {
                    "succeeded": "completed",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }[status]
            report = ExecutionReport(
                proposal_id=proposal.id,
                task_id=proposal.task_id,
                status=status,
                summary=summary,
                artifacts=artifacts,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            session.add(report)
            session.flush()
            self._record_event(
                session,
                task_id=proposal.task_id,
                event_type="governance.execution.reported.v1",
                payload={
                    "proposal_id": str(proposal.id),
                    "report_id": str(report.id),
                    "report_status": status,
                    "proposal_status": proposal.status,
                },
                actor_type=actor_type,
                actor_id=actor_id,
                operation="governance.execution.report",
            )
            return ExecutionReportView(
                report.id,
                report.proposal_id,
                report.task_id,
                report.status,
                report.summary,
                dict(report.artifacts),
                report.created_at,
                proposal.status,
            )

    def list_proposals(self, task_id: UUID) -> list[IntentProposalView]:
        with self._session_factory() as session:
            proposals = session.scalars(
                select(IntentProposal)
                .where(IntentProposal.task_id == task_id)
                .order_by(IntentProposal.created_at.desc(), IntentProposal.id.desc())
            ).all()
            return [self._proposal_view(item) for item in proposals]

    def get_proposal(self, proposal_id: UUID) -> IntentProposalView:
        with self._session_factory() as session:
            proposal = session.get(IntentProposal, proposal_id)
            if proposal is None:
                raise LookupError("proposal not found")
            return self._proposal_view(proposal)

    def list_reviews(self, proposal_id: UUID) -> list[ReviewDecisionView]:
        with self._session_factory() as session:
            proposal = session.get(IntentProposal, proposal_id)
            if proposal is None:
                raise LookupError("proposal not found")
            reviews = session.scalars(
                select(ReviewDecision)
                .where(ReviewDecision.proposal_id == proposal_id)
                .order_by(ReviewDecision.created_at)
            ).all()
            return [
                ReviewDecisionView(
                    item.id,
                    item.proposal_id,
                    item.task_id,
                    item.decision,
                    item.risk_level,
                    item.reason,
                    item.created_at,
                    proposal.status,
                )
                for item in reviews
            ]

    def list_approvals(self, proposal_id: UUID) -> list[ApprovalGrantView]:
        with self._session_factory() as session:
            if session.get(IntentProposal, proposal_id) is None:
                raise LookupError("proposal not found")
            grants = session.scalars(
                select(ApprovalGrant)
                .where(ApprovalGrant.proposal_id == proposal_id)
                .order_by(ApprovalGrant.created_at)
            ).all()
            return [self._approval_view(item) for item in grants]

    def list_reports(self, proposal_id: UUID) -> list[ExecutionReportView]:
        with self._session_factory() as session:
            proposal = session.get(IntentProposal, proposal_id)
            if proposal is None:
                raise LookupError("proposal not found")
            reports = session.scalars(
                select(ExecutionReport)
                .where(ExecutionReport.proposal_id == proposal_id)
                .order_by(ExecutionReport.created_at)
            ).all()
            return [
                ExecutionReportView(
                    item.id,
                    item.proposal_id,
                    item.task_id,
                    item.status,
                    item.summary,
                    dict(item.artifacts),
                    item.created_at,
                    proposal.status,
                )
                for item in reports
            ]

    @staticmethod
    def _approval_view(grant: ApprovalGrant, *, replayed: bool = False) -> ApprovalGrantView:
        effective_status = grant.status
        if effective_status == "active" and grant.expires_at <= datetime.now(UTC):
            effective_status = "expired"
        return ApprovalGrantView(
            grant.id,
            grant.proposal_id,
            grant.task_id,
            dict(grant.scope),
            effective_status,
            grant.expires_at,
            grant.created_at,
            replayed=replayed,
        )
