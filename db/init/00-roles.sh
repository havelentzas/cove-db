#!/bin/bash
# Runs once, on first initialisation of the Postgres volume.
# Creates the login roles used by the app and a separate test database.
set -euo pipefail
APP_PW="${COVE_APP_PASSWORD:-$(cat /run/secrets/app_password)}"
RO_PW="${COVE_READONLY_PASSWORD:-$(cat /run/secrets/readonly_password)}"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v app_pw="$APP_PW" -v ro_pw="$RO_PW" <<'SQL'
CREATE ROLE cove_app LOGIN PASSWORD :'app_pw';
CREATE ROLE cove_readonly LOGIN PASSWORD :'ro_pw';
ALTER ROLE cove_readonly SET default_transaction_read_only = on;
ALTER ROLE cove_readonly SET statement_timeout = '5s';
CREATE DATABASE cove_test OWNER cove_owner;
SQL
