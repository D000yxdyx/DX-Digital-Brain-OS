# DX Digital Brain OS — Kernel Tracer Bullet v0.1

This repository is the deliberately small, verifiable kernel of DX Digital Brain OS. It proves auditable task, governance, memory-record, model-call, plan-review, and contract-only Workflow paths before autonomous Agents, external tools, Redis, vectors, or scheduling are introduced.

## Scope

Included: PostgreSQL 18, Alembic, FastAPI task creation/query, atomic state/event/outbox/audit writes, a lease-based idempotent internal-event worker, the deterministic Governance Contract Foundation, the task-level Archive/Retrospective Contract Foundation, structured plan review/versioning, a contract-only sequential Workflow state machine, and transport-injected offline adapters for DeepSeek/Anthropic request and response contracts. Excluded: Redis, pgvector, Qdrant, Ollama, LangGraph, Agent runtime, dashboard write controls, external tools, autonomous scheduling, and outbound integrations.

## Prerequisites

- Python 3.12 (Windows launcher command: `py -3.12`)
- Docker Desktop with the WSL 2 backend running

## Local startup

1. Copy `.env.example` to `.env` and replace every password/key with local development secrets.
2. Create a virtual environment and install dependencies:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -e ".[dev]"
   ```

3. Start PostgreSQL and wait for its health check:

   ```powershell
   docker compose up -d postgres
   docker compose ps
   ```

4. Apply schema using the migration role, then run FastAPI with the API role:

   ```powershell
   $env:DX_DATABASE_URL = (Get-Content .env | Select-String '^DX_DATABASE_URL_MIGRATION=').ToString().Split('=',2)[1]
   alembic upgrade head
   $env:DX_DATABASE_URL = (Get-Content .env | Select-String '^DX_DATABASE_URL_API=').ToString().Split('=',2)[1]
   uvicorn dxbrain.api.app:app --reload
   ```

5. Send `X-DX-API-Key` equal to `DX_ROOT_API_KEY` and an `Idempotency-Key`:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8000/v1/tasks -Method POST -Headers @{ 'X-DX-API-Key' = '<key>'; 'Idempotency-Key' = 'demo-001' } -ContentType 'application/json' -Body '{"title":"Trace the kernel"}'
   ```

To consume one internal event, open a separate terminal and run:

```powershell
$env:DX_WORKER_DATABASE_URL = (Get-Content .env | Select-String '^DX_DATABASE_URL_WORKER=').ToString().Split('=',2)[1]
dxbrain-worker
```

## Verification

Run the migration first, then execute integration tests against the local database:

```powershell
$env:DX_ROOT_API_KEY = (Get-Content .env | Select-String '^DX_ROOT_API_KEY=').ToString().Split('=',2)[1]
$env:DX_TEST_DATABASE_URL = (Get-Content .env | Select-String '^DX_TEST_DATABASE_URL=').ToString().Split('=',2)[1]
$env:DX_TEST_WORKER_DATABASE_URL = (Get-Content .env | Select-String '^DX_TEST_WORKER_DATABASE_URL=').ToString().Split('=',2)[1]
$env:DX_TEST_MIGRATION_DATABASE_URL = (Get-Content .env | Select-String '^DX_TEST_MIGRATION_DATABASE_URL=').ToString().Split('=',2)[1]
pytest
```

Tests refuse to run against `dxbrain`; they recreate schema only in the disposable `dxbrain_test` database.

For persistence, create a task, run `docker compose restart postgres`, then query the same task. The named volume is mounted at `/var/lib/postgresql`, the PostgreSQL 18-compatible mount point.

## Role boundary

`dx_bootstrap` initializes roles only. `dx_migration` owns schema migrations. FastAPI uses `dx_api`; the worker uses `dx_worker`. Neither runtime role is a superuser, and the API role has no `UPDATE`, `DELETE`, or `TRUNCATE` privilege on the audit table. See [ARCHITECTURE.md](ARCHITECTURE.md) and [DATABASE_DESIGN.md](DATABASE_DESIGN.md).

The responsibility-level contract is recorded in [MODULE_DESIGN.md](MODULE_DESIGN.md).

The accepted future governance and frontend boundary is recorded in [ADR-0001](ADR-0001-governance-and-control-ui.md). The Three-Province/Six-Ministry reference evaluation and non-adoption boundary are recorded in [ADR-0002](ADR-0002-three-province-reference-evaluation.md). The proposed organizational model is recorded in [ADR-0003](ADR-0003-organizational-architecture.md), and the proposed archive/retrospective boundary in [ADR-0004](ADR-0004-records-retrospective-memory.md).

## Read-only Control UI

The first Control UI slice lives in `apps/control-ui`. It reads the authenticated FastAPI projections at `/v1/control/overview`, `/v1/control/tasks`, `/v1/tasks/{task_id}`, and the proposal/review/approval/execution-report routes; it never connects to PostgreSQL and has no mutating controls.

```powershell
cd apps/control-ui
npm install
npm run dev
```

Open `http://127.0.0.1:5173`, enter the local API address and `DX_ROOT_API_KEY`, then select a task to view its state timeline, governance chain, project dossier, retrospective, and validated lessons. The UI remains read-only; archive and retrospective routes are not exposed as mutating controls. The key is held only in the current tab. Configure additional development origins with `DX_CONTROL_UI_ORIGINS` if needed.

On Windows, `DX Digital Brain OS Control UI.lnk` on the Desktop starts both local services and opens the console. The launcher reads credentials from the local `.env` file in memory; it does not embed the API key in the shortcut. Service output is written to `runtime/logs/`.

## Optional model providers

The control UI API address is still the local DX API (`http://127.0.0.1:8000`); provider keys are server-side settings and are not entered into the UI key field. To configure providers, copy the optional `DEEPSEEK_*` and `ANTHROPIC_*` values from `.env.example` into the local `.env`. The read-only endpoint `/v1/control/model-providers` reports only provider readiness, base URL, transport, and default model; it never returns a key.

The initial policy defaults to DeepSeek for planning and Claude for review. The dry-run planner endpoint (`POST /v1/proposals/{proposal_id}/dry-run-plan`) records a model-call audit row but returns the proposal plan without inference or network access. The read-only preflight endpoint (`GET /v1/proposals/{proposal_id}/model-readiness`) explains whether configuration, provider readiness, an active approval, and the `model.call` capability are all present. The live ledger endpoints prepare, execute one explicit HTTP attempt, and finalize a call; they remain blocked while `DX_LLM_LIVE_ENABLED=false`. One low-risk DeepSeek planner call has been validated under this gate; broader outbound use still requires explicit policy, timeout, retry, usage, and audit controls.
The structured plan checkpoint (`POST /v1/model-calls/{model_call_id}/parse-plan`) turns succeeded planner text into a bounded `candidate` draft. A human must explicitly confirm or reject it through `POST /v1/plan-drafts/{draft_id}/review`; this does not start Workflow or rewrite the proposal.
Human edits are versioned through `POST /v1/plan-drafts/{draft_id}/edit` and listed through `/revisions`. Once confirmed, `POST /v1/plan-drafts/{draft_id}/workflow-handoff` records a revision-pinned `requested` handoff only; no scheduler or executor is enabled yet.
The bookkeeping Workflow MVP accepts that handoff with `POST /v1/workflow-handoffs/{handoff_id}/accept`, then advances one step boundary at a time through `POST /v1/workflow-runs/{run_id}/advance`. Pause and cancel are explicit human commands; no external capability is executed.
The Agent contract MVP registers a task-scoped Agent through `POST /v1/tasks/{task_id}/agents`, controls lifecycle through `/v1/agents/{agent_id}/lifecycle`, and exposes a read-only declared-capability check. Registration does not start a model runtime or grant external side effects.
