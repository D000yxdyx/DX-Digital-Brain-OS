# DX Digital Brain OS Roadmap

## Phase 0 — architecture freeze

Completed design artifacts: architecture audit, `ARCHITECTURE.md`, `DATABASE_DESIGN.md`, module boundary, security invariants, and this roadmap. The remaining Phase 0 decision is acceptance of the tracer-bullet boundary.

Organizational architecture research is recorded in `ORGANIZATIONAL_ARCHITECTURE_RESEARCH.md` and proposed in [ADR-0003](ADR-0003-organizational-architecture.md). It is not yet a runtime-topology authorization: fixed Agent counts, autonomous self-modification, and high-risk tools remain out of scope until the ADR is confirmed.

## Phase 1 — Kernel Tracer Bullet v0.1

1. PostgreSQL 18, least-privilege roles, Alembic migration.
2. Atomic task/state/event/outbox/audit command path.
3. FastAPI create/query endpoints with an idempotency key.
4. Lease-based worker and per-consumer inbox barrier.
5. Integration tests for rollback, stale version, replay, audit immutability, and persistence restart.

Exit gate: every item is demonstrated against Compose PostgreSQL and documented in README.

## Phase 1.5 — Governance Contract Foundation

Completed: durable proposal, review decision, approval grant, and execution report records; deterministic legal transitions; expiry-aware approvals; idempotent commands; task-scoped event/outbox/audit linkage; authenticated API contracts; and integration tests.

Exit gate: no governance Agent or mutating UI is enabled until identity, worker authority, approval-token semantics, and external-side-effect boundaries receive a separate decision.

## Phase 5 — Read-only Control UI foundation

Completed initial slice: authenticated overview counts, newest-task read model, task detail timeline, governance chain, project dossier/retrospective/validated-lesson read models, explicit read-only boundary, and a React/Vite console in `apps/control-ui`.

Remaining work: guarded mutating controls and application-specific views. Those require a separate architecture and security decision.

## Phase 1.6 — Model provider contract foundation

Completed: optional server-side configuration for DeepSeek and Anthropic/Claude, provider readiness metadata, planner/reviewer/fallback role routing, typed request/response adapters with normalized usage, a model-call audit table, a deterministic dry-run planner, a two-phase prepared/finalized ledger, and one controlled live DeepSeek planner validation. The live feature flag remains disabled by default, and keys are never exposed to the UI or logs.

Remaining gate for broader live use: approve provider-specific pricing/accounting, structured response parsing, timeout classification, prompt-data boundary, supplier idempotency keys, and compensation for calls that complete outside the database transaction. The readiness preflight and two-phase ledger expose these controls without silently widening execution scope.

## Phase 1.7 — Project dossier and retrospective foundation (proposed)

Proposed: add a deterministic project dossier closeout, evidence-linked retrospective draft, validated lesson records, and a bridge into the later Memory Layer. Audit history remains immutable; model-generated lessons remain candidates until validated. See [ADR-0004](ADR-0004-records-retrospective-memory.md).

Exit gate: dossier completeness checks, source-linked retrospective schema, privacy minimization, retraction/supersession semantics, and one measured example showing that a validated lesson improves a later task without contaminating the fact layer.

## Deferred phases

- Phase 2: working, episodic, semantic, procedural memory; pgvector benchmark and retention policy first.
- Phase 3: Agent runtime, delegation, tool-permission enforcement against approvals, and multi-agent coordination.
- Phase 4: workflow DAG, approval tokens, retries/compensation, human interruption.
- Guarded application experiences and integrations after the read-only console.

The three-lane governance profile and Control UI boundary are accepted in [ADR-0001](ADR-0001-governance-and-control-ui.md). The current implementation remains read-only at the UI boundary; mutating controls still require a separate decision.

No deferred phase may bypass Phase 1 event, audit, permission, and state invariants.

## Phase 1.8 — Structured plan confirmation

Completed: deterministic bounded parsing of succeeded planner text into `candidate` plan drafts, idempotent human review, and separate event/audit records. Confirmed drafts remain proposals for a later Workflow phase; no automatic execution is enabled.

## Phase 1.9 — Plan revision and Workflow handoff contract

Completed: append-only human plan revisions and an idempotent, revision-pinned `requested` Workflow handoff record. Workflow acceptance, scheduling, execution, retries, compensation, and interruption remain deferred to Phase 4.

## Phase 1.10 — Sequential Workflow bookkeeping MVP

Completed: accepted handoffs create revision-pinned runs and ordered step snapshots; explicit human advance, pause, and cancel commands are idempotent and audited. No external action executor or scheduler is enabled.

## Phase 1.11 — Agent contract and lifecycle MVP

Completed: task-scoped versioned Agent registry, contract validation, human-controlled lifecycle (`registered`/`active`/`paused`/`retired`), and declared capability checks. Agent runtime, delegation, tool execution, and multi-agent coordination remain deferred.
