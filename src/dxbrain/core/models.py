from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = {"schema": "dx_core"}

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntentProposal(Base):
    __tablename__ = "intent_proposals"
    __table_args__ = (
        UniqueConstraint(
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_proposal_idempotency",
        ),
        CheckConstraint("revision >= 1", name="ck_proposal_revision_positive"),
        CheckConstraint(
            "status IN ("
            "'proposed', 'awaiting_input', 'approved', 'rejected', "
            "'executing', 'completed', 'failed', 'cancelled')",
            name="ck_proposal_status",
        ),
        {"schema": "dx_governance"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    objective: Mapped[str] = mapped_column(String(240), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requested_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    plan: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_review_idempotency",
        ),
        CheckConstraint(
            "decision IN ('approve', 'reject', 'request_input')",
            name="ck_review_decision",
        ),
        CheckConstraint("risk_level IN ('low', 'medium', 'high')", name="ck_review_risk_level"),
        {"schema": "dx_governance"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.intent_proposals.id"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalGrant(Base):
    __tablename__ = "approval_grants"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_approval_idempotency",
        ),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_approval_status"),
        {"schema": "dx_governance"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.intent_proposals.id"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExecutionReport(Base):
    __tablename__ = "execution_reports"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_execution_report_idempotency",
        ),
        CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'cancelled')",
            name="ck_execution_report_status",
        ),
        {"schema": "dx_governance"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.intent_proposals.id"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    artifacts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_model_call_idempotency"
        ),
        CheckConstraint("mode IN ('dry_run', 'live')", name="ck_model_call_mode"),
        CheckConstraint(
            "status IN ('prepared', 'dry_run', 'succeeded', 'failed', 'blocked')",
            name="ck_model_call_status",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0",
            name="ck_model_call_tokens_nonnegative",
        ),
        Index("ix_model_calls_task_created", "task_id", "created_at"),
        Index("ix_model_calls_proposal_created", "proposal_id", "created_at"),
        {"schema": "dx_model"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("dx_governance.intent_proposals.id"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finalize_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    finalize_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanDraft(Base):
    __tablename__ = "plan_drafts"
    __table_args__ = (
        UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_plan_draft_idempotency"
        ),
        CheckConstraint(
            "status IN ('candidate', 'confirmed', 'rejected')",
            name="ck_plan_draft_status",
        ),
        CheckConstraint("revision >= 1", name="ck_plan_draft_revision_positive"),
        Index("ix_plan_drafts_model_call_created", "model_call_id", "created_at"),
        Index("ix_plan_drafts_proposal_created", "proposal_id", "created_at"),
        {"schema": "dx_governance"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.intent_proposals.id"), nullable=False
    )
    model_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_model.model_calls.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_actor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewer_actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlanDraftRevision(Base):
    __tablename__ = "plan_draft_revisions"
    __table_args__ = (
        UniqueConstraint("draft_id", "revision", name="uq_plan_draft_revision"),
        UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_plan_draft_revision_idempotency"
        ),
        CheckConstraint("revision >= 1", name="ck_plan_draft_revision_number_positive"),
        Index("ix_plan_draft_revisions_draft_created", "draft_id", "created_at"),
        {"schema": "dx_governance"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    draft_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.plan_drafts.id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowHandoff(Base):
    __tablename__ = "workflow_handoffs"
    __table_args__ = (
        UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_workflow_handoff_idempotency"
        ),
        UniqueConstraint(
            "draft_id", "draft_revision", name="uq_workflow_handoff_draft_revision"
        ),
        CheckConstraint(
            "status IN ('requested', 'accepted', 'cancelled')",
            name="ck_workflow_handoff_status",
        ),
        CheckConstraint("draft_revision >= 1", name="ck_workflow_handoff_revision_positive"),
        Index("ix_workflow_handoffs_proposal_created", "proposal_id", "created_at"),
        {"schema": "dx_governance"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.intent_proposals.id"), nullable=False
    )
    draft_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.plan_drafts.id"), nullable=False
    )
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="requested")
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("handoff_id", name="uq_workflow_run_handoff"),
        UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_workflow_run_idempotency"
        ),
        CheckConstraint(
            "status IN ('ready', 'running', 'paused', 'completed', 'cancelled', 'failed')",
            name="ck_workflow_run_status",
        ),
        CheckConstraint("draft_revision >= 1", name="ck_workflow_run_revision_positive"),
        Index("ix_workflow_runs_proposal_created", "proposal_id", "created_at"),
        {"schema": "dx_workflow"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.intent_proposals.id"), nullable=False
    )
    handoff_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.workflow_handoffs.id"), nullable=False
    )
    draft_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_governance.plan_drafts.id"), nullable=False
    )
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_workflow_step_ordinal"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_workflow_step_status",
        ),
        CheckConstraint("ordinal >= 1", name="ck_workflow_step_ordinal_positive"),
        Index("ix_workflow_steps_run_ordinal", "run_id", "ordinal"),
        {"schema": "dx_workflow"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("dx_workflow.workflow_runs.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkflowCommand(Base):
    __tablename__ = "workflow_commands"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "actor_type", "actor_id", "idempotency_key",
            name="uq_workflow_command_idempotency",
        ),
        {"schema": "dx_workflow"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("dx_workflow.workflow_runs.id"), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_agent_name_version"),
        UniqueConstraint("actor_type", "actor_id", "idempotency_key", name="uq_agent_idempotency"),
        CheckConstraint(
            "status IN ('registered', 'active', 'paused', 'retired')",
            name="ck_agent_status",
        ),
        Index("ix_agents_status_created", "status", "created_at"),
        {"schema": "dx_agent"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    contract: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    allowed_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentLifecycleCommand(Base):
    __tablename__ = "agent_lifecycle_commands"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "actor_type", "actor_id", "idempotency_key",
            name="uq_agent_lifecycle_idempotency",
        ),
        CheckConstraint(
            "target_status IN ('active', 'paused', 'retired')",
            name="ck_agent_lifecycle_target_status",
        ),
        Index("ix_agent_lifecycle_agent_created", "agent_id", "created_at"),
        {"schema": "dx_agent"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("dx_agent.agents.id"), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(16), nullable=False)
    target_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectDossier(Base):
    __tablename__ = "project_dossiers"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_dossier_task"),
        UniqueConstraint(
            "actor_type", "actor_id", "idempotency_key", name="uq_dossier_idempotency"
        ),
        CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="ck_dossier_status"),
        {"schema": "dx_archive"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="closed")
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    plan_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifacts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Retrospective(Base):
    __tablename__ = "retrospectives"
    __table_args__ = (
        UniqueConstraint(
            "dossier_id", "actor_type", "actor_id", "idempotency_key", name="uq_retro_idempotency"
        ),
        CheckConstraint(
            "status IN ('draft', 'validated', 'retracted')", name="ck_retrospective_status"
        ),
        {"schema": "dx_archive"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dossier_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_archive.project_dossiers.id"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    expected_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    actual_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    deviations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    root_causes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    keep_items: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    change_items: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    stop_items: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    follow_up_items: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        UniqueConstraint(
            "retrospective_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_lesson_idempotency",
        ),
        CheckConstraint(
            "status IN ('candidate', 'validated', 'superseded', 'retracted')",
            name="ck_lesson_status",
        ),
        CheckConstraint(
            "memory_type IN ('episodic', 'semantic', 'procedural')",
            name="ck_lesson_memory_type",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_lesson_confidence"),
        {"schema": "dx_archive"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    retrospective_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_archive.retrospectives.id"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    memory_type: Mapped[str] = mapped_column(String(16), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    applicability: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_actor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewer_actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class IdempotencyRequest(Base):
    __tablename__ = "idempotency_requests"
    __table_args__ = (
        UniqueConstraint(
            "actor_type", "actor_id", "operation", "idempotency_key", name="uq_idempotency_scope"
        ),
        {"schema": "dx_core"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DomainEvent(Base):
    __tablename__ = "domain_events"
    __table_args__ = {"schema": "dx_events"}

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EntityState(Base):
    __tablename__ = "entity_states"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", name="uq_entity_current_state"),
        CheckConstraint("version >= 1", name="ck_state_version_positive"),
        {"schema": "dx_state"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("dx_events.domain_events.id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StateTransition(Base):
    __tablename__ = "state_transitions"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "version", name="uq_state_transition_version"),
        {"schema": "dx_state"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_status: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("dx_events.domain_events.id"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_outbox_event"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'published')", name="ck_outbox_status"
        ),
        Index("ix_outbox_dispatch", "status", "lease_until", "created_at"),
        {"schema": "dx_events"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("dx_events.domain_events.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventInbox(Base):
    __tablename__ = "event_inbox"
    __table_args__ = (
        UniqueConstraint("consumer_name", "event_id", name="uq_inbox_consumer_event"),
        {"schema": "dx_events"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    consumer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("dx_events.domain_events.id"), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = {"schema": "dx_audit"}

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("dx_core.tasks.id"), nullable=False)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("dx_events.domain_events.id"), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
