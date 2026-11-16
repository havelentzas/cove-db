#!/bin/sh
# Idempotent database setup, run by the `dbinit` service before every migration:
#   - login roles, with passwords from Docker secrets (rotate a secret file, re-run, done)
#   - the shared databases: cove_control (identity, sessions, workspaces, tokens, security events),
#     pocketid (the identity provider's own store), cove_test (tests). Workspace databases
#     (cove_ws_<id>) are created by `python -m cove.control.workspaces`.
#   - CONNECT is granted per database, so e.g. the pocketid role can't open cove at all
set -eu
PGPASSWORD="$(cat /run/secrets/postgres_password)"
export PGPASSWORD PGHOST="${PGHOST:-postgres}" PGUSER=cove_owner PGDATABASE=postgres
psql -X -q -v ON_ERROR_STOP=1 <<'SQL'
\set app_pw `cat /run/secrets/app_password`
\set ro_pw `cat /run/secrets/readonly_password`
\set pid_pw `cat /run/secrets/pocketid_db_password`

SELECT format('CREATE ROLE %I LOGIN', r) FROM unnest(ARRAY['cove_app', 'cove_readonly', 'pocketid']) AS r
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) \gexec
ALTER ROLE cove_app PASSWORD :'app_pw';
ALTER ROLE cove_readonly PASSWORD :'ro_pw';
ALTER ROLE pocketid PASSWORD :'pid_pw';
ALTER ROLE cove_readonly SET default_transaction_read_only = on;
ALTER ROLE cove_readonly SET statement_timeout = '5s';

SELECT format('CREATE DATABASE %I OWNER %I', d, o)
  FROM (VALUES ('cove_control', 'cove_owner'), ('pocketid', 'pocketid'),
               ('cove_test', 'cove_owner')) AS v(d, o)
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = d) \gexec

REVOKE CONNECT, TEMPORARY ON DATABASE cove_control, pocketid, cove_test, postgres FROM PUBLIC;
GRANT CONNECT ON DATABASE cove_control TO cove_app;
SQL
echo "dbinit: roles and databases ok"
