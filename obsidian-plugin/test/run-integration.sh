#!/bin/sh
# Starts a throwaway cove server on the cove_test database (schemas reset, fresh tokens, temp blob dir),
# runs the plugin's integration tests against it with Node, then stops it.
# Usage (from the repo root):  sh obsidian-plugin/test/run-integration.sh
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
POSTGRES_PASSWORD=$(cat ./secrets/postgres_password.txt)
DOCKER=${DOCKER:-docker}
OUT=$(mktemp -d)
chmod 777 "$OUT"
NAME=cove-plugin-test
$DOCKER rm -f $NAME >/dev/null 2>&1 || true
$DOCKER compose run -d --no-deps --name $NAME -p 127.0.0.1:18080:8080 \
  -e COVE_DATABASE_URL="postgresql://cove_owner:${POSTGRES_PASSWORD}@postgres:5432/cove_test" \
  -e COVE_READONLY_URL="postgresql://cove_owner:${POSTGRES_PASSWORD}@postgres:5432/cove_test" \
  -e COVE_BLOB_DIR=/tmp/blobs -v "$OUT":/out \
  web sh -c '
python - <<PY
import json, os, re, glob, psycopg
url = os.environ["COVE_DATABASE_URL"]
with psycopg.connect(url, autocommit=True) as c:
    for s in ("crm", "meta", "notes"):
        c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    for f in sorted(glob.glob("/srv/db/migrations/*.sql")):
        sql = open(f).read().split("-- migrate:up", 1)[1]
        c.execute(re.split(r"^-- migrate:down", sql, flags=re.M)[0])
from cove.notes import service as n
json.dump({d: n.create_token(d, "test")["token"] for d in ("laptop", "phone")}, open("/out/tokens.json", "w"))
PY
exec uvicorn cove.web.app:app --host 0.0.0.0 --port 8080' >/dev/null
trap '$DOCKER rm -f $NAME >/dev/null 2>&1; rm -rf "$OUT"' EXIT
i=0; until [ -s "$OUT/tokens.json" ] && curl -sf http://127.0.0.1:18080/healthz >/dev/null; do
  i=$((i+1)); [ $i -gt 60 ] && { $DOCKER logs $NAME | tail -20; exit 1; }; sleep 1; done
$DOCKER run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e COVE_URL=http://127.0.0.1:18080 \
  -e COVE_TOKENS=/out/tokens.json -v "$OUT":/out -v "$ROOT/obsidian-plugin":/work -w /work node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a \
  npx tsx --test test/sync.test.ts
