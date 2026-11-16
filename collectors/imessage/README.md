# cove iMessage/SMS collector (macOS)

Sends your Messages history (iMessage, SMS, RCS) from your Mac to cove's Messages service, every 10
minutes. One Python file, standard library only, using the `python3` that comes with macOS.

- It **never changes** `chat.db`: each run copies `chat.db` (and its `-wal`/`-shm`) into a private temp
  folder and reads the copy, so Messages.app is never locked or slowed.
- It resumes where it left off (cove stores the high-water mark) and re-reads the last 7 days on every run to
  pick up edits, unsends and tapbacks.
- It only ever prints counts. Message text is never printed or logged.
- The token is a write-only **ingest token** (`cove_ing_…`): it can send messages to cove but can't read
  anything back. It lives in your login **Keychain**, never in a file.

## Install

1. In cove, open **Messages › Settings › Collectors** and create an ingest token (copy it).
2. Copy this folder to the Mac (for example with `git clone`, or AirDrop the three files
   `cove_imessage.py`, `install.sh` and `com.cove.imessage-collector.plist`).
3. In Terminal, in this folder:

   ```sh
   ./install.sh https://<your cove host>
   ```

   It installs the script to `~/.local/share/cove-collector/`, writes `~/.config/cove-collector/config.toml`,
   asks you to paste the token into the Keychain, and starts the launchd job `com.cove.imessage-collector`.

### Full Disk Access

macOS protects `~/Library/Messages`, so the Python that runs the collector needs **Full Disk Access**:

1. System Settings › Privacy & Security › **Full Disk Access**.
2. Click **+**, press **⌘⇧G**, and add `/usr/bin/python3`. Do the same for the real interpreter that
   `install.sh` printed (usually `/Library/Developer/CommandLineTools/usr/bin/python3`, or one inside Xcode).
   Switch both on.
3. Check: `/usr/bin/python3 ~/.local/share/cove-collector/cove_imessage.py --check`

`--check` prints one line each for reading `chat.db` and reaching cove with the Keychain token.

## First backfill

```sh
/usr/bin/python3 ~/.local/share/cove-collector/cove_imessage.py --backfill
```

It sends everything in batches of 500 and shows progress. You can stop it and run it again: batches are
idempotent, and it resumes. Without `--backfill`, the first scheduled run only sends the last 30 days.

`--dry-run` reads and counts without sending anything.

## Config (`~/.config/cove-collector/config.toml`)

| key | default | |
|---|---|---|
| `url` | (required) | your cove's `https://` address |
| `source_key` | `imessage-mac` | how cove tells this Mac's messages apart; keep it stable |
| `label` | `iMessage on my Mac` | shown in Messages › Settings |
| `rescan_days` | `7` | days re-read each run (edits, unsends, tapbacks) |
| `initial_days` | `30` | how far back the first non-backfill run goes |
| `batch_size` | `500` | messages per request (max 1,000) |

## What it reads

`message`, `handle`, `chat`, `chat_message_join`, `chat_handle_join`, `attachment`,
`message_attachment_join`. It handles:

- dates in Apple time (seconds since 2001-01-01 on older macOS, nanoseconds on newer);
- message text kept only in `attributedBody` (macOS 13+), decoded from the NSAttributedString typedstream;
- tapbacks (`associated_message_type` 2000–2006 add, 3000–3006 remove; target `p:N/GUID` or `bp:GUID`),
  sent as reactions on the target message;
- edits (`date_edited`) and unsends (`date_retracted`, whose text is dropped);
- iMessage, SMS and RCS (`service`), your own messages (`is_from_me`), group names (`chat.display_name`);
- attachments as metadata only: file name, type and size (never the local path or the file).

## Logs, updates, removal

- Log: `~/Library/Logs/cove-collector.log`.
- Update: pull the new version and run `./install.sh` again.
- Remove: `./install.sh --uninstall` (your config and Keychain token stay until you delete them; it prints how).

## Development

`fixture.py` builds a synthetic `chat.db` (modern and legacy layouts); `app/tests/test_imessage_collector.py`
runs the collector against it end to end through the real ingest API.
