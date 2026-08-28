# Module Design — Kernel Tracer Bullet v0.1 + Governance + Archive Contracts

## Dependency rule

`api` and `worker` depend on `core`; `core` depends on SQLAlchemy/PostgreSQL abstractions; migrations own DDL and grants. Agent and Workflow services expose contracts only; no module imports an external tool executor, queue, vector database, or connector.

## `dxbrain.core`

| Component | Public responsibility | Contract |
|---|---|---|
| `database` | Builds process-local SQLAlchemy session factories | Receives a role-specific PostgreSQL URL; never accepts bootstrap credentials |
| `models` | Typed mappings for kernel tables | Reflects database constraints; does not execute business operations |
| `tasks.TaskService` | Creates and reads a Task aggregate; makes state transitions | `create_task` creates all kernel artifacts transactionally; replay is keyed by actor/operation/idempotency key |
| `model_calls.ModelCallService` | Persists dry-run planner call metadata and task-scoped evidence | Does not invoke transports, write long-term memory, or execute tools |
| `plan_drafts.PlanDraftService` | Deterministically parses a succeeded model response and records a candidate plan for review | Never infers executable permissions, mutates a proposal, or starts a workflow |
| `workflow.WorkflowService` | Accepts a confirmed handoff and advances a revision-pinned sequential run | Does not call tools, schedule work, retry, compensate, or resume paused runs |
| `agents.AgentService` | Registers versioned Agent contracts, lifecycle states, and declared capability checks | Does not instantiate model runtimes, delegate work, or invoke tools |

`TaskService.create_task` is the only v0.1 task-creation command path. Its transaction boundary begins with a transaction-scoped advisory lock for its idempotency scope and includes `Task`, `IdempotencyRequest`, `DomainEvent`, `EventOutbox`, `EntityState`, `StateTransition`, and `AuditEvent`.

## `dxbrain.core.governance`

`GovernanceService` owns typed `IntentProposal`, `ReviewDecision`, `ApprovalGrant`, and `ExecutionReport` commands. Each command uses a transaction-scoped idempotency lock, writes its governance row, and appends a task-scoped domain event, outbox row, and audit row atomically. Review and execution transitions are deterministic; no command invokes an LLM or performs an external side effect.

The API surface is:

- `POST /v1/tasks/{task_id}/proposals` and `GET /v1/tasks/{task_id}/proposals`
- `POST /v1/proposals/{proposal_id}/reviews` and `GET /v1/proposals/{proposal_id}/reviews`
- `POST /v1/proposals/{proposal_id}/approvals` and `GET /v1/proposals/{proposal_id}/approvals`
- `POST /v1/proposals/{proposal_id}/execution-reports` and `GET /v1/proposals/{proposal_id}/execution-reports`

All routes currently use the local root actor. A future identity/permission engine must replace that dependency before exposing these commands beyond local development.

## `dxbrain.core.archive`

`ArchiveService` owns the task-level project dossier, evidence-linked retrospective draft, candidate lesson, and human lesson review. Each command is idempotent, emits a task-scoped event/outbox row, and appends an audit record in the same transaction. A retrospective requires a closed or cancelled dossier. A lesson starts as `candidate`; only a human actor can mark it `validated` or `retracted`. No command writes the long-term Memory Layer or changes Constitution/Permission.

The API surface is:

- `POST/GET /v1/tasks/{task_id}/dossier`
- `POST/GET /v1/dossiers/{dossier_id}/retrospectives`
- `POST/GET /v1/retrospectives/{retrospective_id}/lessons`
- `POST /v1/lessons/{lesson_id}/review`

## `dxbrain.providers`

`ModelProviderRegistry` owns provider descriptors, role-level routing metadata, and construction of the two typed adapters. `DeepSeekAdapter` shapes OpenAI-compatible chat-completion requests; `AnthropicAdapter` shapes native Messages requests. Both normalize provider responses into `ModelResult` with token usage. API keys are read from server-side settings, reduced to a boolean readiness flag for the status endpoint, and never returned by it or by adapter `repr`. Adapters require an explicitly injected `Transport`; calling them without one fails closed. `HttpxTransport` is a single-attempt transport used only by the guarded live execution route; it is not selected by dry-run or readiness paths.

The authenticated read-only projection is `GET /v1/control/model-providers`.

`ModelCallService.dry_run_plan` is exposed through `POST /v1/proposals/{proposal_id}/dry-run-plan` and `GET /v1/proposals/{proposal_id}/model-calls`. It returns the proposal plan unchanged, records provider/model metadata with zero usage and cost, and emits the normal event/outbox/audit triplet. `GET /v1/proposals/{proposal_id}/model-readiness` is a read-only preflight that reports the live-call flag, provider readiness, active approval, and whether the approval scope contains `model.call`. A guarded live path first calls `POST /v1/proposals/{proposal_id}/live-plan/prepare`, then `POST /v1/model-calls/{model_call_id}/execute` with one explicit HTTP attempt, and finally persists `succeeded` or `failed`; `POST /v1/model-calls/{model_call_id}/finalize` remains available for external workers. Both transitions are audited and idempotent.

`PlanDraftService` exposes `POST /v1/model-calls/{model_call_id}/parse-plan`, `GET /v1/proposals/{proposal_id}/plan-drafts`, `GET /v1/plan-drafts/{draft_id}`, and `POST /v1/plan-drafts/{draft_id}/review`. Parsing is a bounded Markdown-list parser (`markdown-list-v1`) and produces `candidate`; only a human actor may confirm or reject it. Confirmation is an explicit governance checkpoint and has no implicit Workflow side effect. Human edits create append-only revisions (`GET /v1/plan-drafts/{draft_id}/revisions`) while the draft remains a candidate.

After confirmation, a human may create an explicit `requested` handoff with `POST /v1/plan-drafts/{draft_id}/workflow-handoff`; `GET /v1/proposals/{proposal_id}/workflow-handoffs` exposes the contract. This is only a durable handoff request for the future Workflow layer; it does not accept, schedule, execute, retry, or compensate work.

The Workflow MVP accepts a requested handoff through `POST /v1/workflow-handoffs/{handoff_id}/accept`, then exposes a run with `GET /v1/workflow-runs/{run_id}`. `POST /v1/workflow-runs/{run_id}/advance` moves one deterministic step boundary at a time; `pause` and `cancel` are explicit human controls. The run stores the exact draft revision and step snapshot, and every transition has an event/outbox/audit triplet.

## `dxbrain.api`

`api.app` contains the FastAPI application factory and no domain persistence code. It validates request size and fields, compares the local root key in constant time, passes the explicit actor to `TaskService`, maps idempotency payload mismatch to HTTP 409, and returns Task state/history. Its `/v1/control/*` routes are authenticated read-only projections for the Control UI. It does not publish events or modify tables directly.

## `dxbrain.worker`

`worker.outbox.OutboxWorker` claims one row under `FOR UPDATE SKIP LOCKED`, commits a bounded lease, inserts the consumer/event pair into the inbox with `ON CONFLICT DO NOTHING`, then marks the outbox published. The `dxbrain-worker` command intentionally handles one event and exits; scheduling, retry policy, and external side effects are deferred decisions.

## Evolution seams

- A future Event Layer can replace the worker's internal handler only by preserving outbox and inbox contracts.
- A future Permission Engine can replace the root-key dependency only by passing a typed actor/authority result to the command.
- Memory, Agent, and Workflow modules must communicate through durable events/state contracts rather than importing HTTP routes or internal SQLAlchemy sessions.
- `apps/control-ui` consumes authenticated FastAPI read models; it never imports SQLAlchemy or connects to PostgreSQL. Its first slice is GET-only and holds the API key in tab memory only.
