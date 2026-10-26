# DX Digital Brain OS — Kernel Tracer Bullet v0.1

DX Digital Brain OS is an early software prototype for human-governed AI-assisted workflows. It is intended for local development and evaluation. It has not received a production security review, operational acceptance, or independent deployment validation. It provides no production-environment security guarantee.

This candidate is licensed under the MIT License; see [LICENSE](LICENSE). The grant covers project-authored files in this candidate. Third-party packages remain under their own license terms.

The two historical reports formerly under `archive/` are omitted from this candidate while their quotation and redistribution rights remain unverified.

## Feature status

### Implemented in the repository

- FastAPI task APIs backed by PostgreSQL and Alembic migrations.
- Idempotency, task-state transitions, domain events, an outbox, and an internal worker.
- Governance proposals, review/approval records, execution reports, and audit records.
- Plan drafts, revisions, human review, and revision-pinned workflow handoffs.
- A sequential bookkeeping Workflow state machine. It does not execute external capabilities.
- Archive and retrospective record contracts.
- DeepSeek and Anthropic adapters with injected HTTP transport for offline tests.
- A read-only React Control UI.
- A bounded Codex CLI execution provider. The path is gated by the worker configuration and pre-dispatch checks, uses a resolved workspace, and starts Codex in read-only sandbox mode. The provider exists in code; full operational and production validation has not been performed.

### Gated or not fully verified

- Live model calls are disabled by default. One historical low-risk call was previously reported, but this repository does not include repeatable evidence for it. Treat live provider behavior as unverified.
- Real Codex execution is disabled by default. It requires explicit worker configuration, a valid approval/capability/workspace mapping, and a local Codex installation. Do not enable it against sensitive workspaces.
- The Codex provider currently accepts a native Windows `.exe`; Linux and macOS Codex execution are not supported by its current executable validator and have not been verified.
- Database migration and API startup instructions below are documented; this README does not claim a clean-machine install or production deployment has been validated.

### Not implemented or out of scope

- Autonomous multi-agent runtime, automatic scheduling, and automatic approval.
- External tool execution from the bookkeeping Workflow.
- Redis, vector databases, Qdrant, Ollama, and LangGraph integrations.
- Production identity management, multi-tenant authorization, high availability, or an operational support service.

## Requirements

- Python 3.12 or a supported version allowed by pyproject.toml.
- Docker Desktop or another Docker Compose installation with PostgreSQL 18.6 support.
- Node.js and npm compatible with the Vite version in the committed UI lockfile.

Python dependencies are specified as version ranges and are not pinned in a Python lockfile. The UI has package-lock.json; use npm ci for a lockfile-based UI install.

## Minimal local run

These steps create local development state. Never point them at a production or shared database.

1. Create .env only if it does not already exist, then edit it with local development values. Keep real values out of Git.

       if (!(Test-Path .env)) { Copy-Item .env.example .env }

2. Create a virtual environment and install the Python package and development tools.

       py -3.12 -m venv .venv
       .\.venv\Scripts\Activate.ps1
       python -m pip install --upgrade pip
       python -m pip install -e ".[dev]"

3. Start PostgreSQL. Compose binds the database to loopback by default.

       docker compose up -d postgres
       docker compose ps

4. Apply migrations with the migration-role DSN from your local .env. Replace the placeholder with the local value in your shell; do not commit it.

       $env:DX_DATABASE_URL = "<migration-role DSN>"
       python -m alembic upgrade head

5. Start the API with the API-role DSN and a local root API key.

       $env:DX_DATABASE_URL = "<API-role DSN>"
       $env:DX_ROOT_API_KEY = "<local development key>"
       python -m uvicorn dxbrain.api.app:app --reload --host 127.0.0.1

6. Create a task from a second PowerShell terminal.

       Invoke-RestMethod http://127.0.0.1:8000/v1/tasks -Method POST -Headers @{ 'X-DX-API-Key' = '<local development key>'; 'Idempotency-Key' = 'demo-001' } -ContentType 'application/json' -Body '{"title":"Trace the kernel"}'

The worker is a separate one-event command. It uses DX_WORKER_DATABASE_URL; the optional execution gateway uses the separate DX_EXECUTION_DATABASE_URL principal. Real Codex execution remains disabled unless explicitly configured and authorized.

## Control UI

The UI is read-only and communicates with the local API. From the repository root:

       cd apps/control-ui
       npm ci
       npm run dev -- --host 127.0.0.1

Open the local Vite address printed by the command and provide the local API address and key. The UI keeps the key in the current tab; it does not store it in local storage.

## Codex CLI environment

The worker passes Codex a small environment allowlist for OS startup, locale, certificates, and the Codex profile path. Ambient database URLs, DX_ROOT_API_KEY, and ambient model-provider keys are not forwarded. Codex account sign-in uses its own local profile, located through the preserved home/profile variables or CODEX_HOME.

If API-key authentication is needed, configure a dedicated DX_CODEX_API_KEY. Only this explicit setting is mapped to OPENAI_API_KEY for the Codex child. Do not reuse a database, DX root, DeepSeek, Anthropic, or unrelated OpenAI application key. Proxy environment variables are not inherited because they may carry credentials or internal routing information.

## Tests

### Offline tests

The following subset avoids the autouse database fixtures and makes no real provider calls:

       $env:DX_DATABASE_URL = "postgresql+psycopg://dx_api:offline-placeholder@127.0.0.1/dxbrain_test"
       $env:DX_ROOT_API_KEY = "offline-placeholder"
       python -m pytest --noconftest -p no:cacheprovider tests/test_execution_workspace.py tests/test_execution_codex_provider.py tests/test_provider_adapters.py tests/test_providers.py -k "not provider_status_endpoint"

The placeholder values satisfy module-level application configuration only. This command skips the database fixtures and provider-status endpoint, so it does not connect to PostgreSQL or call the Codex CLI.

### Database integration tests

The full suite is destructive to its configured test schema: tests downgrade and upgrade the schema and truncate test tables. Run it only against a newly created, disposable database named dxbrain_test, with all three DX_TEST_DATABASE_URL, DX_TEST_WORKER_DATABASE_URL, and DX_TEST_MIGRATION_DATABASE_URL values set to that database's appropriate roles. Never point these variables at a production, shared, or existing business database. The fixture rejects a database whose name is not dxbrain_test; that check does not prove the database contains no valuable data.

### UI type check

From apps/control-ui after npm ci:

       npx tsc --noEmit -p tsconfig.app.json --pretty false

## Security and contributions

See [SECURITY.md](SECURITY.md) for the current vulnerability-reporting boundary and [CONTRIBUTING.md](CONTRIBUTING.md) for local checks and contribution guidance. No response-time commitment or production support is provided.

## Architecture notes

See ARCHITECTURE.md, DATABASE_DESIGN.md, MODULE_DESIGN.md, and ROADMAP.md. The archived reports are excluded from this candidate; earlier Git history still contains them, and no history rewrite has been performed.
