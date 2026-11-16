#!/bin/sh
# Nightly encrypted backup of the cove database and note attachments, with a restore drill.
#
#   1. pg_dump into tmpfs (memory) — plaintext never touches disk
#   2. restore drill: restore that dump into a scratch database and compare row counts with the live one
#   3. encrypt to the age recipients in COVE_BACKUP_AGE_RECIPIENTS → /backups/cove-<ts>.dump.age
#   4. encrypt any new attachment blobs → /backups/blobs/aa/bb/<sha256>.age (content-addressed, so
#      existing files never change and off-site sync only uploads new ones)
#   5. retention, and a one-line result in /backups/STATUS
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
    DRILL="exact ($(wc -l < "$WORK/restored") tables, $(awk '{s+=$2} END {print s}' "$WORK/restored") rows)"
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

cycle() {
  ts=$(date +%Y%m%d-%H%M%S)
  rm -rf "$WORK" && mkdir -p "$WORK" && chmod 700 "$WORK"
  counts cove > "$WORK/before" || return 1
  pg_dump -Fc -f "$WORK/cove.dump" || { log "dump FAILED"; return 1; }
  counts cove > "$WORK/after" || return 1
  drill "$WORK/cove.dump" || { log "restore drill FAILED for cove-$ts"; return 1; }
  encrypt "$WORK/cove.dump" "/backups/cove-$ts.dump.age" || { log "encryption FAILED"; return 1; }
  rm -rf "$WORK"
  log "backup ok: cove-$ts.dump.age; restore drill: $DRILL"

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

  find /backups -maxdepth 1 -name 'cove-*.dump.age' -mtime +"${BACKUP_RETENTION_DAYS:-14}" -delete
  find /backups -name '*.age.tmp' -delete
  echo "ok $(date -u +%FT%TZ) cove-$ts.dump.age drill=$DRILL blobs_new=$new" > /backups/STATUS
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
