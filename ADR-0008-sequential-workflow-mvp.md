# ADR-0008: Sequential Workflow bookkeeping MVP

- Status: Proposed / implemented as a contract-only execution boundary
- Date: 2026-08-26

## Decision

Accepting a confirmed, revision-pinned handoff creates a durable `workflow_run` and ordered `workflow_steps` snapshot. Human `advance` commands move the run through `ready` → `running` → `completed`; pause and cancel are explicit terminal/control states. Every command is idempotent and audited.

This phase is deliberately bookkeeping-only. A step's capability and parameters are stored for a future executor but never invoked. Scheduling, worker authority, retries, compensation, resume semantics, and external-side-effect verification remain Phase 4 decisions.

## Consequences

The project now has a testable handoff-to-run state machine without turning model text into autonomous actions. The step snapshot preserves provenance even if the draft is later changed, while the absence of an executor keeps the risk boundary explicit.
