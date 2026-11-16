# Backups

Backups are encrypted with [age](https://age-encryption.org) to a key that is **not on this machine**.
The server can write backups but can't read them, so a stolen disk or a compromised off-site bucket
gives away nothing.

## What happens every night
The `backup` service (`db/backup.sh`) has no internet access. Each night it:
1. Dumps the `cove` database (`pg_dump -Fc`) into memory (tmpfs). The plaintext never touches disk.
2. **Runs a restore drill:** it restores that dump into a scratch database (`cove_restore_drill`) and compares
   the row count of every table with the live database. If data changed while the dump ran, only the
   table list is compared. If the drill fails, no backup is written and `backups/STATUS` says `FAILED`.
3. Encrypts the dump to `backups/cove-<timestamp>.dump.age`, and keeps 14 days of dumps.
4. Encrypts any new note attachments to `backups/blobs/aa/bb/<sha256>.age`. Attachments are
   content-addressed, so existing files never change and off-site sync only uploads new ones.

`backups/STATUS` holds one line describing the last run. You can also check `docker compose logs backup`.

The `offsite` service (`db/offsite.sh`) has internet access but no database access. It copies `backups/` to
a [restic](https://restic.net) repository, keeps 30 daily and 12 monthly snapshots, and every Sunday
re-downloads 10% of the data to check that it's intact.

## Setup
1. Create the key pair: `scripts/init-backup-key.sh`. It writes the public key to `.env`
   (`COVE_BACKUP_AGE_RECIPIENTS`) and the private key to `~/cove-backup-identity.txt`.
2. **Move the private key off this machine** (into a password manager, ideally with a printed copy too),
   then delete the local file. Without the private key, no backup can be restored.
   You can add more recipients, such as a second key on a YubiKey, as a comma-separated list.
3. For off-site copies, set these:
   - `COVE_OFFSITE_REPOSITORY` in `.env` (any restic repository). For example, use
     `s3:https://s3.<region>.backblazeb2.com/<bucket>/cove` for Backblaze B2, or `rest:http://<host>:8000/cove`
     for a [rest-server](https://github.com/restic/rest-server) on another machine on the tailnet.
   - provider credentials as `KEY=value` lines in `secrets/offsite_env.txt` (for B2:
     `AWS_ACCESS_KEY_ID=…` and `AWS_SECRET_ACCESS_KEY=…` from an application key limited to that bucket)
   - `COMPOSE_PROFILES=offsite` in `.env`, then `docker compose up -d`

   The restic repository password is in `secrets/restic_password.txt`. Keep a copy of it next to the age
   key, because you need it to download from the repository.

## Restoring
All restores need the private key, `ID=/path/to/cove-backup-identity.txt`.

```sh
scripts/restore.sh db "$ID"                     # newest dump → new database cove_restored
scripts/restore.sh db "$ID" backups/cove-20261116-020000.dump.age cove_restored
scripts/restore.sh blobs "$ID" /tmp/blobs       # every attachment → /tmp/blobs/aa/bb/<sha256>
```
`restore.sh` never writes into the live `cove` database. To replace it:
```sh
docker compose stop web mcp backup
docker compose exec postgres psql -U cove_owner -d postgres \
  -c 'ALTER DATABASE cove RENAME TO cove_old' -c 'ALTER DATABASE cove_restored RENAME TO cove'
docker compose run --rm migrate          # re-applies grants and any newer migrations
docker compose up -d
```
Attachments go back into the `notes_blobs` volume (for example with `docker compose cp`, or with
`docker run -v cove_notes_blobs:/data/blobs …`).

From off-site, after losing this machine: install restic, run `restic -r <repository> restore latest --target ./restore`
(with the repository password and provider credentials), and then use the steps above on the restored `backups/` folder.

A full drill that includes decryption is worth doing every few months, because the nightly drill can't
decrypt: it checks the dump before encryption. CI runs the full encrypt → decrypt → restore cycle on every push.
