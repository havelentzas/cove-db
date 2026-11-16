#!/bin/sh
# Create ./secrets/*.txt for a fresh install (existing files are never overwritten).
set -eu
cd "$(dirname "$0")/.."
mkdir -p secrets && chmod 700 secrets   # the directory guards access on the host
for name in postgres_password app_password readonly_password token_pepper session_key; do
  f="secrets/$name.txt"
  [ -s "$f" ] && { echo "keep    $f"; continue; }
  python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" > "$f"
  chmod 644 "$f"                        # readable inside the containers it's mounted into
  echo "created $f"
done
