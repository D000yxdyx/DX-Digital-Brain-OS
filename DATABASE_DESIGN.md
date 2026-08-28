# Database Design — Kernel Tracer Bullet v0.1 + Governance + Archive Contracts

## Schemas and ownership

Alembic creates all schemas/tables under `dx_migration` ownership. Runtime database users receive no DDL rights.

| Schema | Tables | Purpose |
|---|---|---|
| `dx_core` | `tasks`, `idempotency_requests` | Aggregate and replay protection |
| `dx_state` | `entity_states`, `state_transitions` | Versioned state and immutable history |
| `dx_events` | `domain_events`, `event_outbox`, `event_inbox` | Event fact, dispatch lease, consumer barrier |
| `dx_audit` | `audit_events` | Append-only accountability record |
| `dx_governance` | `intent_proposals`, `review_decisions`, `approval_grants`, `execution_reports`, `plan_drafts` | Typed plan/review/approval/execution contracts and human-reviewed model plan candidates |
| `dx_archive` | `project_dossiers`, `retrospectives`, `lessons` | Closed project records, evidence-linked retrospectives, and candidate/validated lessons |
| `dx_model` | `model_calls` | Provider, model, usage, cost, and dry-run/live call metadata |
| `dx_workflow` | `workflow_runs`, `workflow_steps`, `workflow_commands` | Revision-pinned sequential Workflow bookkeeping |
| `dx_agent` | `agents`, `agent_lifecycle_commands` | Versioned Agent contracts, lifecycle, and declared capabilities |

All IDs are UUIDs; all times are `timestamptz` in UTC. JSON stores payloads but does not replace typed indexes or foreign keys.

## Constraints

| Table | Important constraint |
|---|---|
| `idempotency_requests` | unique `(actor_type, actor_id, operation, idempotency_key)` |
| `entity_states` | unique `(entity_type, entity_id)`, `version >= 1` |
| `state_transitions` | unique `(entity_type, entity_id, version)` |
| `event_outbox` | unique `event_id` |
| `event_inbox` | unique `(consumer_name, event_id)` |

## Transaction boundary

`create_task` uses one SQLAlchemy `session.begin()` transaction. A transaction-scoped PostgreSQL advisory lock serializes a matching actor/operation/idempotency key without granting `UPDATE` on the idempotency table. Task, idempotency record, current state, transition, event/outbox, and audit row are inserted before commit. An exception rolls back the complete unit.

## Outbox lease protocol

1. The worker selects one pending or expired-processing row using `FOR UPDATE SKIP LOCKED`.
2. It commits a short `processing` lease.
3. It inserts the inbox row; that unique key makes duplicates harmless.
4. It marks the outbox row `published` and clears lease fields.

A crash after inbox insert but before acknowledgement is safe after lease expiry: reprocessing sees the existing inbox entry. This is at-least-once delivery with idempotent handling, not a false exactly-once claim.

## Governance contract lifecycle

`intent_proposals.status` follows the bounded lifecycle below. Review decisions and execution reports are append-only; the proposal status is updated only inside the corresponding service transaction.

```text
proposed ──request_input──> awaiting_input
    │                             │
    └────────approve──────────────┘
    │
    ├──reject──> rejected
    └──approve──> approved ──start──> executing
                                ├──succeeded──> completed
                                ├──failed─────> failed
                                └──cancelled──> cancelled
```

An approval grant always has a future expiry timestamp and a non-empty capability scope. Its effective read status becomes `expired` when the timestamp passes; the database row remains append-only unless a future revocation command is separately approved.

## Privilege design

`dx_api` gets command-side writes, event reads needed for PostgreSQL `RETURNING`, and audit insertion, but no update/delete/truncate privilege on `dx_audit.audit_events`. `dx_worker` gets only the outbox/inbox read-write privileges required for lease handling and generated timestamps. Real-role tests enforce this boundary.

The governance migration grants `dx_api` proposal insert/update and decision/approval/report insert privileges. `dx_worker` may read proposals and approvals and append execution reports, but cannot alter proposals, reviews, or approvals.

The archive migration grants `dx_api` select/insert/update on dossier, retrospective, and lesson rows; `dx_worker` receives read-only access for future closeout consumers. Archive tables do not grant any runtime role permission to delete audit or source event rows. A lesson's `validated` status is a reviewed projection, not an automatic write into the future `dx_memory` schema.

The model migration grants `dx_api` select/insert/update on `dx_model.model_calls` and `dx_worker` read-only access. A model-call row is written with a task-scoped domain event and audit row. `live` calls use a two-phase ledger: `prepared` is committed before an external transport may run, then a separate finalize command records `succeeded` or `failed` with usage/cost metadata. The current `dry_run` mode records zero usage and zero cost; the live feature flag remains disabled by default.

`dx_governance.plan_drafts` stores the deterministic parser output of a succeeded planner call. Each draft is `candidate` until a human review changes it to `confirmed` or `rejected`; the review is separately evented and audited. A confirmed draft does not mutate the original proposal or start a workflow automatically.

`dx_governance.plan_draft_revisions` is append-only history for human edits while a draft is a candidate. `dx_governance.workflow_handoffs` records one explicit handoff request per draft revision. Handoffs begin as `requested`; acceptance, scheduling, execution, retry, and compensation remain deferred Workflow responsibilities.

`dx_workflow.workflow_runs` and `dx_workflow.workflow_steps` materialize an accepted handoff as a revision-pinned sequential run. `dx_workflow.workflow_commands` makes advance/pause/cancel commands idempotent. The MVP records state transitions only; it has no external tool executor, scheduler, retries, compensation, or resume-after-pause authority.
