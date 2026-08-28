# ADR-0009: Agent contract and lifecycle MVP

- Status: Proposed / implemented as a contract-only slice
- Date: 2026-08-26

## Decision

Agents are registered with a name, version, role, explicit input/output schemas, side-effect declaration, and allowed capabilities. The registry is task-scoped so every lifecycle event has a task audit anchor. Lifecycle transitions are human-controlled: `registered` → `active`/`retired`, `active` → `paused`/`retired`, and `paused` → `active`/`retired`.

The permission endpoint checks only two local facts: the Agent is active and the capability is declared. It does not replace proposal approval, issue a tool grant, invoke a model, delegate to another Agent, or run a Workflow step.

## Consequences

The system now has a durable Agent identity and contract that a future Workflow executor can reference. Keeping capability checks separate from approval grants prevents a registry record from becoming an implicit authorization to cause side effects.
