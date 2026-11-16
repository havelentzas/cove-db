#!/bin/sh
# Off-site copy of ./backups with restic. Everything it uploads is already age-encrypted; restic adds a
# second layer of encryption, deduplication and retention (30 daily + 12 monthly snapshots).
# This container has internet access but no database access and no age private key.
#
# Settings: RESTIC_REPOSITORY (from COVE_OFFSITE_REPOSITORY in .env), the repository password in
# secrets/restic_password.txt, and any provider credentials as KEY=value lines in secrets/offsite_env.txt
# (e.g. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY for B2's S3 API). OFFSITE_ONCE=1 runs one cycle.
set -eu
: "${RESTIC_REPOSITORY:?COVE_OFFSITE_REPOSITORY is not set in .env (see docs/backups.md)}"
export RESTIC_PASSWORD_FILE=/run/secrets/restic_password RESTIC_CACHE_DIR=/tmp/restic-cache
if [ -s /run/secrets/offsite_env ]; then set -a; . /run/secrets/offsite_env; set +a; fi

log() { echo "$(date -u +%FT%TZ) $*"; }

restic cat config >/dev/null 2>&1 || { log "initialising repository"; restic init; }

cycle() {
  restic backup --host cove --tag nightly --exclude '*.tmp' --exclude /backups/legacy-plaintext /backups &&
  restic forget --host cove --keep-daily 30 --keep-monthly 12 --prune &&
  # weekly: re-download and verify a random 10% of the data, so silent corruption is noticed
  if [ "$(date +%u)" = 7 ] || [ -n "${OFFSITE_ONCE:-}" ]; then restic check --read-data-subset=10%; fi
}

while true; do
  if cycle; then
    log "offsite ok"; echo "ok $(date -u +%FT%TZ)" > /tmp/offsite-status
  else
    log "offsite FAILED"; [ -n "${OFFSITE_ONCE:-}" ] && exit 1
  fi
  [ -n "${OFFSITE_ONCE:-}" ] && exit 0
  sleep 86400
done
