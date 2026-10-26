# Contributing

DX Digital Brain OS is an early prototype. Contributions are welcome as proposals, but review and response times are not guaranteed.

## Before opening a change

- Read README.md, ARCHITECTURE.md, the relevant ADR, and current tests for the area you plan to change.
- Keep changes narrow and describe the behavior and evidence they add.
- Do not include API keys, database credentials, personal data, local absolute paths, production logs, or private research material.
- Do not claim production readiness, external adoption, or live-service validation without reproducible evidence.
- Check that a proposed feature stays within the documented project boundary. Autonomous scheduling, broad agent runtime, and external tool integrations require a separate design decision.

## Local checks

Install the Python package and development dependencies as described in README.md. For a quick offline check, run:

    $env:DX_DATABASE_URL = "postgresql+psycopg://dx_api:offline-placeholder@127.0.0.1/dxbrain_test"
    $env:DX_ROOT_API_KEY = "offline-placeholder"
    python -m pytest --noconftest -p no:cacheprovider tests/test_execution_workspace.py tests/test_execution_codex_provider.py tests/test_provider_adapters.py tests/test_providers.py -k "not provider_status_endpoint"

These placeholder values satisfy module-level configuration only. The command skips database fixtures and the provider-status endpoint, so it does not connect to PostgreSQL or call the Codex CLI.

The Control UI type check is:

    cd apps/control-ui
    npm ci
    npx tsc --noEmit -p tsconfig.app.json --pretty false

The full Python suite runs destructive schema and table fixtures. Use only a newly created, disposable database named dxbrain_test; see README.md. Never run it against production, shared, or valuable existing data.

## Pull requests

Include a concise problem statement, the implementation boundary, tests run, and any unverified behavior. Keep unrelated formatting or refactoring out of the change. A pull request does not imply acceptance or a promised review schedule.
