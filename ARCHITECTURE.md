# DX Digital Brain OS Architecture — Kernel Tracer Bullet v0.1 + Governance + Archive Contracts

## Purpose and freeze boundary

This is Phase 1's proof of the operating-system kernel, not a reduced chatbot. It validates the minimum closed loop: an authenticated command produces a task, state record, state transition, immutable intent event, durable outbox record, and audit record in one PostgreSQL transaction; an internal worker then consumes the outbox event once per named consumer.

No autonomous Workflow executor/scheduler, Agent registry, Redis, vector store, external tool, dashboard write control, or multi-user authorization is part of this freeze. The contract-only Workflow MVP records revision-pinned bookkeeping transitions but does not execute side effects. The governance contract foundation is intentionally limited to typed persistence and authenticated commands. Typed DeepSeek and Anthropic adapters shape and parse requests offline. A guarded live planner path now exists for one approved, no-tool, low-risk call, using the two-phase model-call ledger; broad model runtime remains outside the freeze.

## Module boundaries

| Module | Owns | Must not own |
|---|---|---|
| `api` | HTTP validation, root-key authentication, command orchestration | Direct worker acknowledgement or DDL |
| `core` | Domain types, mappings, transactional task command | HTTP transport or background polling |
| `worker` | Lease acquisition, inbox idempotency, outbox acknowledgement | Creating user tasks |
| `governance` | Typed proposal/review/approval/report lifecycle and its event/audit linkage | Model inference, tool side effects, or bypassing Kernel transactions |
| `archive` | Project dossier, retrospective draft, candidate lesson, and evidence linkage | Direct memory mutation, unverified learning, or deleting audit history |
| `providers` | Provider descriptors, role routing, typed request/response adapters | API-key exposure, implicit outbound calls, prompt execution, or bypassing governance |
| Alembic | PostgreSQL schemas, constraints, privileges | Runtime business logic |
| PostgreSQL | Durable state, events, outbox, audit trail | Agent decisions or side effects |
| `apps/control-ui` | Read-only operational and governance views through FastAPI | Direct database access or hidden writes |

## Command and event data flow

```text
Client + X-DX-API-Key
  -> POST /v1/tasks + Idempotency-Key
  -> one transaction:
       tasks/idempotency + current-state/transition + event/outbox + audit
  -> commit
  -> worker claim lease -> inbox unique(consumer,event) -> outbox published
```

The transaction is the closure point: a failure before commit leaves none of the records visible. The outbox is the only handoff to asynchronous code. The inbox unique constraint makes a repeated delivery a no-op for that consumer; it does not claim global exactly-once delivery.

Governance commands use the same closure point. A proposal, review decision, approval grant, or execution report is committed together with its task-scoped domain event, outbox row, and audit row. Proposal status changes are legal only through the service transition rules; reports are append-only observations.

Archive commands use the same closure point. A project dossier, retrospective draft, candidate lesson, or lesson review is committed with a task-scoped domain event, outbox row, and audit row. Lessons remain candidates until a human review marks them validated; validation does not yet write the long-term Memory Layer.

## Authority and permissions

Authentication is a deliberately temporary local root key. It proves one explicit actor (`human:dx`) but is not a production identity system. Approval booleans, Agent/tool permissions, and delegation do not exist in v0.1 and must not be inferred from request JSON.

- `dx_bootstrap`: Docker initialization only; never injected into the application.
- `dx_migration`: schema owner and Alembic user.
- `dx_api`: task/state/event/outbox/audit writes plus the event reads required for database-generated timestamps.
- `dx_worker`: outbox claim/acknowledgement and inbox writes/reads required for generated timestamps.

`dx_api` cannot modify or delete audit data after insertion. Model-call ledger rows may be finalized through their lifecycle update, but each transition also appends an immutable audit event. Compose mounts the PostgreSQL 18 named volume at `/var/lib/postgresql`, avoiding the incompatible legacy `/var/lib/postgresql/data` mount.

## Invariants

1. `entity_states.version` advances only with a matching state transition.
2. A task command either writes every kernel artifact or writes none.
3. A transaction-scoped advisory lock serializes the same idempotency scope; the same payload returns the original task, while a changed payload is rejected.
4. An outbox event is processed at most once for a named consumer according to the durable inbox.
5. Audit rows are append-only to the API role.
6. A lesson cannot become validated without a human review and a source-linked retrospective.
7. A live model call requires the feature flag, configured provider, unexpired approval, and `model.call` capability; preparation is committed before transport execution and finalization is separately audited.
8. Model text is never treated as executable workflow input directly: parsing creates a `candidate` plan draft, and a human confirmation is required before any future Workflow handoff. Confirmation does not mutate the proposal in this phase.
9. Human edits are append-only draft revisions. A confirmed draft can create only a durable `requested` handoff record; no Workflow scheduler or executor is implicitly invoked.
10. Workflow acceptance snapshots the confirmed revision into ordered steps. The MVP can only perform explicit bookkeeping transitions (`ready`/`running`/`completed`, plus pause/cancel); it cannot invoke external capabilities.
11. An Agent must be registered with a versioned contract and remain `active` before its declared capability can pass the local permission check. A capability check is not an approval grant and never authorizes a side effect by itself.

## Deferred decisions

Phase 2 memory is pgvector-first, not Qdrant by default. Redis is evaluated only after the PostgreSQL outbox is demonstrably insufficient. Cryptographic approval tokens, full RBAC/ABAC, worker identity, and durable workflow scheduling need separate architecture decisions before implementation.

The accepted governance and UI boundary is recorded in [ADR-0001](ADR-0001-governance-and-control-ui.md). The external Three-Province/Six-Ministry reference evaluation is recorded in [ADR-0002](ADR-0002-three-province-reference-evaluation.md). The controlled model-call boundary is recorded in [ADR-0005](ADR-0005-controlled-model-call.md). Structured plan parsing and human confirmation are recorded in [ADR-0006](ADR-0006-structured-plan-confirmation.md). The current UI remains read-only; broader mutating controls and outbound model calls require their own reviewed contracts.
