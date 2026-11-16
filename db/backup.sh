#!/bin/sh
# Nightly logical backup of the cove database with simple retention.
set -eu
while true; do
  ts=$(date +%Y%m%d-%H%M%S)
  if pg_dump -Fc -f "/backups/cove-$ts.dump"; then
    echo "backup ok: cove-$ts.dump"
  else
    echo "backup FAILED at $ts" >&2
  fi
  if [ -d /blobs ]; then mkdir -p /backups/blobs && cp -rn /blobs/. /backups/blobs/ && echo "blobs synced"; fi
  find /backups -name 'cove-*.dump' -mtime +"${BACKUP_RETENTION_DAYS:-14}" -delete
  sleep 86400
done
