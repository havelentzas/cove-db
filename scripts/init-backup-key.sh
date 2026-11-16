#!/bin/sh
# Create the age key pair for encrypted backups (docs/backups.md).
# The public key goes into .env (COVE_BACKUP_AGE_RECIPIENTS); the private key is written OUTSIDE the repo
# and must then be moved off this machine (password manager, hardware key, printed copy).
set -eu
cd "$(dirname "$0")/.."
out=${1:-$HOME/cove-backup-identity.txt}
grep -q '^COVE_BACKUP_AGE_RECIPIENTS=age1' .env 2>/dev/null && { echo "a backup key is already configured in .env"; exit 1; }
[ -e "$out" ] && { echo "$out already exists"; exit 1; }
umask 077
sudo docker run --rm --entrypoint age-keygen cove-backup:latest > "$out"
pub=$(sed -n 's/^# public key: //p' "$out")
if grep -q '^COVE_BACKUP_AGE_RECIPIENTS=' .env; then
  sed -i "s|^COVE_BACKUP_AGE_RECIPIENTS=.*|COVE_BACKUP_AGE_RECIPIENTS=$pub|" .env
else
  echo "COVE_BACKUP_AGE_RECIPIENTS=$pub" >> .env
fi
echo "public key → .env: $pub"
echo "PRIVATE key → $out"
echo "Move the private key off this machine now (password manager), then delete $out."
echo "Without it the backups cannot be restored."
