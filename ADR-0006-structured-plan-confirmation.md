# ADR-0006: Structured plan parsing and human confirmation

- Status: Proposed / implemented as a controlled checkpoint
- Date: 2026-08-26

## Decision

Treat planner output as untrusted text. `PlanDraftService` parses only numbered or bullet Markdown list items with the deterministic `markdown-list-v1` parser, bounds the result to 32 steps, and stores a `candidate` record in `dx_governance.plan_drafts`. Every parse and review emits a domain event, outbox row, and audit record.

Only a `human` actor may change a draft to `confirmed` or `rejected`. Confirmation is deliberately non-invasive: it does not mutate the source intent proposal and does not start Workflow. A later Workflow handoff must define its own contract, permissions, and tests.

## Consequences

This gives the system a verifiable model-output checkpoint and preserves provenance through the model-call ID and source hash. It adds one explicit human step and avoids pretending that free-form model text is safe executable state.
