#!/usr/bin/env bash
set -Eeuo pipefail

# The bootstrap role is intentionally never used by application processes.
required=(DX_MIGRATION_PASSWORD DX_API_PASSWORD DX_WORKER_PASSWORD)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 1
  fi
done

psql --set=ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set=dx_migration_password="${DX_MIGRATION_PASSWORD}" \
  --set=dx_api_password="${DX_API_PASSWORD}" \
  --set=dx_worker_password="${DX_WORKER_PASSWORD}" <<'SQL'
CREATE ROLE dx_migration LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  PASSWORD :'dx_migration_password';
CREATE ROLE dx_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  PASSWORD :'dx_api_password';
CREATE ROLE dx_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  PASSWORD :'dx_worker_password';
ALTER DATABASE dxbrain OWNER TO dx_migration;
REVOKE ALL ON DATABASE dxbrain FROM PUBLIC;
GRANT CONNECT ON DATABASE dxbrain TO dx_api, dx_worker;
SQL

psql --set=ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<'SQL'
CREATE DATABASE dxbrain_test OWNER dx_migration;
REVOKE ALL ON DATABASE dxbrain_test FROM PUBLIC;
GRANT CONNECT ON DATABASE dxbrain_test TO dx_api, dx_worker;
SQL
