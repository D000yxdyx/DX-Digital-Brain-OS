# ADR-0002: Three-Province/Six-Ministry Reference Evaluation

- Status: Accepted
- Date: 2026-08-26
- Scope: governance model and future orchestration

## Context

Public GitHub projects using a Three-Province/Six-Ministry metaphor were evaluated as architecture references, with [cft0808/edict](https://github.com/cft0808/edict) as the closest match. Related OpenClaw-oriented projects include [danghuangshang](https://github.com/wanikua/danghuangshang) and [tiangong](https://github.com/wanikua/tiangong).

The public material demonstrates useful governance concepts: explicit task states, a planning/review/dispatch path, review rejection, permission matrices, audit trails, intervention controls, and an operational dashboard. The same material also shows that these projects are OpenClaw-centric, evolve quickly, and include optional direct-channel bypasses and experimental self-evolving or personality features. The review was based on public repository documentation and is not a code-level security audit or an endorsement of production readiness.

## Decision

DX Digital Brain OS adopts the governance principles, not the reference projects' runtime topology or role count.

1. The existing eight-layer architecture remains the system decomposition. Governance is a cross-cutting control plane, not an additional mandatory Agent layer.
2. The control plane contains Constitution, permission, risk, approval, policy review, audit, and observability concerns.
3. The data plane contains Tasks, State, Events, Memory, Workflow, domain workers, and optional Agents.
4. Three governance lanes are retained as responsibility contracts:
   - Intent/Plan (中书): produce a typed `IntentProposal`.
   - Policy/Review (门下): produce an approve/reject/request-input `ReviewDecision`.
   - Workflow/Execution (尚书): execute only an approved, versioned command and produce an `ExecutionReport`.
5. Six capability domains are bounded contexts, not six permanent model instances. An Agent is introduced only when a measured context-isolation or parallelization benefit justifies it.
6. PostgreSQL remains the canonical source for task state, domain events, audit, and reliable handoff records. Redis, if later introduced, is an ephemeral delivery/cache component rather than the source of truth.
7. The Control UI is a DX-owned React frontend behind FastAPI. OpenClaw may be added as an authenticated edge adapter, but it cannot own canonical state or bypass approval and audit boundaries.

## Explicitly not adopted

- Forking or embedding Edict, danghuangshang, or tiangong as the DX kernel.
- A fixed fleet of nine or twelve always-on Agents.
- Direct channel commands that skip policy and audit gates.
- Treating model thinking text, JSONL sessions, a dashboard, or a Redis stream as authoritative state.
- Self-modifying prompts, Agent personality systems, Agent competitions, or gamification in the Kernel MVP.

## Consequences

The project can reuse the useful review and observability ideas without inheriting unnecessary coordination, model, or channel coupling. The cost is that proposals, reviews, approvals, and execution reports need typed persistence and API contracts before governance Agents are enabled. This cost is intentional and testable.

## Implementation gate

Before the first governance Agent or mutating Control UI action, define and test proposal/review/approval/report records, rejection and retry semantics, approval expiry, idempotency, permission denial, and audit linkage. Until then, the verified Kernel Tracer Bullet remains unchanged.

