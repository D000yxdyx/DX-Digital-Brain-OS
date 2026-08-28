# ADR-0001: Governance Profile and Control UI Boundary

- Status: Accepted
- Date: 2026-08-26
- Scope: post-Kernel Tracer Bullet evolution

## Context

The project needs a clear governance model for future Intent, Permission, Workflow, Agent, and human approval work. A three-province/six-ministry metaphor is useful for explaining responsibility, but literal division into nine autonomous model calls would add coordination cost and create competing state owners. The project also needs a visual operating console without confusing a developer tool or an agent gateway with the system of record.

## Decision

Adopt the metaphor as a responsibility map, not as an implementation requirement for a fixed number of Agents.

### Three governance lanes

| Lane | DX responsibility | Required output |
|---|---|---|
| Intent / Plan (中书) | Interpret a user goal and propose a structured plan | `IntentProposal` with rationale, scope, and requested capabilities |
| Policy / Review (门下) | Apply Constitution, permission, risk, and approval rules | `ReviewDecision`: approve, reject, or request human input |
| Workflow / Execution (尚书) | Execute only an approved, versioned command | `ExecutionReport` linked to kernel events and audit |

Audit/Observability (御史台) is an independent cross-cutting concern. It observes every proposal, review, approval, execution, and failure; it is not a peer Agent and cannot be bypassed by the executor.

### Six capability domains

The initial domain map is Tasks/Projects, Memory/Knowledge, Research, Schedule/Workflow, Tools/Automation, and Personal Applications. These are bounded contexts and API contracts, not six mandatory model instances. New domains require explicit ownership and a state schema before they are added.

### Agent topology rule

Start with one coordinator and deterministic policy/execution services. Add a specialized Agent only when a benchmark demonstrates a context-isolation or parallelization benefit. Every handoff carries a typed proposal/decision/report reference; free-form summaries are not authoritative state. No Agent may write directly to PostgreSQL or a browser client.

## Control UI placement

The authoritative visual console will be a separately deployable frontend in the same repository:

```text
apps/control-ui/       React + TypeScript + Vite
src/dxbrain/api/       FastAPI contracts and query endpoints
PostgreSQL              canonical state, event, and audit source
```

The UI accesses the system only through authenticated FastAPI endpoints. It never connects directly to the database and it never infers state from model text. The first UI milestone is read-only: task timeline, state transitions, event/outbox health, audit chain, and review/approval records. Mutating actions remain server-side commands and must generate normal kernel audit records.

Codex is the development cockpit for this frontend and backend: it edits the repository, runs tests, and reviews the resulting UI. It is not the production runtime or the data store.

If “小龙虾” means OpenClaw, it is an optional edge adapter for messages, sessions, or operator shortcuts. Its own Gateway and Control UI must not become the canonical DX state owner. OpenClaw may call the DX API through a reviewed adapter; it may not bypass authentication, approval, or audit boundaries.

## Alternatives rejected

1. Six or nine always-on Agents: rejected because role names do not prove context isolation or quality gain.
2. Putting the system console inside OpenClaw: rejected because it couples canonical state to a channel/gateway runtime and duplicates administration surfaces.
3. Letting the frontend query PostgreSQL: rejected because it bypasses API permissions, audit, and schema evolution boundaries.
4. Building the dashboard before the kernel: rejected; v0.1 remains a verified kernel and the UI is a later read model over stable contracts.

## Consequences

Positive: one source of truth, explicit human veto points, low initial model cost, and a UI that can outlive Codex or OpenClaw. Negative: the UI requires deliberate API/read-model design, and the governance lanes will add workflow latency once implemented. Those costs are accepted because they are observable and testable rather than hidden in prompts.

## Implementation gate

Before building the first governance Agent, add typed database/API records for proposals, reviews, approvals, and execution reports; define rejection/retry semantics; and extend the audit tests. Before enabling any write control in `apps/control-ui`, prove permission denial, approval expiry, idempotency, and audit linkage.
