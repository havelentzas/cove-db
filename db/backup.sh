#!/bin/sh
# Nightly encrypted backup of cove's databases, note attachments and secrets, with a restore drill.
#
#   For each database in BACKUP_DATABASES (cove_control, pocketid) and every workspace database:
#   1. pg_dump into tmpfs (memory) — plaintext never touches disk
#   2. restore drill: restore that dump into a scratch database and compare row counts with the live one
#   3. encrypt to the age recipients in COVE_BACKUP_AGE_RECIPIENTS → /backups/<db>-<ts>.dump.age
#   Then:
#   4. encrypt any new attachment blobs → /backups/blobs/aa/bb/<sha256>.age (content-addressed, so
#      existing files never change and off-site sync only uploads new ones)
#   5. encrypt ./secrets → /backups/secrets-<ts>.tar.age (DB passwords, token pepper, Pocket ID's key)
#   6. retention, and a one-line result in /backups/STATUS
#
# The private key is kept off this machine (docs/backups.md): this host can write backups but not read them.
# BACKUP_ONCE=1 runs one cycle and exits non-zero on failure (CI, manual runs).
set -eu
PGPASSWORD="$(cat /run/secrets/postgres_password)"
export PGPASSWORD
: "${BACKUP_AGE_RECIPIENTS:?COVE_BACKUP_AGE_RECIPIENTS is not set in .env; refusing to write unencrypted backups (see docs/backups.md)}"
RECIPIENTS=""
for r in $(echo "$BACKUP_AGE_RECIPIENTS" | tr ',' ' '); do RECIPIENTS="$RECIPIENTS -r $r"; done
DRILL_DB=cove_restore_drill
WORK=/tmp/work

log() { echo "$(date -u +%FT%TZ) $*"; }

# "schema.table rowcount" for every table outside the system schemas, sorted
counts() {
  psql -d "$1" -XAtq -F' ' -v ON_ERROR_STOP=1 -c "
    select n.nspname || '.' || c.relname,
           (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', n.nspname, c.relname), false, true, '')))[1]::text
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where c.relkind in ('r', 'p') and n.nspname not in ('pg_catalog', 'information_schema')
      and n.nspname not like 'pg_toast%' and n.nspname not like 'pg_temp%'
    order by 1"
}

drill() { # $1 = dump file; restores it into $DRILL_DB and checks it against the live counts
  dropdb --if-exists "$DRILL_DB" && createdb "$DRILL_DB" || return 1
  pg_restore --no-owner --no-acl --exit-on-error -d "$DRILL_DB" "$1" || return 1
  counts "$DRILL_DB" > "$WORK/restored" || return 1
  dropdb "$DRILL_DB"
  if cmp -s "$WORK/before" "$WORK/after"; then
    cmp -s "$WORK/before" "$WORK/restored" || { diff "$WORK/before" "$WORK/restored" >&2; return 1; }
    DRILL="exact ($(wc -l < "$WORK/restored") tables, $(awk '{s+=$2} END {print s+0}' "$WORK/restored") rows)"
  else
    # data changed while dumping, so exact counts can't be compared; require the same tables
    cut -d' ' -f1 "$WORK/before" > "$WORK/t1"; cut -d' ' -f1 "$WORK/restored" > "$WORK/t2"
    cmp -s "$WORK/t1" "$WORK/t2" || { diff "$WORK/t1" "$WORK/t2" >&2; return 1; }
    DRILL="tables only, data changed during the dump ($(wc -l < "$WORK/restored") tables)"
  fi
}

encrypt() { # $1 = source, $2 = destination; atomic so a crash never leaves a half-written file
  # shellcheck disable=SC2086
  age $RECIPIENTS -o "$2.tmp" "$1" && mv "$2.tmp" "$2"
}

backup_db() { # $1 = database
  rm -rf "$WORK" && mkdir -p "$WORK" && chmod 700 "$WORK"
  counts "$1" > "$WORK/before" || return 1
  pg_dump -Fc -d "$1" -f "$WORK/db.dump" || { log "dump FAILED: $1"; return 1; }
  counts "$1" > "$WORK/after" || return 1
  drill "$WORK/db.dump" || { log "restore drill FAILED: $1-$ts"; return 1; }
  encrypt "$WORK/db.dump" "/backups/$1-$ts.dump.age" || { log "encryption FAILED: $1"; return 1; }
  rm -rf "$WORK"
  log "backup ok: $1-$ts.dump.age; restore drill: $DRILL"
  DRILLS="$DRILLS $1=[$DRILL]"
}

cycle() {
  ts=$(date +%Y%m%d-%H%M%S)
  DRILLS=""
  workspaces=$(psql -d cove_control -XAtq -c "SELECT db_name FROM control.workspaces WHERE status <> 'archived' ORDER BY created_at" 2>/dev/null) || workspaces=""
  for d in ${BACKUP_DATABASES:-cove_control} $workspaces; do backup_db "$d" || return 1; done

  new=0
  if [ -d /blobs ]; then
    cd /blobs
    for f in $(find . -type f ! -name '*.tmp' | sed 's|^\./||'); do
      out="/backups/blobs/$f.age"
      [ -e "$out" ] && continue
      mkdir -p "$(dirname "$out")" && encrypt "$f" "$out" || { log "blob encryption FAILED: $f"; return 1; }
      new=$((new + 1))
    done
    cd /
    log "blobs: $new new"
  fi

  if [ -d /cove-secrets ]; then
    tar -C /cove-secrets -cf "/tmp/secrets.tar" . && encrypt /tmp/secrets.tar "/backups/secrets-$ts.tar.age"
    rc=$?; rm -f /tmp/secrets.tar; [ $rc = 0 ] || { log "secrets encryption FAILED"; return 1; }
  fi

  find /backups -maxdepth 1 \( -name '*-*.dump.age' -o -name 'secrets-*.tar.age' \) \
    -mtime +"${BACKUP_RETENTION_DAYS:-14}" -delete
  find /backups -name '*.age.tmp' -delete
  echo "ok $(date -u +%FT%TZ) $ts drills:$DRILLS blobs_new=$new" > /backups/STATUS
}

while true; do
  if ! cycle; then
    echo "FAILED $(date -u +%FT%TZ)" > /backups/STATUS
    rm -rf "$WORK"
    [ -n "${BACKUP_ONCE:-}" ] && exit 1
  fi
  [ -n "${BACKUP_ONCE:-}" ] && exit 0
  sleep 86400
done
