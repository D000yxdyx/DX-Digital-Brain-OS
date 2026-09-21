# AgentExecution Persistence Design (V0.2)

- Status: **Proposed / Candidate** (design only — no migration, no code)
- Extends: `ADR-0010-agent-execution-gateway.md` (committed on `agent-execution-bridge-v02`, `d06afe8`)
- Adjudication: **E.0.1** — `reported` removed from Phase 1 lifecycle (placeholder once ExecutionReport
  linkage is deferred); `dispatched` semantics + all failure paths clarified; `dx_worker` (read-only) /
  `dx_api` (owns creation + lifecycle mutation) boundary, OutboxWorker→Execution Gateway call path, and
  task-anchored DomainEvent infrastructure constraint documented in §13.
- Scope: repository-grounded persistence design for `AgentExecution` only.
- Out of scope (this document): Alembic migration, SQLAlchemy model changes, service/adapter code,
  worker changes, API changes, Multica/Codex/Claude integration, `tests/conftest.py` edits.
- All schema/constraint/grant text below is **Proposed / Candidate** and MUST NOT be treated as
  implemented fact. The migration boundary is in §18.

---

## 1. Method — repository-grounded, not ADR-derived

Every decision below is grounded in existing code, not assumed. Files inspected (read-only):

- `src/dxbrain/core/models.py` — all ORM tables, schema tags, PK/UUID/FK/status/JSONB conventions.
- `src/dxbrain/core/governance.py` — `_record_event` atomic outbox helper (`:128`), `_lock` advisory
  xact lock (`:118`), `report_execution` (`:431`), `GovernancePayloadMismatch` (`:39`).
- `src/dxbrain/core/model_calls.py` — `_lock` (`:93`), `_payload_hash` (`:72`),
  `ModelCallPayloadMismatch` (`:26`), two-phase prepare/finalize idempotency (`:244`, `:368`).
- `src/dxbrain/worker/outbox.py` — `OutboxWorker.consume_one` lease pattern (`:23`),
  `EventInbox` upsert-on-conflict (`uq_inbox_consumer_event`, `:49`).
- `tests/conftest.py` — hardcoded TRUNCATE list and FK cleanup order (`:80`).
- `migrations/versions/20260826_0010_workflow_mvp.py` (`:98`) and `_0012_agent_contract.py` (`:76`) —
  schema creation + `dx_api`/`dx_worker` grant pattern.
- `migrations/versions/20260826_0011_workflow_handoff_update_grant.py` — incremental `GRANT UPDATE` pattern.

---

## 2. Existing persistence patterns discovered (reuse targets)

| Concern | Existing pattern (file:line) | Reuse decision |
|---|---|---|
| PK | `UUID` `primary_key=True, default=uuid4` on every table (`models.py`) | Reuse: `id UUID` PK. |
| Schema tag | `__table_args__ = (..., {"schema": "dx_X"})`; no cross-schema mixing inside one table | Reuse: single schema per table. |
| FK style | `ForeignKey("dx_core.tasks.id")` explicit schema-qualified (`models.py:57`) | Reuse: fully schema-qualified FKs. |
| Status enum | `CheckConstraint("status IN (...)", name="ck_*_status")` (`models.py`) | Reuse: `ck_agent_execution_status`. |
| JSONB payload | `JSONB` for structured/extensible data (`scope`, `plan`, `artifacts`, `response_summary`) | Reuse for `request_input`, `constraints`, `artifacts`, `log_references`. |
| UTC time | `DateTime(timezone=True), server_default=func.now()`; code uses `datetime.now(UTC)` | Reuse: all timestamps timezone-aware UTC. |
| Idempotency | `actor_type`(32) + `actor_id`(128) + `idempotency_key`(128) + `request_hash`(64, sha256); `UniqueConstraint`; replay → `PayloadMismatch` on hash mismatch (`governance.py:197`, `model_calls.py:151`) | Reuse exactly. |
| Concurrency | `pg_advisory_xact_lock(hashtextextended(:lock_key,0))` (`governance.py:122`, `model_calls.py:97`) | Reuse for create/transition critical sections. |
| Atomic outbox | In ONE `session_factory.begin()`: domain row → `DomainEvent` → flush → `EventOutbox` → `AuditEvent` (`governance.py:128-157`) | Reuse for REQUESTED creation. |
| Event naming | `aggregate.verb.vN` (`governance.proposal.created.v1`, `model.planner.finalized.v1`) | Reuse: `agent_execution.*.v1`. |
| Event bus key | `DomainEvent.aggregate_id` is FK → `dx_core.tasks.id` (`models.py:608`); `aggregate_type` is a string | Reuse: `aggregate_type="task"`, `aggregate_id=task_id`; put `agent_execution_id` in payload. |
| Outbox worker | `EventOutbox` (`pending→processing→published`) + `EventInbox` (`uq_inbox_consumer_event`) lease (`outbox.py`) | Reuse the SAME worker; no second queue. |
| Roles/grants | `GRANT USAGE ON SCHEMA ... TO dx_api, dx_worker`; `dx_api` = SELECT/INSERT/UPDATE; `dx_worker` = SELECT only (`migrations/0010:98`, `0012:76`) | Reuse: lifecycle writes run as **dx_api**. |
| Test cleanup | `conftest.py:80` TRUNCATE list ordered children-before-parents (no CASCADE) | New table inserted at correct FK order. |

---

## 3. Proposed schema ownership

**Decision: `agent_executions` lives in schema `dx_workflow`.**

Rationale (per user principle #18 — reuse existing domain boundary, do not create a schema for looks):

1. The strongest relationship of an `AgentExecution` is to a **specific approved workflow step**
   (`dx_workflow.workflow_steps`). The execution *is* the technical doing of that step via an external
   agent/workforce provider. The FK to `workflow_steps` is natural and within-reach.
2. `AgentExecution` already carries `workflow_run_id`, `workflow_step_id`, `draft_id`, `draft_revision`
   — all workflow-domain identities (the "execution-time identity snapshot", §7). Placing it in
   `dx_workflow` keeps these FKs coherent with the workflow boundary.
3. It parallels `ModelCall` (in `dx_model`): both are "external provider invocations" bound to a
   governance proposal. `dx_workflow` is the correct home for the *execution* counterpart of
   `workflow_steps`, just as `dx_model` is the home for model calls.

**Non-blocking alternative (DEFERRED):** if a future phase needs AgentExecution usable *outside* a
workflow (standalone agent tasks not tied to a `workflow_step`), a dedicated `dx_execution` schema
could be introduced in a later ADR. Phase 1 does NOT create a new schema.

No new schema is created in Phase 1 (`dx_workflow` already exists since migration `0010`).

---

## 4. Canonical identity

- `agent_executions.id` (UUID) = **canonical DX AgentExecution identity**. Generated by DX, owned by DX.
- `external_execution_id` (String, nullable) = provider issue id (Multica issue id / Codex execution id
  / Claude execution id / Ollama run id). **It can NEVER be PK, workflow identity, or task identity.**
  It is a correlation handle only, populated after dispatch.

---

## 5. Proposed `agent_executions` table (Candidate)

> All items below are **Proposed / Candidate**. Column names, types, nullability, constraints, and
> indexes are design intent for the Phase-1 migration in §18 — not executable yet.

| Column | Type | Null | FK / Constraint | Purpose |
|---|---|---|---|---|
| `id` | UUID | N | PK, default uuid4 | Canonical DX AgentExecution ID |
| `task_id` | UUID | N | FK `dx_core.tasks.id` | Governance task context (every table carries this) |
| `proposal_id` | UUID | N | FK `dx_governance.intent_proposals.id` | Execution derives from an approved proposal |
| `approval_id` | UUID | N | FK `dx_governance.approval_grants.id` | The approval grant that authorized this execution (enforces "execute only approved revision") |
| `workflow_run_id` | UUID | N | FK `dx_workflow.workflow_runs.id` | Bound run |
| `workflow_step_id` | UUID | N | FK `dx_workflow.workflow_steps.id` | Primary execution-time identity: the step being executed |
| `draft_id` | UUID | N | FK `dx_governance.plan_drafts.id` | Pinned approved draft |
| `draft_revision` | Integer | N | `ck_agent_execution_revision_positive` (`>=1`) | Pinned revision (snapshot, not live-followed) |
| `agent_role` | String(64) | N | — | Snapshot of executing agent role / requested capability role |
| `agent_id` | UUID | Y | **No FK in Phase 1** (see §6) | Optional reference to a DX-registered agent; external providers leave this null |
| `provider` | String(32) | N | — | Symbolic provider key (`multica`/`codex`/`claude`/`ollama`); no provider-registry FK (registry deferred) |
| `capability` | String(128) | N | — | Snapshot of `workflow_steps.capability` (what is being executed) |
| `workspace_ref` | String(64) | Y | `ck_agent_execution_workspace_ref_symbolic` (see §10) | Server-controlled symbolic workspace key, NOT a path |
| `status` | String(16) | N | default `requested`; `ck_agent_execution_status` (5 states, §11) | Lifecycle state |
| `request_input` | JSONB | N | — | Structured execution input (NOT identity/revision/status/provider) |
| `constraints` | JSONB | Y | — | Execution constraints (timeout, resource limits, allowed tools, etc.) |
| `external_execution_id` | String(255) | Y | — | Provider issue/execution id (correlation only) |
| `summary` | Text | Y | — | Human-readable result summary |
| `artifacts` | JSONB | Y | — | Structured artifact **metadata / references**, not raw blobs (mirrors `execution_reports.artifacts`) |
| `log_references` | JSONB | Y | — | References to logs (URIs/keys), not raw stdout/stderr (see §12) |
| `result_tail` | Text | Y | — | Truncated output tail (e.g. ≤4 KB) for quick inspection; full logs stay external |
| `exit_code` | Integer | Y | — | Provider exit code |
| `error_code` | String(64) | Y | — | Failure code (mirrors `model_calls.error_code`) |
| `error_detail` | Text | Y | — | Failure detail text |
| `idempotency_key` | String(128) | N | `uq_agent_execution_idempotency` | Idempotency (§14) |
| `request_hash` | String(64) | N | — | sha256 of sorted request payload (§14) |
| `actor_type` | String(32) | N | — | Idempotency actor (§14) |
| `actor_id` | String(128) | N | — | Idempotency actor (§14) |
| `created_at` | DateTime(tz) | N | server_default now() | REQUESTED timestamp |
| `dispatched_at` | DateTime(tz) | Y | — | DISPATCHED timestamp |
| `started_at` | DateTime(tz) | Y | — | RUNNING timestamp |
| `finished_at` | DateTime(tz) | Y | — | SUCCEEDED/FAILED timestamp |
| `updated_at` | DateTime(tz) | N | server_default now(), onupdate now() | Row mutation time |

Constraints:

- `UniqueConstraint("actor_type","actor_id","idempotency_key", name="uq_agent_execution_idempotency")`
- `CheckConstraint("status IN ('requested','dispatched','running','succeeded','failed')", name="ck_agent_execution_status")`
- `CheckConstraint("draft_revision >= 1", name="ck_agent_execution_revision_positive")`
- `CheckConstraint("workspace_ref IS NULL OR workspace_ref ~ '^[a-z0-9][a-z0-9_-]*$'", name="ck_agent_execution_workspace_ref_symbolic")` — enforces *symbolic, not path* (rejects `/`, `\`, leading-slash). Optional but recommended.

Indexes (candidate):

- `ix_agent_executions_task_created (task_id, created_at)`
- `ix_agent_executions_step_status (workflow_step_id, status)`
- `ix_agent_executions_proposal_created (proposal_id, created_at)`

---

## 6. FK vs snapshot decisions (per relationship)

| Field | Decision | Reason |
|---|---|---|
| `task_id` | **FK** `dx_core.tasks.id`, NOT NULL | Universal governance context; every table has it. |
| `proposal_id` | **FK** `dx_governance.intent_proposals.id`, NOT NULL | Execution derives from a proposal; referential integrity to governance. |
| `approval_id` | **FK** `dx_governance.approval_grants.id`, NOT NULL | ADR-0010 requires executing only an *approved* revision. Recording the specific grant enforces this at the persistence layer and is auditable. |
| `workflow_run_id` | **FK** `dx_workflow.workflow_runs.id`, NOT NULL | Execution bound to a run. |
| `workflow_step_id` | **FK** `dx_workflow.workflow_steps.id`, NOT NULL | Primary execution-time identity (the step being executed). |
| `draft_id` | **FK** `dx_governance.plan_drafts.id`, NOT NULL | Pinned approved draft; referential integrity. |
| `draft_revision` | **Plain Integer**, NOT NULL, `>=1` | Revision is an int *within* a draft, not a separate entity; FK would be wrong. Pinned at request time (§7). |
| `agent_role` | **Plain String snapshot**, NOT NULL | Avoids future drift if the agent contract changes/retires; records the requested role at execution time. |
| `agent_id` | **Nullable UUID, NO FK in Phase 1** (DEFERRED linkage) | External providers (Multica/Codex/Claude workforce) may not correspond to a `dx_agent.agents` row. Forcing a FK would break external executions. The DX agent registry (`dx_agent`) is currently contract-only with no execution linkage yet; formal FK is a deferred decision (§19). |
| `provider` | **Plain String**, NOT NULL | Provider registry is deferred (ADR-0010); `provider` is a symbolic key, exactly like `model_calls.provider` (`models.py:197`). |
| `capability` | **Plain String snapshot**, NOT NULL | Snapshot of `workflow_steps.capability`; avoids drift if the step is later revised. |
| `workspace_ref` | **Plain String**, nullable (§10) | No workspace/project registry exists; symbolic key only. |

**Principle applied:** referential integrity where the target is a stable, first-class entity DX owns
(tasks, proposals, approvals, runs, steps, drafts); **snapshot** (plain column) where the target is
external, volatile, or not-yet-modeled (agent contract, provider, capability, workspace).

---

## 7. Revision binding (execute only the approved revision)

At REQUESTED creation, capture a frozen execution-time identity snapshot:

- `draft_id` (FK) + `draft_revision` (int) taken from `workflow_runs.draft_id` / `draft_revision`
  (which itself was pinned at handoff via `workflow_handoffs.draft_revision`, `models.py:313`).
- `workflow_run_id` + `workflow_step_id` identify the exact step.
- `approval_id` records the grant that authorized it.

The execution MUST NOT live-follow a newer revision published after creation. Because `draft_revision`
is a plain pinned column (not a live FK), the execution permanently references the revision it was
authorized against. Any later `plan_draft_revisions` row does not affect an already-created execution.

This mirrors the existing pinning in `WorkflowRun` (`draft_revision` column, `models.py:349`) and
`WorkflowHandoff` (`draft_revision`, `models.py:313`).

---

## 8. ExecutionRequest input / constraints (what goes in JSONB vs columns)

Following user principle #11 — identity/revision/status/provider stay as **columns**, not payload:

- **Columns (first-class):** `task_id`, `proposal_id`, `approval_id`, `workflow_run_id`,
  `workflow_step_id`, `draft_id`, `draft_revision`, `provider`, `capability`, `agent_role`,
  `workspace_ref`, `status`.
- **`request_input` JSONB (NOT NULL):** the structured execution input — the step's `parameters`
  (`workflow_steps.parameters`, `models.py:380`) plus any execution-specific arguments. This is the
  legitimate "extensible structured data" use of JSONB, consistent with `ModelCall.request_summary`.
- **`constraints` JSONB (nullable):** execution constraints (e.g. `{"timeout_sec": 300,
  "max_tokens": 4096, "allowed_tools": [...]}`). Extensible, non-identity.
- **Explicitly NOT in JSONB:** task_id, approval_id, workflow_step_id, status, provider, draft_revision.
  Hiding these in a payload would defeat FK integrity and queryability.

---

## 9. ExecutionResult (artifacts / logs — keep PostgreSQL lean)

V0.2 stores **metadata and references**, never raw large blobs (user principle: do not write unlimited
stdout/stderr/blob into PostgreSQL).

- `external_execution_id` String(255) — provider correlation id (populated after dispatch).
- `summary` Text — short human-readable outcome.
- `artifacts` JSONB — structured **references**, e.g.
  `{"type":"file","uri":"s3://bucket/key","byte_size":1234,"captured_at":"..."}`. Mirrors
  `execution_reports.artifacts` (`models.py:161`).
- `log_references` JSONB — references to logs, e.g.
  `[{"stream":"stdout","uri":"...","byte_size":...,"captured_at":"..."}]`. No raw log text stored.
- `result_tail` Text — a **truncated** tail (≤4 KB) for quick inspection without fetching external logs.
- `exit_code` Integer — provider exit code.
- `error_code` String(64) / `error_detail` Text — failure info (mirrors `model_calls.error_code`).

**No repository artifact/log table exists today**, so Phase 1 designs only the minimal JSONB reference
structure above. A dedicated artifact/blob store is explicitly OUT of scope (§18) and deferred.

---

## 10. workspace_ref persistence

1. **Stored?** Yes — `workspace_ref` column on `agent_executions`.
2. **Type / constraint:** `String(64)`, nullable, with
   `ck_agent_execution_workspace_ref_symbolic` forbidding path characters (`/`, `\`, leading slash).
   It is a **symbolic key** (`default`, `p001`, `math-modeling`), never a filesystem path.
3. **Nullable?** Yes. Some executions may not need a workspace, or workspace resolution is deferred to
   dispatch time. `NULL` is permitted and means "no workspace resolved / not required".
4. **FK?** No. There is no workspace/project registry in the repository (`models.py` has no
   `project_id`/`workspace` field on any table). Storing `workspace_ref` as a controlled symbolic
   string is the allowed direction (ADR-0010 §Ten).
5. **Resolution:** at dispatch, the server resolves `workspace_ref` → physical workspace via a
   server-side allowlist/config (unknown → **deny**, no default path fallback, per ADR-0010). The
   *location* of that mapping (static config vs minimal registry) is **DEFERRED** — not created in
   Phase 1.

---

## 11. Lifecycle & transition matrix (E.0.1 frozen)

**Frozen Phase-1 lifecycle (5 states, no `reported`):**

```
requested → dispatched → running → succeeded
                             └──────→ failed
```

`reported` was REMOVED from the Phase-1 lifecycle in E.0.1. It only had meaning once
`AgentExecution → Governance ExecutionReport` linkage is established, and Phase 1 explicitly defers that
linkage (ADR-0010; §15). Keeping `reported` as a placeholder state would have been inventing a
persistence meaning it does not have in Phase 1, so it is dropped rather than stubbed. A future phase
that wires `reported` to the governance report will re-introduce the state + `reported.v1` event via a
new ADR — it is **not** silently added here.

`CheckConstraint` (frozen): `status IN ('requested','dispatched','running','succeeded','failed')`.

**Precise semantics of each state:**

- `requested` — DX has created the `agent_executions` row (status `requested`) and written
  `agent_execution.requested.v1` atomically (§13). The execution is authorized, identity-snapshotted
  (§7), and waiting for dispatch. It is a DX-internal fact; no provider has been contacted yet.
- `dispatched` — **DX-side fact, not provider confirmation.** The Execution Gateway has accepted the
  dispatch request, validated it, resolved the (optional) `workspace_ref` (deny-on-unknown, §10), and
  *forwarded* it to the provider (created the provider-side job / stored `external_execution_id`).
  Reaching `dispatched` does **NOT** mean the provider has started, accepted, or acked the run — only
  that DX handed it off. This is intentionally a single DX-controlled transition so a provider outage
  or rejection after handoff is modelable (see `dispatched → failed`).
- `running` — the provider has started executing (DX received a start/accepted signal, or the adapter
  polled a `running` provider status). Provider-internal, reflected back into DX.
- `succeeded` — execution finished successfully (provider exit code 0 / success signal). `SUCCEEDED ≠
  Task DONE` (acceptance #8): this is an execution-level terminal, the governance Task is NOT auto-closed.
- `failed` — execution failed. Terminal. Carries `error_code` / `error_detail` (§9). A `running`
  timeout is modeled as `failed` with `error_code = 'timeout'` (DEFERRED retry sub-machine, below).

**Legal transitions (frozen matrix):**

| From | To | Allowed | Failure class | Note |
|---|---|---|---|---|
| requested | dispatched | ✅ | — | DX forwards to provider (`agent_execution.dispatched.v1`) |
| requested | failed | ✅ | pre-dispatch | Terminal. DX-side validation / provider-unreachable *before* handoff (e.g. bad request, workspace deny, provider API down at submit time). `error_code` set. |
| dispatched | running | ✅ | — | Provider started (`agent_execution.started.v1`) |
| dispatched | failed | ✅ | post-dispatch | **Terminal.** Provider/network failure *after* DX handoff but *before* provider start: provider rejects the job, network drop on ack, dispatch-time auth/expiry. (Workspace-ref resolution is part of *reaching* `dispatched` — a workspace-resolution failure is the **pre-dispatch** `requested → failed` edge, NOT this one.) This is the path that makes `dispatched → failed` mandatory — without it, a post-dispatch failure would be orphaned (stuck in `dispatched` forever). |
| running | succeeded | ✅ | — | Execution finished OK (`agent_execution.succeeded.v1`) |
| running | failed | ✅ | runtime | Execution error / crash / `timeout` (`error_code='timeout'`) (`agent_execution.failed.v1`) |

Illegal (deliberately absent): any transition into a non-listed state; `requested → running` /
`requested → succeeded` (must pass through `dispatched`); `running → dispatched` (no re-dispatch);
`* → cancelled` (ADR-0010 excludes CANCELLED); auto `succeeded → done` (no such transition — `succeeded`
is terminal, see acceptance #8). `succeeded` and
`failed` are both terminal in Phase 1 — there is no `reported`/`done` edge.

**Every failure path has a valid lifecycle exit (E.0.1 adjudication):**

1. Pre-dispatch failure → `requested → failed` (DX never contacted provider, provider down at submit,
   **or `workspace_ref` resolution deny** — resolution is a precondition of reaching `dispatched`, so its
   failure is classified here, not under post-dispatch).
2. Post-dispatch failure → `dispatched → failed` (provider reject / network ack-drop / dispatch-time
   auth expiry). **This is why `dispatched → failed` MUST be legal** — a `dispatched` execution that
   cannot reach `running` needs a terminal, not an orphaned row.
3. Runtime failure / timeout → `running → failed` (`error_code='timeout'` for the timeout case).

No failure leaves an AgentExecution in a non-terminal limbo in Phase 1.

**V0.2 gap (DEFERRED, not a new state):** if a `running` execution times out, V0.2 models it as
`failed` with `error_code = 'timeout'`. A dedicated `timed_out` state / retry sub-machine is a future
extension, recorded here as DEFERRED so it does not silently expand the state machine.

Transition enforcement reuses the advisory-xact-lock + status-guard pattern already in
`governance.py:303` (`if proposal.status not in (...): raise GovernanceConflict`) and
`model_calls.py:423` (`if call.status != "prepared": raise ModelCallConflict`): every lifecycle UPDATE
first re-takes the row advisory lock, then asserts `status` is in the legal pre-state set for the
target transition, else raises `AgentExecutionConflict` (PROPOSED, mirror of `GovernanceConflict` /
`ModelCallConflict`).

---

## 12. Idempotency design

Reuses the repository's exact pattern (§2, `governance.py:197-217`, `model_calls.py:151-163`):

- Columns `actor_type`(32) + `actor_id`(128) + `idempotency_key`(128) + `request_hash`(64), with
  `UniqueConstraint("actor_type","actor_id","idempotency_key", name="uq_agent_execution_idempotency")`.
- `_payload_hash` = sha256 of `json.dumps(payload, sort_keys=True, separators=(",",":"),
  ensure_ascii=False)` (identical to `governance.py:100` / `model_calls.py:72`).
- Create flow (within `session_factory.begin()` + `pg_advisory_xact_lock`):
  1. Lock on `(actor_type, actor_id, idempotency_key)`.
  2. Select existing by those three columns. If found:
     - same `request_hash` → **replay**: return the existing execution (idempotent create).
     - different `request_hash` → raise `AgentExecutionPayloadMismatch` (mirror
       `GovernancePayloadMismatch` / `ModelCallPayloadMismatch`).
  3. Else insert the `requested` row + outbox event (§13).

Answers to the four required questions:

1. **Same approved step re-requested** → the caller-supplied `idempotency_key` (e.g. derived from
   `workflow_step_id + draft_revision`) hits `uq_agent_execution_idempotency`; replay returns the
   existing execution instead of creating a second logical one.
2. **Outbox retry avoiding duplicate dispatch** → the REQUESTED row + `agent_execution.requested.v1`
   outbox event are written atomically (§13). The `OutboxWorker` lease
   (`pending→processing→published` + `EventInbox.uq_inbox_consumer_event`, `outbox.py:23-61`) ensures the
   event is consumed exactly once. A retry replays the outbox row, not a new execution. The dispatch
   handler additionally guards: only a `requested` execution may transition to `dispatched` (§11).
3. **execution_id as provider idempotency key** → `agent_executions.id` (DX UUID) may be passed to the
   provider as its idempotency token so provider-side retries map to the same external execution.
4. **Provider without idempotency support** → DX-local protection is primary: the advisory xact lock +
   `UniqueConstraint` + outbox lease. The dispatch handler is retry-safe (status pre-check before
   dispatch). No SQL locking implementation is designed here (per user instruction) — it reuses the
   existing `pg_advisory_xact_lock(hashtextextended(...))` primitive.

---

## 13. Outbox transaction design

**AgentExecution REQUESTED creation writes, in ONE DB transaction** (mirrors `governance.py:128-157`):

```
with session_factory.begin() as session:
    advisory_xact_lock(...)                      # idempotency (§12)
    execution = AgentExecution(status="requested", ...)   # domain row
    session.add(execution); session.flush()
    event = DomainEvent(
        aggregate_type="task",                   # FK requires task_id (models.py:608)
        aggregate_id=task_id,
        event_type="agent_execution.requested.v1",   # FROZEN by ADR-0010
        payload={"agent_execution_id": str(execution.id), "workflow_step_id": ..., "status": "requested"},
    )
    session.add(event); session.flush()
    session.add(EventOutbox(event_id=event.id, status="pending"))
    session.add(AuditEvent(actor_type=..., actor_id=..., operation="agent_execution.request.requested",
                           task_id=task_id, event_id=event.id, detail=payload))
```

- **Same transaction** as required (user principle #15). No second queue.
- `DomainEvent.aggregate_id` is FK → `dx_core.tasks.id`, so `aggregate_type="task"` and `agent_execution_id`
  lives in the payload (consistent with every existing event, `governance.py:138`).

  > **Infrastructure-compatibility constraint — NOT an identity claim (E.0.1 explicit).** The
  > `AgentExecution` IS a first-class entity with its own canonical identity `agent_executions.id` (UUID,
  > §4). The reason its events are *task-anchored* (`aggregate_type="task"`, `aggregate_id=task_id`) is
  > purely that `DomainEvent.aggregate_id` is today hard-FK'd to `dx_core.tasks.id` (`models.py:608`) — an
  > **infrastructure compatibility constraint of the existing event schema**, not a statement that
  > AgentExecution "lacks identity" or is subordinate to Task. In Phase 1 the `agent_execution_id` is
  > always carried in the event payload so the execution can be correlated independently of the Task
  > anchor. A future phase that widens `DomainEvent.aggregate_id` (or adds a typed aggregate FK) could
  > re-anchor these events to `agent_executions.id`; that is an event-schema change, out of scope here.
- The new `agent_execution.requested.v1` consumer is a **new OutboxWorker handler** (future
  implementation) but reuses the existing `EventOutbox`/`EventInbox`/`OutboxWorker` machinery — no
  parallel queue.

---

## 14. Audit design (AuditEvent vs DomainEvent vs Outbox)

Three distinct purposes, all already modeled:

- **DomainEvent** (`dx_events.domain_events`) — state-change facts for the event bus / projections.
  Proposed candidate events (only `requested.v1` is frozen by ADR-0010; the rest are PROPOSED for V0.2):
  `agent_execution.requested.v1` (frozen), `agent_execution.dispatched.v1`,
  `agent_execution.started.v1`, `agent_execution.succeeded.v1`, `agent_execution.failed.v1`. Each is
  wrapped into `EventOutbox` for async dispatch.
- **AuditEvent** (`dx_audit.audit_events`) — append-only *who-did-what* trail. Emitted for every
  significant action: `request`, `dispatch`, `start`, `success`, `failure` (operations like
  `agent_execution.request.requested`, `agent_execution.lifecycle.dispatched`, etc.). AuditEvents are
  created alongside the corresponding DomainEvents in the same transaction (per §13 pattern).
- **Outbox Event** — the `EventOutbox` row carrying the `DomainEvent.id`; consumed by `OutboxWorker`.

Not every status needs its own DomainEvent by mechanical rule, but for AgentExecution the lifecycle
transitions are meaningful domain events, so the five listed above are proposed. Each transition also
writes one `AuditEvent`. **`reported.v1` is deliberately absent in Phase 1** (E.0.1): `reported` was
removed from the lifecycle because the `AgentExecution → ExecutionReport` linkage is deferred (§15), so
there is no Phase-1 terminal event to emit. `succeeded.v1` / `failed.v1` are the execution-level terminal
events in Phase 1; neither auto-generates a governance report.

---

## 15. Governance ExecutionReport separation

- `AgentExecution` (this table) ≠ `ExecutionReport` (`dx_governance.execution_reports`, `models.py:137`).
  Different concepts, different schemas, no shared table.
- **No new FK** `execution_reports.agent_execution_id` is added in Phase 1 (user principle #17). The
  worker does **not** auto-create a governance `ExecutionReport` at any Phase-1 terminal state
  (`succeeded`/`failed`) — there is no `reported` state to hang it on (E.0.1).
- If a future human/governance action wants to reference an agent execution, the schema linkage is
  **DEFERRED** (a later ADR may add a linkage column or table). Phase 1 migration must not pre-empt it.
- `agent_executions` does not reference `execution_reports` either — clean separation preserved.

---

## 16. Grants / role considerations

Reuses the established grant pattern (`migrations/0010:98-110`, `0012:76-87`):

```sql
-- dx_workflow USAGE already granted in migration 0010; extend table-level grants:
GRANT SELECT, INSERT, UPDATE ON dx_workflow.agent_executions TO dx_api;
GRANT SELECT                ON dx_workflow.agent_executions TO dx_worker;
```

**Key repository-grounded constraint (user principle #19):** the existing pattern gives `dx_worker`
**SELECT only** on domain tables; all INSERT/UPDATE on domain tables go to `dx_api`. Therefore the
AgentExecution lifecycle writes (REQUESTED→…→SUCCEEDED/FAILED status UPDATEs) are performed by the **Execution
Gateway service running as `dx_api`**, NOT by `dx_worker`. `dx_worker` remains the outbox/inbox
consumer (read-only on domain tables). This must be reflected in the future service design.

### Privilege / runtime boundary (E.0.1 explicit)

The boundary is stated once and must not be widened for convenience during implementation:

- **`dx_api` owns AgentExecution creation AND every lifecycle mutation.** Both the REQUESTED row insert
  (§13) and all status transitions (`requested→dispatched`, `dispatched→running`, `running→succeeded`,
  `running→failed`, and the two pre/post-dispatch `failed` paths from §11) run as `dx_api`. `dx_api` is
  the role the Execution Gateway service authenticates as.
- **`dx_worker` is strictly read-only on `dx_workflow.agent_executions`** (SELECT only, per §16 grant).
  It never INSERTs or UPDATEs an execution row. Do NOT grant `dx_worker` INSERT/UPDATE on
  `agent_executions` merely to let the outbox consumer "flip status directly" — that would bypass the
  Gateway and the advisory-lock + status-guard enforcement (§11).

**Expected call path — OutboxWorker → Execution Gateway (E.0.1):**

```
[dx_api]  API / workflow handoff layer
            └─ creates AgentExecution (status=requested) + agent_execution.requested.v1
               atomically (§13)                          ── runs as dx_api
[db]      EventOutbox (pending)  ─── written in same tx

[dx_worker]  OutboxWorker.consume_one        ── runs as dx_worker (SELECT only on domain tables)
                leases EventOutbox (pending→processing→published)
                reads agent_execution.requested.v1 payload (agent_execution_id, workflow_step_id)
                └─ CALLS the Execution Gateway (dx_api service)  ← cross-role call, NOT a direct DB write
                        ├─ Gateway validates + resolves workspace_ref (deny-on-unknown, §10)
                        ├─ Gateway forwards to provider, stores external_execution_id
                        └─ Gateway transitions requested → dispatched (runs as dx_api)
                            └─ Gateway later records running/succeeded/failed (as provider signals arrive)

[db]       EventInbox upsert (uq_inbox_consumer_event) dedup; OutboxWorker marks published
```

The point: `dx_worker` (OutboxWorker) only *reads* the event and *invokes* the Gateway; the Gateway —
authenticated as `dx_api` — performs the actual creation-adjacent dispatch and all status mutations. This
keeps the read-only `dx_worker` contract intact and forces all lifecycle logic through the single
Gateway path (single place for the advisory-lock + status-guard enforcement of §11). Implementation must
not "optimize" by giving `dx_worker` write rights.

- Do **NOT** apply the append-only `AuditEvent` grant style to `agent_executions` — it is mutable and
  requires `UPDATE` (status transitions). Grant `UPDATE` to `dx_api` like other domain tables.
- No `DELETE` granted to `dx_api` on `agent_executions` in Phase 1 (lifecycle is status-driven, not
  delete-driven). Revocation/retention is handled by status, not row deletion.
- The migration role (used to run Alembic) already owns DDL; no new role is introduced.

---

## 17. `tests/conftest.py` impact (design only — do NOT edit this phase)

`conftest.py:80` TRUNCATEs in FK order (children before parents, no CASCADE). `agent_executions`
references `dx_workflow.workflow_steps` (and `workflow_runs`, `plan_drafts`, `intent_proposals`,
`tasks`), so it must be truncated **before** those parents.

Proposed insertion point — add `dx_workflow.agent_executions` immediately before
`dx_workflow.workflow_steps` in the existing list:

```sql
TRUNCATE dx_archive.lessons, dx_archive.retrospectives, dx_archive.project_dossiers,
         dx_workflow.workflow_commands,
         dx_workflow.agent_executions,            -- NEW (Phase 1 migration)
         dx_workflow.workflow_steps, dx_workflow.workflow_runs,
         dx_governance.workflow_handoffs,
         dx_governance.plan_draft_revisions, dx_governance.plan_drafts,
         dx_agent.agent_lifecycle_commands, dx_agent.agents,
         dx_model.model_calls,
         dx_governance.execution_reports, dx_governance.approval_grants,
         dx_governance.review_decisions, dx_governance.intent_proposals,
         dx_audit.audit_events, dx_events.event_inbox, dx_events.event_outbox,
         dx_state.state_transitions, dx_state.entity_states, dx_events.domain_events,
         dx_core.idempotency_requests, dx_core.tasks
```

This keeps `agent_executions` cleaned before its FK parents. The actual edit belongs to the Phase-1
implementation slice (migration + conftest together, per user decision #9 in prior phases).

---

## 18. Minimal migration boundary (Phase 1)

The Phase-1 migration (candidate name `20260921_0013_agent_execution_persistence`) creates **only**:

1. `agent_executions` table in existing schema `dx_workflow` (columns/constraints/indexes per §5).
2. The `GRANT` statements per §16 (extend, not replace, existing `dx_workflow` grants).

It MUST NOT create, in the same migration:

- a workspace registry / project registry,
- `tool_executions`,
- a provider registry,
- an artifact/blob store,
- a scheduler / retry queue,
- any `execution_reports.agent_execution_id` linkage,
- `dx_state.entity_states`/`state_transitions` linkage for agent_executions (deferred; the `status`
  column on the table is sufficient for V0.2, mirroring `workflow_steps`/`model_calls`).

If a second migration is needed later (e.g. `agent_id` FK, workspace mapping table, execution-report
linkage), it is a separate, independently reviewed change — not bundled here.

---

## 19. Open questions

### BLOCKING
- None. The repository provides every pattern needed for a minimal Phase-1 persistence design.

### NON-BLOCKING (decide during implementation)
- **`agent_id` linkage:** Phase 1 stores `agent_id` as a nullable UUID with **no FK** (external
  providers may not map to `dx_agent.agents`). Decision: add a real FK to `dx_agent.agents.id` later
  once the agent-registry↔execution relationship is finalized, OR keep it a free UUID reference. Either
  is compatible with this design.
- **`workspace_ref` mapping storage:** where the symbolic→physical mapping lives (static config vs
  minimal registry). Deferred; does not block Phase 1 (column + deny-on-unknown is enough).

### DEFERRED (explicitly out of V0.2 scope)
- `CANCELLED` state and any cancellation semantics (ADR-0010).
- Dedicated `timed_out` state / retry sub-machine (modeled as `failed` + `error_code='timeout'` for now).
- `tool_executions` (separate future concept).
- Provider registry (`provider` stays a symbolic String).
- Workspace/project registry.
- Artifact/blob store (Phase 1 stores JSONB references only).
- `ExecutionReport ↔ AgentExecution` schema linkage (no auto-creation; future human/governance action).
- `dx_state` projection linkage for agent_executions.

---

## 20. Acceptance checklist (user principle #24)

| # | Principle | Met by |
|---|---|---|
| 1 | provider-neutral | `provider` is a symbolic String; no Multica coupling |
| 2 | no Multica coupling | no Multica import/table; `external_execution_id` is generic |
| 3 | no second approval system | reuses `approval_grants` FK; no new approval entity |
| 4 | revision pinned | `draft_id`+`draft_revision` pinned at REQUESTED (§7) |
| 5 | server-controlled workspace | `workspace_ref` symbolic + deny-on-unknown (§10) |
| 6 | atomic outbox compatible | REQUESTED writes row+event+outbox+audit in one tx (§13) |
| 7 | idempotent create/dispatch | `uq_agent_execution_idempotency` + advisory lock + outbox lease (§12) |
| 8 | SUCCEEDED ≠ DONE | no auto Task DONE; `succeeded`/`failed` are execution-level terminal, no `reported` state (§11) |
| 9 | governance report separation | no FK, no auto-create (§15) |
| 10 | no new infrastructure | reuses PostgreSQL/OutboxWorker/grants; no Redis/Celery/Kafka/etc. |
| 11 | minimal migration | only `agent_executions` + grants (§18) |
| 12 | PostgreSQL 18 compatible | UUID/JSONB/CHECK/partial-index all PG-native; no exotic features |
| 13 | existing conventions reused | PK/UUID/FK/status/JSONB/UTC/idempotency/grants all mirrored (§2) |

---

*End of design. No migration, no code, no `conftest.py` change was made. Candidate schema is Proposed
and must be re-validated against the actual Phase-1 migration before implementation.*
