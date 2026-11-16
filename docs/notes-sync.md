# Notes sync protocol (v1)

The server side of two-way sync between cove **Notes** and an Obsidian vault. The `cove-sync`
Obsidian plugin (next milestone) is the main client, but any client can use this API.

- **Base URL:** `https://<tailnet-host>/api/notes`, or `http://127.0.0.1:8080/api/notes` locally.
- **Auth:** `Authorization: Bearer cove_…`. Create a token per device at `/settings/devices`.
  Every write is audited as `device:<name>`.
- **Server code:** `app/src/cove/notes/service.py`, the only writer, also used by MCP and the web UI.
  The HTTP layer is `app/src/cove/web/notes_api.py`.

## Model
| Field | Meaning |
|---|---|
| `id` | UUID, **chosen by the client that creates the file**, so retried creates are idempotent. Stable across renames. |
| `path` | Vault-relative path, NFC, `/`-separated. Unique among live files (case-insensitive). `.obsidian/` is never synced. |
| `kind` | `note` (`.md`), `canvas` (`.canvas`, JSON Canvas) or `attachment` (anything else). |
| `content` / `blob_sha256` | Notes and canvases carry text. Attachments point to a content-addressed blob. |
| `content_hash` | sha256 of the content bytes (UTF-8 for text). |
| `version` | Starts at 1 and increases on every write, rename or delete. |
| `seq` | Global, strictly increasing change-feed position. |
| `deleted` | Tombstone. Deleted files stay in the feed so offline clients learn about them. |

The server never reformats a file. Title, frontmatter, tags, aliases and links are derived from it.

## Client state (the "ledger")
For each local path: `{id, baseVersion, baseHash}`, plus a global `lastSeq` and an outbox of pending operations.
A file is **locally modified** if its current hash ≠ `baseHash`.

## Endpoints
| | |
|---|---|
| `GET /whoami` | Checks the token. Returns `{server, api_version, device, head_seq}`. |
| `GET /manifest` | All live files: `{files: [{id, kind, path, content_hash, version}], head_seq}`. Used for first sync. |
| `GET /changes?since=SEQ&limit=500` | Files with `seq > since`, oldest first, tombstones included: `{changes, last_seq, head_seq, more}`. Repeat until `more` is false. |
| `GET /files/{id}` | Metadata, plus `content` for notes and canvases. |
| `PUT /files/{id}` | Body: `{path, content \| blob_sha256, base_version?}`. Creates or updates. |
| `POST /files/{id}/rename` | Body: `{path, base_version}`. |
| `DELETE /files/{id}?base_version=N` | Tombstone. |
| `GET /files/{id}/history` | Version list. |
| `HEAD /blobs/{sha256}` | 200 if already stored (skip upload), 404 if not. |
| `PUT /blobs/{sha256}` | Raw bytes. The server verifies the hash. Max 200 MB. |
| `GET /blobs/{sha256}` | Raw bytes. |
| `GET /search?q=&tag=&folder=` | Search. |

### Write results
Every write returns `{status, file, …}`:

| HTTP | `status` | Client action |
|---|---|---|
| 201 | `created` | Store `{id, version, hash}` in the ledger. |
| 200 | `updated` / `unchanged` / `restored` | Update the ledger from `file`. |
| 200 | `merged` | The server merged your edit with a newer one. **Write `content` to disk**, then update the ledger. |
| 409 | `conflict` | Overlapping edits. The server version is kept and yours is saved as `conflict_copy` (a new file). Pull both. |
| 409 | `path_taken` | Another live file has this path (`existing`). On first sync, adopt `existing.id` if the hashes match. |
| 404 / 422 | `not_found` / `invalid` | Show the error. Don't retry unchanged. |

### Conflict rules (server-side)
- **Text edit on a stale base:** three-way merge of `base_version` against the current version. Non-overlapping changes merge; overlapping ones create `Name (conflict YYYY-MM-DD device).md`.
- **Rename on a stale base:** if the server copy was also renamed, the server path wins (`conflict`).
- **Delete of a file edited since your base:** refused (`conflict`). An edit beats a delete.
- **Edit of a deleted file:** restores it (`restored`).
- **Attachment changed on both sides:** the second write becomes a conflict copy.

## Client algorithm (for the plugin)
1. **Startup:** `whoami`. On first run, do the first-sync procedure below. Otherwise scan local files, queue a push for any whose hash ≠ `baseHash`, and queue new and missing files.
2. **Events:** create, modify (debounced ~2 s), rename and delete each go to the outbox and are flushed in order. Uploads for new files use `crypto.randomUUID()` as the id.
3. **Pull:** on startup, on app focus and every ~30 s, call `GET /changes?since=lastSeq`. For each change:
   - **Local file unmodified:** apply it (download the content, rename, or move to the trash for tombstones).
   - **Local file modified:** push it first; the server merges.
   - In both cases, update the ledger, then set `lastSeq = last_seq`.
4. **Echo suppression:** when applying a remote change, set `baseHash` to the new hash **before** writing the file, so the resulting `modify` event is ignored.
5. **Attachments:** hash the bytes, `HEAD /blobs/{sha}`, `PUT` only if it's missing, then `PUT /files/{id}` with `blob_sha256`.
6. **Offline:** the outbox persists across restarts and is replayed when the server is reachable again. Client-chosen ids make replays safe.

### First sync
Fetch `/manifest`, then match local files by path:
- **Same hash:** adopt the server's id and version.
- **Different hash:** push with `base_version` = the server's version. Your copy wins, and the old one stays in history.
- **Only on the server:** download it.
- **Only local:** create it.

## Server behaviour worth knowing
- Links are resolved the way Obsidian does: exact path first, then file name (case-insensitive, `.md` optional, shortest path wins). Backlinks therefore work even for notes created after the link.
- Every write is kept in `notes.versions`, the source for merges and history. There's no pruning yet.
- Attachments live on the `notes_blobs` volume, under `/data/blobs/aa/bb/<sha256>`. The nightly backup copies new blobs to `./backups/blobs/`.
