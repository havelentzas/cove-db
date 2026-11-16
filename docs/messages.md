# Messages

An archive of your iMessage/SMS and WhatsApp history (later Signal, Telegram…), so you and your agents can
see what's going on with someone. Service key `messages`, schema `messages`. It's the first service built on
the fabric (`docs/fabric.md`): People is optional for it, and it is optional for People.

## Model (schema `messages`)

| table | what |
|---|---|
| `sources` | One per collector or importer: `key` (chosen by the collector), `platform`, `label`, `collector`, `high_water` (the collector's resume point), `last_ingest_at` |
| `handles` | An address someone writes from: `(platform, value)` unique. `value` is normalised: E.164 for phones, lower-case email, `name:<display name>` for WhatsApp exports. `contact_ref` (a People ref, nullable) with `resolution` `auto \| manual \| suggested \| none`; `is_me` marks your own handles |
| `conversations` | `source_id`, `external_id`, `platform`, `kind` (`direct \| group`), `title`, `last_message_at`, `message_count`, `agent_visible`, `archived`. Participants in `conversation_handles` |
| `messages` | `external_id` unique per source, `sender_handle_id` (null when from you), `is_from_me`, `reply_to_id`, `sent_at`, `edited_at`, `retracted`, `body`, `kind` (`text \| attachment \| system \| call \| sticker`), `reactions` (aggregated tapbacks), `platform_meta`, `seq` (change feed), `search` (`'simple'` tsvector), `is_sensitive` |
| `attachments` | Metadata only: `filename`, `mime`, `size`, `sha256`, `stored` (bytes are not stored yet) |

The read-only SQL role gets **no** access to this schema, so `system:sql` can't get around `messages:read`.

## Scopes

| scope | allows |
|---|---|
| `messages:read` | read messages, conversations and handles |
| `messages:write` | curate: link handles to contacts, hide conversations from agents, archive |
| `messages:ingest` | write-only bulk ingest for collectors; **cannot read anything back** |

None of them are in the default agent scopes. Viewers don't get `messages:read`.

Collectors use an **ingest token** (`cove_ing_…`, its own token kind, created under Messages › Settings). It
only works on `/api/v1/messages/ingest/*`, only carries `messages:ingest`, and is never accepted by MCP or the
notes sync API.

## Ingest protocol (v1)

Contract: `schemas/contracts/messages-ingest.v1.json`.

- `POST /api/v1/messages/ingest/batch`, `Authorization: Bearer cove_ing_…`. Body: `{format, source, handles,
  conversations, messages, reactions, edits, retractions, high_water?}`. Up to 1,000 messages per batch (8 MB).
- **Idempotent** on `(source, external_id)`: re-sending a batch is a no-op; a changed body/edit/retraction
  updates in place.
- Returns `{accepted, updated, duplicates, rejected: [{external_id, reason}]}`. A batch with any invalid
  envelope is a 422 with the error envelope; individual bad messages are listed in `rejected`.
- `high_water` is stored only after the batch commits. `GET /api/v1/messages/ingest/state?source=<key>`
  returns it so a collector can resume.
- After a batch: new handles are resolved through `fabric.ask("identity.resolve")`. If People is unavailable
  they stay unresolved and are retried when `people.contact.*` events arrive (the worker).
- One audit row per batch (counts only). Logs and errors never include message text.

## Agent-facing rules

- Every message body in MCP tool results is wrapped as `{"untrusted_text": …}`.
- Conversations with `agent_visible = false` and messages flagged `is_sensitive` (one-time codes) are left out
  of every agent path: MCP tools, `context_for`, `search_everything`, fabric capabilities, and `/api/v1` when
  called with a personal token. The web UI (you, signed in) shows them, marked.
- Sender names come from People through the fabric, so they follow the caller's `people:read`.

## Collectors

- **iMessage/SMS on a Mac:** `collectors/imessage/` (one stdlib-only Python file, launchd every 10 minutes,
  token in the Keychain). See `collectors/imessage/README.md`.
- **WhatsApp exports:** upload the `.txt` or `.zip` from "Export chat" at Messages › Settings. Senders are
  display names, so they become `name:` handles; matches to People are suggestions you confirm.

No unofficial WhatsApp clients or bridges (terms of service, account-ban risk).

## Failure modes

| situation | behaviour |
|---|---|
| People off or no `people:read` | senders show as raw handles; `coverage.missing` names People; ingest still works and handles stay unresolved |
| Messages off | People's contact page, follow-ups and `context_for` work exactly as before, with Messages listed as missing |
| collector re-sends | duplicates counted, nothing changes |
| reaction for a message not yet ingested | rejected with `target_not_found`; the collector's re-scan window sends it again later |
