from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dxbrain.core.models import (
    ApprovalGrant,
    AuditEvent,
    DomainEvent,
    EventOutbox,
    IntentProposal,
    ModelCall,
)
from dxbrain.providers.adapters import ChatMessage, ModelRequest, Transport
from dxbrain.providers.registry import ModelProviderRegistry


class ModelCallPayloadMismatch(ValueError):
    """A model-call idempotency key was reused with another request."""


class ModelCallConflict(ValueError):
    """A model-call lifecycle transition is not currently legal."""


@dataclass(frozen=True)
class ModelCallView:
    id: UUID
    task_id: UUID
    proposal_id: UUID | None
    role: str
    mode: str
    status: str
    provider: str
    model: str
    request_summary: dict[str, Any]
    response_summary: dict[str, Any]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None
    latency_ms: int | None
    error_code: str | None
    started_at: Any
    finished_at: Any
    created_at: Any
    replayed: bool = False


@dataclass(frozen=True)
class ModelReadinessView:
    proposal_id: UUID
    role: str
    provider: str
    model: str
    provider_configured: bool
    live_enabled: bool
    approval_present: bool
    approval_allows_model_call: bool
    allowed: bool
    reasons: list[str]


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ModelCallService:
    """Persist dry-run model planning without enabling network access."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider_registry: ModelProviderRegistry,
        *,
        live_enabled: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._provider_registry = provider_registry
        self._live_enabled = live_enabled

    @staticmethod
    def _lock(session: Session, *, actor_type: str, actor_id: str, key: str) -> None:
        lock_key = f"{actor_type}:{actor_id}:model-call:dry-run-plan:{key}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _view(row: ModelCall, *, replayed: bool = False) -> ModelCallView:
        return ModelCallView(
            id=row.id,
            task_id=row.task_id,
            proposal_id=row.proposal_id,
            role=row.role,
            mode=row.mode,
            status=row.status,
            provider=row.provider,
            model=row.model,
            request_summary=dict(row.request_summary),
            response_summary=dict(row.response_summary),
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            cost_usd=row.cost_usd,
            latency_ms=row.latency_ms,
            error_code=row.error_code,
            started_at=row.started_at,
            finished_at=row.finished_at,
            created_at=row.created_at,
            replayed=replayed,
        )

    def dry_run_plan(
        self,
        *,
        proposal_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ModelCallView:
        descriptor = self._provider_registry.resolve("planner")
        with self._session_factory.begin() as session:
            self._lock(session, actor_type=actor_type, actor_id=actor_id, key=idempotency_key)
            proposal = session.scalar(
                select(IntentProposal).where(IntentProposal.id == proposal_id).with_for_update()
            )
            if proposal is None:
                raise LookupError("proposal not found")

            payload = {
                "proposal_id": str(proposal_id),
                "mode": "dry_run",
                "role": "planner",
                "provider": descriptor.name,
                "model": descriptor.default_model,
            }
            request_hash = _payload_hash(payload)
            replay = session.scalar(
                select(ModelCall).where(
                    ModelCall.actor_type == actor_type,
                    ModelCall.actor_id == actor_id,
                    ModelCall.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise ModelCallPayloadMismatch(
                        "model-call idempotency key was reused with another payload"
                    )
                return self._view(replay, replayed=True)

            plan = [dict(step) for step in proposal.plan]
            request_summary = {
                "source": "intent_proposal",
                "proposal_id": str(proposal.id),
                "objective": proposal.objective,
                "requested_capabilities": list(proposal.requested_capabilities),
                "plan_step_count": len(plan),
            }
            response_summary = {
                "mode": "dry_run",
                "note": "No model transport was injected; proposal plan was returned unchanged.",
                "plan": plan,
            }
            call = ModelCall(
                task_id=proposal.task_id,
                proposal_id=proposal.id,
                role="planner",
                mode="dry_run",
                status="dry_run",
                provider=descriptor.name,
                model=descriptor.default_model,
                request_hash=request_hash,
                request_summary=request_summary,
                response_summary=response_summary,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                cost_usd=0.0,
                latency_ms=0,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
            session.add(call)
            session.flush()
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=proposal.task_id,
                event_type="model.planner.dry_run.v1",
                payload={
                    "model_call_id": str(call.id),
                    "proposal_id": str(proposal.id),
                    "provider": descriptor.name,
                    "model": descriptor.default_model,
                    "status": "dry_run",
                },
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="model.planner.dry_run",
                    task_id=proposal.task_id,
                    event_id=event.id,
                    detail={
                        "model_call_id": str(call.id),
                        "proposal_id": str(proposal.id),
                        "provider": descriptor.name,
                        "model": descriptor.default_model,
                        "mode": "dry_run",
                    },
                )
            )
            return self._view(call)

    def list_calls(self, proposal_id: UUID) -> list[ModelCallView]:
        with self._session_factory() as session:
            if session.get(IntentProposal, proposal_id) is None:
                raise LookupError("proposal not found")
            rows = session.scalars(
                select(ModelCall)
                .where(ModelCall.proposal_id == proposal_id)
                .order_by(ModelCall.created_at, ModelCall.id)
            ).all()
            return [self._view(row) for row in rows]

    def prepare_live_plan(
        self,
        *,
        proposal_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ModelCallView:
        """Create the durable pre-call record; this method never calls a provider."""

        descriptor = self._provider_registry.resolve("planner")
        with self._session_factory.begin() as session:
            self._lock(session, actor_type=actor_type, actor_id=actor_id, key=idempotency_key)
            proposal = session.scalar(
                select(IntentProposal).where(IntentProposal.id == proposal_id).with_for_update()
            )
            if proposal is None:
                raise LookupError("proposal not found")
            payload = {
                "proposal_id": str(proposal_id),
                "mode": "live",
                "role": "planner",
                "provider": descriptor.name,
                "model": descriptor.default_model,
            }
            request_hash = _payload_hash(payload)
            replay = session.scalar(
                select(ModelCall).where(
                    ModelCall.actor_type == actor_type,
                    ModelCall.actor_id == actor_id,
                    ModelCall.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise ModelCallPayloadMismatch(
                        "model-call idempotency key was reused with another payload"
                    )
                return self._view(replay, replayed=True)

            if not self._live_enabled:
                raise ModelCallConflict("live model calls are disabled by configuration")
            if not descriptor.configured:
                raise ModelCallConflict("planner provider has no configured API key")
            approval = session.scalar(
                select(ApprovalGrant)
                .where(
                    ApprovalGrant.proposal_id == proposal_id,
                    ApprovalGrant.status == "active",
                    ApprovalGrant.expires_at > datetime.now(UTC),
                )
                .order_by(ApprovalGrant.created_at.desc())
            )
            if approval is None:
                raise ModelCallConflict("proposal has no active approval grant")
            if "model.call" not in approval.scope.get("capabilities", []):
                raise ModelCallConflict("approval scope does not include model.call")

            call = ModelCall(
                task_id=proposal.task_id,
                proposal_id=proposal.id,
                role="planner",
                mode="live",
                status="prepared",
                provider=descriptor.name,
                model=descriptor.default_model,
                request_hash=request_hash,
                request_summary={
                    "source": "intent_proposal",
                    "proposal_id": str(proposal.id),
                    "objective": proposal.objective,
                    "requested_capabilities": list(proposal.requested_capabilities),
                    "plan_step_count": len(proposal.plan),
                },
                response_summary={
                    "state": "prepared",
                    "note": "Awaiting an explicitly authorized provider transport.",
                },
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                cost_usd=None,
                latency_ms=None,
                started_at=datetime.now(UTC),
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
            session.add(call)
            session.flush()
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=proposal.task_id,
                event_type="model.planner.prepared.v1",
                payload={
                    "model_call_id": str(call.id),
                    "proposal_id": str(proposal.id),
                    "provider": descriptor.name,
                    "model": descriptor.default_model,
                    "status": "prepared",
                },
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="model.planner.prepare",
                    task_id=proposal.task_id,
                    event_id=event.id,
                    detail={
                        "model_call_id": str(call.id),
                        "proposal_id": str(proposal.id),
                        "provider": descriptor.name,
                        "model": descriptor.default_model,
                        "mode": "live",
                        "status": "prepared",
                    },
                )
            )
            return self._view(call)

    def finalize_call(
        self,
        *,
        model_call_id: UUID,
        status: str,
        response_summary: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        cost_usd: float | None,
        latency_ms: int | None,
        error_code: str | None,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ModelCallView:
        """Finalize a prepared call after an external transport returns."""

        if status not in ("succeeded", "failed"):
            raise ValueError("model-call final status must be succeeded or failed")
        if min(input_tokens, output_tokens, total_tokens) < 0:
            raise ValueError("model-call token counts must be non-negative")
        payload = {
            "model_call_id": str(model_call_id),
            "status": status,
            "response_summary": response_summary,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "error_code": error_code,
        }
        request_hash = _payload_hash(payload)
        with self._session_factory.begin() as session:
            self._lock(
                session,
                actor_type=actor_type,
                actor_id=actor_id,
                key=f"{model_call_id}:{idempotency_key}",
            )
            call = session.scalar(
                select(ModelCall).where(ModelCall.id == model_call_id).with_for_update()
            )
            if call is None:
                raise LookupError("model call not found")
            if call.finalize_idempotency_key is not None:
                if (
                    call.finalize_idempotency_key != idempotency_key
                    or call.finalize_request_hash != request_hash
                ):
                    raise ModelCallPayloadMismatch(
                        "model-call finalize key was reused with another payload"
                    )
                return self._view(call, replayed=True)
            if call.status != "prepared":
                raise ModelCallConflict(f"cannot finalize model call in status {call.status}")
            call.status = status
            call.response_summary = response_summary
            call.input_tokens = input_tokens
            call.output_tokens = output_tokens
            call.total_tokens = total_tokens
            call.cost_usd = cost_usd
            call.latency_ms = latency_ms
            call.error_code = error_code
            call.finished_at = datetime.now(UTC)
            call.finalize_idempotency_key = idempotency_key
            call.finalize_request_hash = request_hash
            event = DomainEvent(
                aggregate_type="task",
                aggregate_id=call.task_id,
                event_type="model.planner.finalized.v1",
                payload={
                    "model_call_id": str(call.id),
                    "proposal_id": str(call.proposal_id) if call.proposal_id else None,
                    "status": status,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "cost_usd": cost_usd,
                    "error_code": error_code,
                },
            )
            session.add(event)
            session.flush()
            session.add(EventOutbox(event_id=event.id, status="pending"))
            session.add(
                AuditEvent(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    operation="model.planner.finalize",
                    task_id=call.task_id,
                    event_id=event.id,
                    detail={
                        "model_call_id": str(call.id),
                        "status": status,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                        "cost_usd": cost_usd,
                        "latency_ms": latency_ms,
                        "error_code": error_code,
                    },
                )
            )
            return self._view(call)

    def execute_prepared(
        self,
        *,
        model_call_id: UUID,
        actor_type: str,
        actor_id: str,
        execution_key: str,
        transport: Transport,
    ) -> ModelCallView:
        """Execute one prepared call and finalize it with a single external attempt.

        Unlike the other state-mutating methods on this service, the critical
        section here spans an outbound network call, so it cannot use the
        usual transaction-scoped ``_lock`` (that lock is released as soon as
        its transaction commits, which happens before the provider call would
        even start). Instead this holds a session-scoped Postgres advisory
        lock, keyed by model_call_id, for the entire duration of this method
        -- including the network call -- so a retried or concurrent
        execute_prepared() for the same model call can never both reach the
        provider before either has finalized. The lock is released when this
        method returns, whatever the outcome.
        """

        if not self._live_enabled:
            raise ModelCallConflict("live model calls are disabled by configuration")

        lock_key = f"model-call:execute:{model_call_id}"
        with self._session_factory() as session:
            session.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
            session.commit()
            try:
                call = session.scalar(
                    select(ModelCall).where(ModelCall.id == model_call_id)
                )
                if call is None:
                    raise LookupError("model call not found")
                finalize_key = f"execute:{execution_key}"
                if call.status in ("succeeded", "failed"):
                    if call.finalize_idempotency_key == finalize_key:
                        return self._view(call, replayed=True)
                    raise ModelCallConflict(
                        f"cannot execute model call in status {call.status}"
                    )
                if call.status != "prepared":
                    raise ModelCallConflict(
                        f"cannot execute model call in status {call.status}"
                    )
                if call.proposal_id is None:
                    raise ModelCallConflict("prepared model call has no proposal")
                proposal = session.get(IntentProposal, call.proposal_id)
                if proposal is None:
                    raise LookupError("proposal not found")
                adapter = self._provider_registry.adapter(call.provider)  # type: ignore[arg-type]
                request = ModelRequest(
                    model=call.model,
                    system=(
                        "You are a planning assistant. Return a concise plan in plain text. "
                        "Do not call tools or claim that any action was executed."
                    ),
                    messages=(
                        ChatMessage(
                            role="user",
                            content=json.dumps(
                                {
                                    "objective": proposal.objective,
                                    "rationale": proposal.rationale,
                                    "scope": proposal.scope,
                                    "requested_capabilities": proposal.requested_capabilities,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                    max_tokens=512,
                    temperature=0,
                )

                started = time.perf_counter()
                try:
                    result = adapter.complete(request, transport)
                except Exception as error:  # provider failures become durable failed outcomes
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    return self.finalize_call(
                        model_call_id=model_call_id,
                        status="failed",
                        response_summary={"error": "provider call failed"},
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                        cost_usd=None,
                        latency_ms=latency_ms,
                        error_code=type(error).__name__[:64],
                        actor_type=actor_type,
                        actor_id=actor_id,
                        idempotency_key=f"execute:{execution_key}",
                    )
                latency_ms = int((time.perf_counter() - started) * 1000)
                truncated_text = result.text[:20_000]
                return self.finalize_call(
                    model_call_id=model_call_id,
                    status="succeeded",
                    response_summary={
                        "text": truncated_text,
                        "truncated": len(result.text) > len(truncated_text),
                        "request_id": result.request_id,
                    },
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    total_tokens=result.usage.total_tokens,
                    cost_usd=None,
                    latency_ms=latency_ms,
                    error_code=None,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=f"execute:{execution_key}",
                )
            finally:
                session.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": lock_key},
                )
                session.commit()

    def live_readiness(self, proposal_id: UUID) -> ModelReadinessView:
        """Explain why a proposal can or cannot make a live planner call."""

        descriptor = self._provider_registry.resolve("planner")
        with self._session_factory() as session:
            proposal = session.get(IntentProposal, proposal_id)
            if proposal is None:
                raise LookupError("proposal not found")
            approval = session.scalar(
                select(ApprovalGrant)
                .where(
                    ApprovalGrant.proposal_id == proposal_id,
                    ApprovalGrant.status == "active",
                    ApprovalGrant.expires_at > datetime.now(UTC),
                )
                .order_by(ApprovalGrant.created_at.desc())
            )
            approval_present = approval is not None
            approval_allows_model_call = bool(
                approval
                and "model.call" in approval.scope.get("capabilities", [])
            )
            reasons: list[str] = []
            if not self._live_enabled:
                reasons.append("live model calls are disabled by configuration")
            if not descriptor.configured:
                reasons.append("planner provider has no configured API key")
            if not approval_present:
                reasons.append("proposal has no active approval grant")
            elif not approval_allows_model_call:
                reasons.append("approval scope does not include model.call")
            allowed = not reasons
            return ModelReadinessView(
                proposal_id=proposal_id,
                role="planner",
                provider=descriptor.name,
                model=descriptor.default_model,
                provider_configured=descriptor.configured,
                live_enabled=self._live_enabled,
                approval_present=approval_present,
                approval_allows_model_call=approval_allows_model_call,
                allowed=allowed,
                reasons=reasons,
            )
