# ADR-0010: Agent Execution Gateway and the V0.2 Execution Bridge

- Status: Proposed
- Date: 2026-09-21
- Scope: Agent / Workforce execution, Execution Gateway, Governance binding, Outbox, Security
- Baseline: `81c0ffc` (`docs: freeze pre-execution baseline`), branch `feat/agent-execution-bridge-v02`
- Relationship: complements ADR-0005. Does **not** modify, override, or redefine it.

## Context

DX Digital Brain OS is the System of Record, Governance Plane, Audit Plane, and Memory Plane. It currently has no Agent runtime, no external Agent execution, no autonomous scheduler, and no queue platform beyond the PostgreSQL outbox (ADR-0001, ADR-0008).

The project now wants a path to let approved work be executed by an external workforce/agent backend (Multica being the first candidate, with Codex CLI, Claude CLI, Ollama, and computer-use as possible later providers). The naive approach — letting DX Brain call Multica directly, or letting the FastAPI request thread invoke an external agent — would bind canonical DX state to a vendor, leak governance authority to an external system, and create a second, unaudited execution path alongside the existing outbox.

This ADR defines the boundary that makes external execution possible without surrendering source-of-truth, approval, or audit authority.

It deliberately does **not** implement anything. Implementation follows in later phases.

## Decision

Introduce a **provider-neutral Agent Execution Gateway** inside DX Digital Brain OS. The Execution Gateway is the **exclusive outbound boundary for external agent/workforce execution providers**, and it acts only as a result of an already-approved, revision-pinned workflow step.

This exclusive-boundary statement covers agent/workforce execution providers. It does **not** apply to, does **not** narrow, and does not otherwise affect the model-call provider transport already defined by ADR-0005.

### Layer model

```text
Owner / Human
    ↓  (approval, review, veto)
DX Digital Brain OS          ← System of Record, Governance, Audit, Memory
    ↓
Execution Gateway            ← provider-neutral, DX-owned
    ↓
Execution Provider Adapter   ← MulticaAdapter / CodexCLIAdapter / ... / FakeAdapter
    ↓
External Execution Provider  ← Multica workforce backend, etc.
    ↓
Execution Result
    ↓
DX persistence → Audit → Human Review
```

Multica (and any future provider) is an **execution provider / workforce backend only**. It is never:

- source of truth
- workflow authority
- approval authority
- task authority
- audit authority

### Two provider classes

The word "provider" must not be used unqualified. Two distinct provider classes exist, with different owners, lifecycles, and ledgers:

| Class | Meaning | Owner | Examples |
|---|---|---|---|
| **model provider** | Serves a single model request/response, with token and cost accounting | ADR-0005 (`src/dxbrain/providers/`) | `DeepSeekAdapter`, `AnthropicAdapter` |
| **agent / workforce execution provider** | Executes a governed unit of approved work that may internally contain many model calls, tool calls, file operations, and command executions | ADR-0010, this ADR (`src/dxbrain/execution/`) | `FakeAdapter` / `OfflineAdapter`, `MulticaAdapter`, `CodexCLIAdapter`, future `ClaudeCodeCLIAdapter`, `OllamaAdapter` |

Naming hazard to avoid: `AnthropicAdapter` (a **model** provider under ADR-0005) and a future `ClaudeCodeCLIAdapter` (an **agent/workforce execution** provider under this ADR) are not interchangeable. The bare term "Claude provider" is ambiguous and must not be used; the same applies to "Codex" or "OpenAI" style names, which can refer to either class.

Only the second class is governed by the Execution Gateway's exclusive outbound boundary.

## Domain boundaries

### Canonical identity

| Concept | Canonical identity | Notes |
|---|---|---|
| Task | **DX task id** (`dx_core.tasks.id`) | Only DX defines task identity and lifecycle |
| Workflow | **DX workflow run id / workflow step id** (`dx_workflow.workflow_runs.id`, `workflow_steps.id`) | Already revision-pinned via `draft_revision` (ADR-0007, ADR-0008) |
| Agent execution | **DX agent execution id** | Generated and owned by DX |
| External run | **external_execution_id only** | e.g. a Multica issue/run id. Never canonical, never a join key for DX truth, never used to decide task state |

An external id may be stored for traceability and reconciliation. It must never be promoted to canonical identity, and no state machine may key on it.

### Three distinct concepts

These must not be collapsed into one:

| Concept | Meaning | Owner |
|---|---|---|
| **model_call** | A single model request/response, with token and cost accounting | ADR-0005 (controlled model call, two-phase ledger) |
| **agent_execution** | A governed execution unit: one approved piece of work that may internally contain many model calls, tool calls, file operations, and command executions | **ADR-0010 (this ADR)** |
| **tool_execution** | A concrete tool/command/external capability occurrence *inside* an agent_execution | Future work; not designed here |

ADR-0010 does not cover or redefine `model_call`. An agent_execution may *contain* model calls that remain subject to ADR-0005 rules. The two ledgers stay separate.

### Boundary vs Governance ExecutionReport

`AgentExecution` (with its result) and `dx_governance.execution_reports` (Governance ExecutionReport, the 尚书-lane outcome in ADR-0001) are **two different concepts**.

| | AgentExecution | Governance ExecutionReport |
|---|---|---|
| Nature | technical execution fact: telemetry, provider result, exit state | governance-layer evidence / attestation / review object |
| Written by | execution worker / Gateway | existing Governance path only |
| Answers | "what the provider actually did" | "what Governance attests" |

Explicitly forbidden:

- The execution worker must **not** auto-generate a Governance ExecutionReport after `SUCCEEDED`.
- An `AgentExecution` reaching `SUCCEEDED` must **not** auto-transition to Task DONE or to any governance completion state.

In V0.2 Phase 1 the two remain **fully separate**. If Governance later needs to cite an AgentExecution as evidence, that must occur through an **explicit human/governance action**, never as a side effect of execution.

This ADR decides the conceptual boundary only. It does **not** decide any future FK, join table, or schema linkage — those remain deferred and must not be pre-locked here.

### Event namespace

Events follow the repository convention `aggregate.verb.vN` used by `dx_events.domain_events.event_type` (for example `task.created.v1`, `model.planner.finalized.v1`, `agent.registered.v1`). Audit operations use the same stem without the version suffix.

`agent_execution.*` is an **independent domain namespace**. The first canonical outbox event is:

```text
agent_execution.requested.v1
```

Further event types may follow the same shape if the implementation needs them — for example `agent_execution.dispatched.v1`, `agent_execution.started.v1`, `agent_execution.succeeded.v1`, `agent_execution.failed.v1`, `agent_execution.reported.v1` — but V0.2 Phase 1 is **not** required to emit all of them.

The governance event `governance.execution.reported.v1` already exists and **must not** be reused for agent execution.

### Module boundary

Execution lives in a namespace **parallel to** the existing model-provider package, not inside it:

```text
src/dxbrain/providers/    model-call providers (DeepSeek, Anthropic, ...)  — ADR-0005
src/dxbrain/execution/    agent/workforce execution: gateway, contracts, adapters
```

This keeps two different abstractions (one model request vs. one governed execution unit) from sharing one registry and one lifecycle.

## Execution lifecycle

```text
REQUESTED → DISPATCHED → RUNNING → SUCCEEDED / FAILED → REPORTED
```

- `REQUESTED` — created in the same transaction as the outbox event. Nothing has left DX yet.
- `DISPATCHED` — the worker has handed the request to a provider adapter.
- `RUNNING` — provider has acknowledged an in-flight execution.
- `SUCCEEDED` / `FAILED` — the *execution* ended. This is a statement about the execution, not about the task.
- `REPORTED` — the result has been persisted, audited, and surfaced for human review.

`CANCELLED` is **not** a V0.2 lifecycle state. Cancellation semantics are deferred to a future ADR/revision. V0.2 deliberately defines no cancel command, cancel API, provider cancellation flow, cancel approval, or cancel retry.

**`SUCCEEDED` != Task DONE.** An execution succeeding means only that this execution ran to completion. Final task completion continues to require the existing DX Governance / Human Approval path. No external provider may set a task to done, and no adapter result may write task state directly.

## Governance / approval relationship

The Gateway does not introduce a second approval system. An execution request must bind, at minimum:

- `task_id`
- `proposal_id` (existing `dx_governance.intent_proposals`)
- `workflow_run_id` and `workflow_step_id`
- `approval_id` — an existing, active, unexpired `dx_governance.approval_grants` row whose scope covers the requested capability
- `draft_id` + `draft_revision` — the approved plan revision, pinned at creation time
- `capability` — taken from `workflow_steps.capability`, and must be within the executing agent's `allowed_capabilities` (ADR-0009)

An Agent may execute only **the revision that was approved**. If the plan or workflow is later revised, an existing execution request must **not** silently switch to the new revision; a new request must pass through the governance boundary again.

ADR-0009's `check_permission` remains a local fact check (agent active + capability declared) and is explicitly **not** an authorization to execute.

## Outbox and idempotency

The existing atomic outbox is reused; no new queue infrastructure.

```text
approved, revision-pinned workflow step
  → single transaction:
        create agent_execution (REQUESTED)
      + domain event
      + outbox row (agent_execution.requested.v1)
      + audit row
  → dedicated execution worker
      → lease + consume (existing OutboxWorker lease/inbox pattern)
      → idempotent dispatch → adapter
      → persist result, advance state, emit event, write audit
```

**Forbidden:** the FastAPI HTTP request thread calling Multica, Codex, Claude, Ollama, or any external agent directly. Long-running work, retries, failure isolation, and idempotency are the worker's job.

Idempotency requirements:

- `agent_execution.requested.v1` may be delivered more than once; the worker must dispatch idempotently.
- One DX agent execution id must never become multiple logical executions because of an outbox retry.
- Where a provider supports an idempotency key, prefer the DX execution id or a stable derivative of it.
- Where a provider does not, DX must still de-duplicate at its own persistence layer.
- The existing repo pattern (`idempotency_key` + `request_hash` mismatch detection) is the baseline; specific lock strategy is an implementation decision and is intentionally not fixed here.

## Provider abstraction

The Gateway is provider-neutral. The adapter set is open-ended:

```text
FakeAdapter / OfflineAdapter   ← the only adapter required in V0.2 stage 1
MulticaAdapter                 ← future
CodexCLIAdapter                ← future
ClaudeCLIAdapter               ← future
OllamaAdapter                  ← future
FutureAdapter                  ← future
```

This ADR does **not** commit to implementing any adapter beyond the offline/fake one. Minimum adapter capabilities for V0.2 are **dispatch** and **collect result**; any status-polling or cancellation capability is an implementation-phase contract, not a decision made here, and cancellation is deferred (see Execution lifecycle).

## Execution request and result semantics

Logical fields, reusing existing repository naming wherever it already exists:

**Execution request**

| Field | Source / note |
|---|---|
| `execution_id` | DX-generated canonical id |
| `task_id` | `dx_core.tasks.id` |
| `proposal_id` | `dx_governance.intent_proposals.id` |
| `workflow_run_id` | `dx_workflow.workflow_runs.id` |
| `workflow_step_id` | `dx_workflow.workflow_steps.id` |
| `approval_id` | `dx_governance.approval_grants.id` |
| `draft_id`, `draft_revision` | approved plan revision, pinned |
| `capability` | from `workflow_steps.capability` |
| `agent_role` | references `dx_agent.agents.role` |
| `provider` | logical provider name selected by DX |
| `parameters` / input | from `workflow_steps.parameters` |
| `constraints` | timeout, resource, and capability limits |
| `workspace_ref` | logical symbolic key, server-resolved — see below |

### workspace_ref contract

The repository currently has no project/workspace registry (`src/dxbrain/core/models.py` contains no `project_id` or workspace field). Rather than leave the concept undefined, V0.2 fixes this minimal contract and nothing more:

**`workspace_ref` is a provider-neutral symbolic reference. It is not a filesystem path.**

- A client or internal workflow passes a **logical key** only.
- The server resolves that key to a real workspace through DX-controlled configuration.
- **Unknown `workspace_ref` → deny.** There is no fallback to a default path.
- Clients may never supply an arbitrary absolute path.
- Adapters must not trust client-supplied paths; they receive only the server-resolved location.

Illustrative logical keys — examples only, **no machine path is recorded in this ADR**:

```text
"default"
"p001"
"math-modeling"
```

V0.2 explicitly does **not** introduce: workspace CRUD, a project ownership model, or a workspace lifecycle system.

The resolution mechanism remains an implementation choice — a static settings mapping or a minimal server-controlled registry both satisfy this contract. This ADR does not lock the storage form, and the concept must never be improvised into a raw path column accepted from a client.

**Execution result**

`external_execution_id`, `status`, `summary`, `artifacts`, `logs` / log references, `exit_code`, `error`, `started_at`, `finished_at`.

These are domain-level semantics only. Column types, nullability, FK layout, and schema placement are explicitly **not** decided in this ADR.

## Security boundary

A future real execution worker runs as a dedicated OS user (`aiworker`) with:

- access limited to the **approved project workspace** resolved by DX
- **default denial** of: the user home directory, SSH private keys, credential stores, arbitrary absolute paths, and administrator/root privileges

High-risk capabilities must continue to require explicit Human Approval. Indicative list: `delete`, `git push`, `git merge`, package installation, credential access, system modification, network publish, and expensive external API use.

The Execution Gateway must not become a channel for bypassing governance. If a capability cannot be evaluated against an approval, the default outcome is refusal.

## Kill switch

Agent execution is **default OFF**.

```text
DX_AGENT_EXECUTION_ENABLED=false     # master switch, follows the DX_LLM_LIVE_ENABLED pattern
DX_MULTICA_ENABLED=false             # provider-specific switch (future)
```

Both must be enabled, together with an active approval and a declared capability, before any real dispatch. The naming follows the existing `DX_LLM_LIVE_ENABLED` convention in `src/dxbrain/config.py`.

V0.2 initial concurrency: **1**. No scheduler is designed here.

## V0.2 MVP scope

Stage 1 proves only the internal chain:

```text
DX approved workflow step
→ execution request
→ outbox
→ execution worker
→ Fake / Offline Adapter
→ execution result persistence
→ audit
→ human review
```

No real Multica connection in stage 1. Only after this loop is stable and audited may a `MulticaAdapter` be introduced behind a manually gated smoke test.

## Non-goals

This ADR is **not** a design for:

- an autonomous agent platform
- a scheduler
- a planner
- a multi-agent orchestration engine
- a research OS
- a memory system redesign
- a vector database system
- a distributed queue platform
- a Multica-specific architecture
- a Codex-specific or Claude-specific architecture
- a replacement for existing Governance
- a replacement for existing Workflow
- a replacement for existing Outbox
- a replacement for the model_call ledger

## Consequences

**Positive.** External execution becomes possible without ceding canonical identity, approval authority, or audit. Providers become replaceable. The governance boundary stays the only place where "may this happen" is decided, and `SUCCEEDED` remains a statement about an execution rather than about a task.

**Negative / accepted costs.** An extra indirection layer and one more worker to operate; execution latency increases because nothing happens in the request thread; the offline adapter means stage 1 proves plumbing, not real-world agent behaviour. These are accepted because they keep risk observable and testable instead of hidden inside a vendor integration.

**Open risk.** The `workspace_ref` contract is now fixed, but the resolution mapping behind it does not exist yet; it must be implemented before any real adapter is enabled. Cancellation is likewise undefined in V0.2, so an in-flight execution that must be stopped has no designed path in this phase.

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| **A. DX Brain calls Multica directly** | Provider lock-in; governance leakage; source-of-truth confusion; canonical state becomes dependent on a vendor's identifiers and availability |
| **B. Put agent execution into `model_calls`** | Different lifecycle and semantics: one model request/response vs. one governed execution unit potentially containing many calls and side effects. Would corrupt the ADR-0005 cost/token ledger |
| **C. Execute the agent synchronously in the FastAPI request thread** | Long-running work, no durable retry, no failure isolation, duplicated side effects on timeout, and idempotency becomes unenforceable |
| **D. Build a second approval system for executions** | Governance fork and state divergence; two places would answer "is this allowed", and they would disagree |
| **E. Introduce Redis / Celery / LangGraph / CrewAI / AutoGen now** | The existing PostgreSQL outbox already provides durable, atomic, at-least-once delivery with lease handling; added infrastructure complexity is not justified at MVP scale |

## Implementation notes / future work

Recorded, **not** implemented in this ADR:

1. **`tests/conftest.py` has a hardcoded TRUNCATE table list.** When `agent_executions` or related FK tables are added, the migration + model + conftest cleanup slice must be validated together, or integration tests will fail at cleanup with FK errors. Not modified in this phase.
2. **Event naming.** The namespace is `agent_execution.*` following the repo `aggregate.verb.vN` convention, with `agent_execution.requested.v1` as the first canonical outbox event. The exact emitted set beyond that is an implementation decision; `governance.execution.reported.v1` must not be reused.
3. **Workspace resolution.** No project/workspace concept exists today. `workspace_ref` is fixed by this ADR as a symbolic key resolved server-side through DX-controlled configuration. Choosing between a static settings mapping and a minimal server-controlled registry is an implementation choice; it must be designed before any real adapter is enabled.
4. **Adapter interface** — minimum V0.2 capabilities are dispatch and collect result. Status polling and cancellation are not committed here.
5. **Relation to `dx_governance.execution_reports`** — the conceptual boundary is now decided (separate concepts; no auto-generation, no auto task completion). Any future FK, join table, or schema linkage remains explicitly undecided.
6. **Persistent schema design** (tables, columns, FKs, grants, migration ownership under `dx_migration`) is out of scope for this ADR.
7. **Cancellation** — no cancel command, cancel API, provider cancellation, cancel approval, or cancel retry is designed in V0.2. Deferred to a future ADR/revision.
