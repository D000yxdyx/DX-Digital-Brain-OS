# ADR-0007: Plan revisions and explicit Workflow handoff

- Status: Proposed / implemented as a contract-only slice
- Date: 2026-08-26

## Decision

Human edits to a `candidate` plan are append-only rows in `dx_governance.plan_draft_revisions`; the draft keeps the latest revision number and steps. A confirmed draft may create one idempotent, revision-pinned `requested` row in `dx_governance.workflow_handoffs`.

The handoff is a durable boundary, not a Workflow engine. It does not accept, schedule, execute, retry, compensate, or mutate the source proposal. Those behaviors require a separate Phase 4 design covering worker authority, approval tokens, interruption, and side-effect recovery.

## Consequences

The system preserves every human change and makes the exact revision handed to a future Workflow layer explicit. The additional human checkpoint and the one-handoff-per-revision rule prevent accidental execution while the Workflow contract is still under design.
