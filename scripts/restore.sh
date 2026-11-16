#!/bin/sh
# Restore an encrypted cove backup. Needs the age private key (kept off this machine; see docs/backups.md).
#
#   scripts/restore.sh db    <identity-file> [backups/<db>-….dump.age] [target-db]
#       decrypt a dump (default: the newest workspace dump) into a NEW database (default: cove_restored).
#       It never touches a live database; swapping one in is a deliberate manual step (docs/backups.md).
#   scripts/restore.sh blobs <identity-file> <output-dir>
#       decrypt every attachment in backups/blobs/ into <output-dir>/aa/bb/<sha256>
#   scripts/restore.sh secrets <identity-file> <output-dir>
#       decrypt the newest secrets-….tar.age into <output-dir> (copy into ./secrets on a rebuilt machine)
# Dumps: cove_ws_<id>-… (one per workspace), cove_control-…, pocketid-…; restore each into its own new database.
set -eu
cd "$(dirname "$0")/.."
mode=${1:-}; key=${2:-}
[ -r "$key" ] || { sed -n '2,12p' "$0"; exit 2; }
key=$(realpath "$key")
DC="sudo docker compose"

case $mode in
  db)
    dump=${3:-$(ls -1t backups/cove_ws_*.dump.age | head -1)}
    target=${4:-cove_restored}
    case $target in cove|cove_control|pocketid|cove_ws_*) echo "refusing to restore over the live $target database" >&2; exit 2 ;; esac
    echo "restoring $dump → database $target"
    $DC run --rm -T -v "$key:/key:ro" -e TARGET="$target" -e DUMP="/backups/$(basename "$dump")" \
      --entrypoint sh backup -c '
        set -eu; export PGPASSWORD="$(cat /run/secrets/postgres_password)"
        createdb "$TARGET"
        age -d -i /key "$DUMP" | pg_restore --no-owner --no-acl --exit-on-error -d "$TARGET"
        echo "done. Grant access with: GRANT … TO cove_app, or rename it into place (docs/backups.md)"'
    ;;
  blobs)
    out=$(realpath -m "${3:?output dir}"); mkdir -p "$out"
    $DC run --rm -T -v "$key:/key:ro" -v "$out:/out" --entrypoint sh backup -c '
      set -eu; cd /backups/blobs; n=0
      for f in $(find . -type f -name "*.age" | sed "s|^\./||"); do
        mkdir -p "/out/$(dirname "$f")"; age -d -i /key -o "/out/${f%.age}" "$f"; n=$((n + 1))
      done; echo "decrypted $n blobs into the output dir"'
    ;;
  secrets)
    out=$(realpath -m "${3:?output dir}"); mkdir -p "$out" && chmod 700 "$out"
    src=$(ls -1 backups/secrets-*.tar.age | tail -1)
    $DC run --rm -T --no-deps -v "$key:/key:ro" -v "$out:/out" -e SRC="/backups/$(basename "$src")" --entrypoint sh backup -c '
      set -eu; age -d -i /key "$SRC" | tar -C /out -xf -; echo "restored secrets from $SRC"'
    ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
