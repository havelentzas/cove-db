# The fabric: how cove services use each other without depending on each other

cove is a set of small services (People, Notes, Messages, and later Move, Money, Home, Reading…). Each one is
more useful when it can see the others: "what's going on with this person?" should bring back messages,
logged conversations and notes together. The **fabric** (`app/src/cove/fabric/`) is the small integration
layer that makes this possible while keeping every service **optional**. A service can be missing, turned
off, empty, slow or broken, and everything else still works and says what it couldn't include.

## Principles

1. **Each service owns its schema and its writes.** No foreign keys, joins, views or writes across service
   schemas. Cross-service links are **opaque references** stored as text (see [Refs](#refs)). A service may
   cache a display label next to a reference, but never relies on it. Dropping or disabling a service never
   breaks another service's migrations, queries or pages (`tests/test_fabric.py::test_no_cross_schema_foreign_keys`).
2. **Integrations are optional by construction.** A consumer asks for a capability and gets either a value
   (`Result`) or an explicit `Unavailable(reason)`, never an exception. Every consumer has a defined behaviour
   without the input: show less, never fail.
3. **Partial results say so.** Anything that combines services carries
   `coverage: {"included": [...], "missing": [{"service", "reason"}]}`, so agents and the UI can tell
   "nothing found" from "couldn't look".
4. **Permissions travel with the caller.** A capability runs as the current principal and checks that
   principal's scopes. A token with `messages:read` but not `people:read` gets messages with raw sender
   handles, plus `missing: [{service: people, reason: no_permission}]`. Never contact names by the back door.
5. **Derived data is disposable.** Anything a service computes from another service (handle → contact
   resolution, "last messaged") is a cache rebuilt from events or a backfill. Deleting it loses nothing the
   user typed (manual links are user data and are kept).
6. **Async over sync for side effects.** Services publish change events to an outbox. Consumers pull them
   with their own cursor. A producer never waits on a consumer; a consumer that lags or crashes only hurts itself.

## Refs

`cove:<service>/<kind>/<id>`, all lower-case, `<id>` is the record's own id (usually a UUID).

| ref | owner |
|---|---|
| `cove:people/contact/<uuid>` | People |
| `cove:people/interaction/<id>` | People |
| `cove:notes/file/<uuid>` | Notes |
| `cove:messages/conversation/<uuid>` | Messages |
| `cove:messages/message/<uuid>` | Messages |
| `cove:messages/handle/<uuid>` | Messages |

`fabric.ref(service, kind, id)` builds one and `fabric.parse_ref(text)` splits one (`None` if it isn't a ref).
Refs are stored as `text` columns with no constraint pointing anywhere else.

## Capabilities and contracts

A **capability** is a named, versioned function a service offers to the others. Its input and output are
JSON Schema contracts in `schemas/contracts/<name>.v1.json`. Within a version a contract only ever gains
optional fields; anything incompatible is a new version (`…v2`) served alongside the old one.

| capability | input | output | providers |
|---|---|---|---|
| `identity.resolve` | `{identifiers: [str]}`: E.164 phone, lower-case email, platform handle or display name | `[{identifier, ref, label, confidence, method}]` | People (contacts); Messages (its own handles, so a phone number still finds threads without People) |
| `identity.describe` | `{refs: [ref]}` | `[{ref, label, kind, url, summary, aliases?}]` (unknown refs are left out) | every service, for its own refs |
| `timeline.items` | `{subject_ref?, since?, until?, limit, cursor?}` | `{items: [{ref, service, kind, occurred_at, title, excerpt, participants: [ref], url}], next_cursor}` | People (interactions), Notes (notes edited, and notes mentioning the person), Messages (conversation activity, one item per conversation per day) |
| `search.query` | `{q, limit}` | `[{ref, service, title, excerpt, score, occurred_at}]` | every service |
| `activity.last_contact` | `{refs: [person ref]}` | `{ref: {last_inbound, last_outbound, channel}}` | Messages; People (logged conversations) |
| `activity.awaiting_reply` | `{refs?: [person ref], days, limit}` | `[{ref, title, subject_refs, last_inbound_at, url}]` | Messages |

Each capability has a **scope**: the provider's read scope (`people:read`, `notes:read`, `messages:read`).
The fabric checks it against the current principal before calling the provider.

### Using a capability

```python
from cove import fabric

r = fabric.ask("identity.resolve", provider="people", identifiers=["+31612345678"])
if r.ok:
    matches = r.value
else:
    matches = []          # r.reason is disabled | not_installed | no_permission | error | timeout

g = fabric.gather("timeline.items", subject_ref=ref, limit=50)
g.values      # {"people": {...}, "messages": {...}}  (only the providers that answered)
g.coverage    # {"included": ["people", "messages"], "missing": [{"service": "notes", "reason": "disabled"}]}
```

- `ask` never raises for a provider problem. It returns `Unavailable` when the provider's service is not
  installed or turned off in this workspace, when the principal lacks the scope, when the provider raises,
  or when it takes longer than its timeout (default 3 s, `COVE_FABRIC_TIMEOUT`).
- `gather` runs every provider of a capability in parallel, each with its own timeout, and merges the coverage.
- Calls are logged (`cove.fabric` logger: capability, service, outcome, milliseconds). Arguments and results
  are **never** logged: they can contain message text.
- A provider called from inside another provider runs inline (no extra thread), bounded by the outer timeout.

## Events

Each workspace database has an outbox, `meta.events (seq, service, type, ref, occurred_at, payload)`. A service
publishes inside the same transaction as the write that caused it (`fabric.publish(c, …)`), so an event exists
exactly when the change committed. Payloads are small and never contain message text.

| type | ref | payload |
|---|---|---|
| `people.contact.created` / `updated` / `archived` | `cove:people/contact/<uuid>` | `{}` |
| `messages.handle.created` | `cove:messages/handle/<uuid>` | `{platform}` |
| `notes.file.changed` | `cove:notes/file/<uuid>` | `{deleted}` |

Consumers pull with their own cursor (`meta.event_cursors (consumer, last_seq)`): `fabric.events(since, types)`
reads, and the **worker** (`python -m cove.fabric.worker`, its own container) runs every registered consumer for
every workspace every few seconds. A consumer that raises does not advance its cursor and is retried next round;
other consumers carry on. Events older than 30 days are pruned once every cursor is past them.

## Service enablement

`meta.services (key, enabled, installed_version, settings)`, one row per installed service per workspace,
inserted by the service's migration. Turning a service off (`/settings/services`, owners only):
- its MCP tools disappear from tool listings and refuse calls;
- its pages and `/api/v1/<service>/` routes answer "turned off" and its nav and home tile are hidden;
- its capabilities return `Unavailable(disabled)`;
- its data is kept, and turning it back on restores everything.

A service with no row (its migration never ran, or its code isn't present) is `not_installed`.

## HTTP API (`/api/v1/`)

Same shape for every service: `GET /api/v1/<service>/<things>` (list, with `?cursor=&limit=` and filters),
`GET …/<things>/{id}`, and `?since=` change feeds where it makes sense. Every object carries its `ref`. Errors use
one envelope: `{"error": {"code": "not_found", "message": "…"}}`. Auth is a personal access token
(`Authorization: Bearer cove_pat_…`) or a signed-in browser session (state-changing requests then need the
`X-CSRF-Token` header). The OpenAPI document is at `/api/v1/openapi.json`, grouped by service tag.

| endpoint | what |
|---|---|
| `GET /api/v1/fabric/capabilities` | what each service provides and consumes, and what's available to you right now |
| `GET /api/v1/fabric/context?ref=…` (or `?q=name`) | "everything about X": description, timeline, last contact, with coverage |
| `GET /api/v1/fabric/search?q=` | search every service, with coverage |
| `GET /api/v1/fabric/events?since=` | the event feed, limited to services you can read |

## Failure modes

| situation | what the caller sees |
|---|---|
| service turned off | `Unavailable(disabled)`; `coverage.missing` names it; its panel disappears |
| service not installed | `Unavailable(not_installed)` |
| caller lacks the scope | `Unavailable(no_permission)`; nothing from that service leaks |
| provider raises | `Unavailable(error)`, logged with the exception type only; the response is still a 200 |
| provider too slow | `Unavailable(timeout)` after `COVE_FABRIC_TIMEOUT`; its late result is discarded |
| consumer crashes | its cursor stays put; it retries next round; producers and other consumers are unaffected |

## Adding a capability

1. Write `schemas/contracts/<name>.v1.json` (`input` and `output` schemas, `description`).
2. Add the provider function to the service's `fabric.py` manifest under `provides`, with its scope.
3. Add a row to the table above, and a contract test (`tests/test_fabric.py` validates every provider's output
   against its contract).

## Adding a service that degrades gracefully

1. Its migration creates its own schema (no foreign keys out of it), grants, a `meta.domains` row, a
   `meta.services` row and a `meta.changelog` row.
2. `<service>/fabric.py` declares a manifest: `key`, `version`, `provides`, `consumes` (all optional) and
   `publishes`. Add the module to `fabric.registry.SERVICE_MODULES`.
3. It provides at least `identity.describe` and `search.query` for its own refs, and `timeline.items` if it has
   anything dated.
4. Everything it consumes goes through `fabric.ask` / `fabric.gather`, with a written-down behaviour for
   `Unavailable`. Add a test that turns the other service off and checks the page and tools still work.
5. Writes publish events with `fabric.publish` inside the write's transaction.
6. Its MCP tools carry `meta={"service": "<key>"}` and its routers `tags=["<key>"]`, so they are hidden when
   it's turned off and listed on the home page when it's on.
