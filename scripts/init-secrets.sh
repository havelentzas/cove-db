#!/bin/sh
# Create ./secrets/*.txt for a fresh install (existing files are never overwritten).
set -eu
cd "$(dirname "$0")/.."
mkdir -p secrets && chmod 700 secrets   # the directory guards access on the host
new() { [ -s "secrets/$1.txt" ] && { echo "keep    secrets/$1.txt"; return 1; }; return 0; }
put() { printf '%s' "$2" > "secrets/$1.txt"; chmod 644 "secrets/$1.txt"; echo "created secrets/$1.txt"; }
rand() { python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')"; }

for name in postgres_password app_password readonly_password token_pepper session_key restic_password \
            pocketid_db_password pocketid_api_key; do
  new "$name" && put "$name" "$(rand)"
done
new pocketid_encryption_key && put pocketid_encryption_key "$(python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode(), end='')")"
# Pocket ID reads its whole connection string from a file; keep it in step with the role password
put pocketid_db_url "postgres://pocketid:$(cat secrets/pocketid_db_password.txt)@postgres:5432/pocketid?sslmode=disable" >/dev/null
# written by scripts/setup-auth.sh once Pocket ID is running; empty means sign-in is not configured (fail closed)
[ -e secrets/oidc_client_secret.txt ] || put oidc_client_secret ""
# off-site provider credentials (KEY=value lines); empty until you configure off-site backups
[ -e secrets/offsite_env.txt ] || put offsite_env ""
