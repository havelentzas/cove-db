# cove-db — context for the managing agent

cove-db is the user's personal "life databases" system: Postgres in Docker, exposed to agents through
one MCP server and to the user through a web UI. Each life area is a **domain** (a Postgres schema).
Live domains: **People** (personal CRM, `crm` schema), **Notes** (the user's Obsidian vault, synced
two ways, `notes` schema; protocol in `docs/notes-sync.md`) and **Messages** (iMessage/SMS and WhatsApp archive,
`messages` schema; `docs/messages.md`). Services use each other only through the **fabric** (`docs/fabric.md`):
capabilities that answer a value or `Unavailable(reason)`, never an exception, so any service can be missing,
turned off, unreadable or broken and the rest keeps working and says what's missing (`coverage`).

**Read `ETHOS.md` before adding or changing a service.** It is the guiding document (also at `/ethos`).

## Workspaces (one database each)
Each workspace is its own database `cove_ws_<id>` with roles `ws_<id>` / `ws_<id>_ro`. Every request runs as a
`cove.context.Principal` bound to one workspace (set by `web/auth.py` from the session or device token; MCP uses
its personal access token). `db.conn()` / `db.ro_conn()` connect to the current workspace and
`SET ROLE` into its role; with no principal they raise `NoWorkspace`. So service code never names a database:
it just calls `db.conn()`. Anything cached in-process must be keyed by workspace (see `notes._cached`).
Migrations in `db/migrations` grant to `cove_app`/`cove_readonly` as before; the migrator moves those grants onto
each workspace's roles. Provision with `docker compose run --rm migrate python -m cove.control.workspaces
create <slug> "<name>" --owner <username>` (also `list`, `add-member`, `adopt`). Control tables: `db/control`.

## Permissions (must stay complete)
`cove/auth/policy.py` has the scopes. Every **public function in a service module** (including each service's
`fabric.py` providers and `cove/fabric/*`) must be listed in the `policy.guard(...)` table at the bottom of that
module (with its scope, `None` for any member, or `PURE`), and every **MCP tool** must be in `TOOL_SCOPES` in
`mcp_server.py`. `tests/test_policy.py` fails otherwise (add new modules to its list).
Messages scopes (`messages:read|write|ingest`) are in no role's default for viewers and not in
`DEFAULT_AGENT_SCOPES`. Token kinds: `device` (`cove_dev_`, notes sync), `personal` (`cove_pat_`, MCP and
`/api/v1`) and `ingest` (`cove_ing_`, collectors: only `messages:ingest`, only on `/api/v1/messages/ingest/*`).
MCP and personal-token API callers are **agents** (`Principal.agent`): they never see conversations hidden from
agents or one-time codes.
MCP requests authenticate with a `cove_pat_` token (`/settings/agents`); the token's name is the agent's actor label.

## Live context lives in the database, not here
At the start of any session that touches data, call the MCP tools **`describe_system`** (domains, tables,
counts, recent schema changes) and **`read_notes`** (durable notes from earlier sessions about the user's
preferences). Save new durable learnings with `write_note`. This file covers *how the system is built*;
`meta.*` covers *what's in it*.

Environment-specific details (hostnames, device names) live in the git-ignored `CLAUDE.local.md`. This repo is
public: never commit hostnames, emails, tokens or personal data. See `SECURITY.md`.

## Services (`docker compose`, project name `cove`)
| service  | what | where |
|---|---|---|
| postgres | `pgvector/pgvector:pg17`, volume `cove_pgdata`. Databases: `cove_control`, `pocketid`, one `cove_ws_<id>` per workspace, `cove_test` | internal network only |
| migrate  | `python -m cove.control.migrate`: `db/control` → `cove_control`, then `db/migrations` → every workspace database (one failing is marked `needs_attention`, the rest go on), then exits. Also where privileged workspace commands run | — |
| dbinit   | `db/roles.sh`: login roles (passwords from secrets) and databases, idempotent; runs before every migrate | — |
| pocket-id | Pocket ID (passkey OIDC provider), state in its own `pocketid` database | 127.0.0.1:1411, tailnet :9443 |
| web      | FastAPI + Jinja2 + HTMX (`cove.web.app`): home page `/` (all services + endpoints), People at `/people`, Notes at `/notes`, Messages at `/messages`, JSON API under `/api/v1/` (OpenAPI at `/api/v1/openapi.json`), catalog JSON at `/api/services` | 127.0.0.1:8080, and on the tailnet via `tailscale serve` (hostname: see `CLAUDE.local.md`) |
| worker   | `python -m cove.fabric.worker`: runs each service's event consumers (e.g. re-matching message senders when a contact changes) for every workspace, every 10 s | — |
| mcp      | MCP SDK 2.x `MCPServer`, streamable HTTP (`cove.mcp_server`) | http://127.0.0.1:8765/mcp, and on the tailnet at `https://<host>:8443/mcp` (host allow-list: `COVE_MCP_EXTRA_HOSTS`) |
| backup   | nightly encrypted (age) dump + attachments → `./backups`, restore drill, 14-day retention (`docs/backups.md`) | — |
| offsite  | profile `offsite`: restic copy of `./backups` off-site (30 daily / 12 monthly) | — |

`web` and `mcp` are the same image (`app/Dockerfile`, build context = repo root).
The user isn't in the `docker` group in older shells; use `sudo docker compose …` if `docker` is denied.
Containers are hardened (caps dropped, no-new-privileges, read-only app FS, Postgres on an internal network);
keep it that way when adding services: start from the `x-hardened` / `x-app` anchors in docker-compose.yml.

## Brand (applies to every service, now and later)
`brand/` is the shared identity: `BRAND.md` (principles, colour, type, voice — read it before any UI
work), `cove.css` (tokens + components), `services.json` (one signal colour per service), `logo/`, `fonts/`.
Web UIs serve it at `/brand`, set `<html data-service="<key>">`, and add components to `cove.css` rather than
per-service CSS. Look: ink on paper, wide heavy Archivo headings, Martian Mono for data/labels, one bright
signal colour per service used as solid blocks, crisp borders and hard offset shadows. Avoid soft beige,
pastel washes and blurred shadows (the user rejected that look as "too AI"). Copy stays warm and plain.

## Code map
- `app/src/cove/crm/service.py` — **the only code that writes CRM data.** Web and MCP both call it, so
  validation (JSON Schema), optimistic locking (`version`) and audit logging (`meta.audit_log`, actor =
  `web[:<tailscale login>]` / `mcp:<client name>`) are identical. Add features here first, then expose them.
- `app/src/cove/notes/service.py` — the only writer of notes (versions, 3-way merge, conflict copies,
  tombstones, blobs, device tokens). `notes/markdown.py` parses and renders Obsidian Markdown (sanitized with nh3).
- `app/src/cove/web/notes_api.py` — token-authenticated sync API `/api/notes/*`; `web/notes_web.py` — `/notes`
  pages and `/settings/devices`.
- `app/src/cove/fabric/` — the integration layer: `core` (manifests, `ask`/`gather`, `Result`/`Unavailable`,
  coverage, refs `cove:<service>/<kind>/<id>`), `services` (`meta.services` enablement per workspace), `events`
  (`meta.events` outbox, cursors), `aggregate` (`context_for`, `search_everything`), `worker`. Contracts are
  JSON Schema in `schemas/contracts/*.v1.json`. Each service declares its manifest in `<service>/fabric.py`.
- `app/src/cove/messages/` — `service.py` (the only reader/curator of messages; agent rules), `ingest.py` (bulk
  ingest, `cove.messages.ingest/v1`), `normalize.py` (handles, OTP detection), `importers/whatsapp.py`.
  Web: `web/messages_api.py` (`/api/v1/messages/*`), `web/messages_web.py` (`/messages`), `web/panels.py` (the
  Messages panel on a contact page, fabric-only).
- `collectors/imessage/` — the Mac collector (stdlib-only Python, launchd, Keychain token); tested from
  `app/tests/test_imessage_collector.py` against a synthetic `chat.db`.
- `app/src/cove/web/api_v1.py` — `/api/v1/` conventions (error envelope, cursors, `service_on`), fabric
  endpoints, People and Notes JSON routes, `/api/v1/openapi.json`. `web/services_web.py` — `/settings/services`.
- `app/src/cove/crm/vcard.py` — JSContact ⇄ vCard (RFC 9555 subset). vCard import 3.0/4.0, export 4.0.
- `app/src/cove/templating.py` — generic path-based form templates (domain-agnostic).
- `app/src/cove/mcp_server.py` — thin async MCP adapter over the service layer.
- **Sign-in** (`docs/auth.md`): `web/auth.py` is a deny-by-default middleware (session + CSRF on every
  route except `PUBLIC_PATHS`/`PUBLIC_PREFIXES`), plus `/login`, `/auth/*`, `/account`, `/admin`.
  `auth/oidc.py` (code flow + PKCE against Pocket ID), `auth/sessions.py` (server-side sessions in
  `cove_control`), `control/identity.py` (users, identities, security events). Every POST form needs
  `{{ csrf_field() }}` (a test checks). Tests: the `browser` fixture / `conftest.Browser` is a signed-in client.
- `app/src/cove/web/` — routes (`app.py`), Jinja templates, vendored `htmx.min.js`.
- `schemas/jscontact-card.schema.json` — validation for cards. `templates/contact-default.json` — the
  contact form, driven entirely by this file.
- `db/migrations/` — dbmate SQL (`-- migrate:up` / `-- migrate:down`). `db/init/00-roles.sh` creates login
  roles on first volume init only.

## Service registration (keeps the home page accurate automatically)
- MCP tools: `@mcp.tool(..., meta={"service": "<key>"})` (`core` for system tools).
- Web routes: put them on `APIRouter(tags=["<key>"])` and `include_router` it (see `people` in `web/app.py`).
- `brand/services.json`: set the service's `status` to `live` and its `path`.
The home page (`web/hub.py`) builds its catalog from these, so nothing is hand-maintained.

## Data conventions (CRM)
- Contact data lives ONLY in `crm.contacts.card`, a JSContact Card (RFC 9553). Collections are id-keyed maps.
  App-specific card properties use the vendor prefix `cove.local:` (e.g. `cove.local:howWeMet`).
- Tags, relationships, interactions and follow-up cadence live in their own tables. Incoming `keywords` /
  `relatedTo` (to known contacts) are moved into those tables, and merged back on vCard export.
- `display_name`, `emails`, `phones`, `search` are derived by trigger — never write them directly.
- Contacts are archived, not deleted.
- Naive datetimes are interpreted in `COVE_TIMEZONE` (Europe/Amsterdam); storage is timestamptz.

## Roles
`cove_owner` (migrations and provisioning, superuser) · `cove_app` (web+mcp login role: no table privileges
in workspace databases, only `SET ROLE ws_<id>`; in `cove_control`: RW users/identities/sessions/memberships/
api_tokens, INSERT-only security_events) · `cove_readonly` (`sql_query` login role: only `SET ROLE ws_<id>_ro`;
read-only, 5s timeout) · `ws_<id>` (per workspace: RW crm/notes, agent notes; INSERT-only audit log) ·
`ws_<id>_ro` (per workspace: SELECT, no token hashes, **nothing** in `messages` or `meta.events`, so
`system:sql` can't get around `messages:read`) · `pocketid` (owns only the `pocketid` database).
CONNECT is granted per database (`db/roles.sh`, `cove.control.workspaces`).
Secrets (DB passwords, token pepper, session key) are Docker secrets in `./secrets/*.txt` (git-ignored; dir 700),
read via `cove/secrets.py`; create them with `scripts/init-secrets.sh`. `.env` holds only non-secret settings.

## Common tasks
- Rebuild & restart: `docker compose up -d --build`
- Tests: `docker compose --profile test run --rm test` (uses `cove_test`, resets schemas per test; mounts
  `collectors/` too). Most tests run as the database owner; grants are checked under the real workspace roles
  in `test_workspaces.py` (a fresh workspace and one upgraded from the previous migration), so add new schemas
  there. The test service runs `dbinit` against whatever Postgres the compose project has: to keep tests off a
  live stack, use a separate project (`-p cove-test`) with its own image tag.
- Identity schema change: `db/control/NNN_name.sql` (database `cove_control`, schema `control`).
- Schema change: add `db/migrations/NNN_name.sql`, insert a row into `meta.changelog` in the same
  migration, grant to `cove_app`/`cove_readonly` as usual, then `docker compose run --rm migrate` (applies it to
  every workspace and moves the grants onto the workspace roles).
- **Adding a new domain** (Move, Money, Home, Reading…): work through `ETHOS.md` › Checklist for a new service.
  Every service is optional to every other one (ETHOS principles 1–5; mechanics in `docs/fabric.md`):
  1. Migration: its own schema, **no foreign keys, views or joins into another service's schema**
     (`test_no_cross_schema_foreign_keys_or_views` scans `db/migrations`); links to other services are opaque ref
     text columns. Explicit grants to `cove_app` (default privileges aren't relied on), decide what
     `cove_readonly` may see, and insert `meta.domains`, `meta.services` and `meta.changelog` rows.
  2. `<domain>/service.py` following the CRM pattern (actor + audit + validation, `guard` table). Anything
     it needs from another service goes through `fabric.ask`/`fabric.gather`, with a written-down behaviour for
     `Unavailable` (show less, never fail). Writes publish events with `fabric.publish(c, …)` in the same
     transaction.
  3. `<domain>/fabric.py`: a manifest providing at least `identity.describe` and `search.query` (and
     `timeline.items` if it has dated things); add it to `fabric.core.SERVICE_MODULES`. Validate outputs against
     the contracts in a test.
  4. Surfaces: MCP tools with `meta={"service": "<key>"}`, routers with `tags=["<key>"]` and
     `dependencies=[service_on("<key>")]` (pages under `/<key>`, JSON under `/api/v1/<key>/` with refs and
     cursors), so turning it off hides all of it.
  5. Tests that turn the service off and that turn its dependencies off, and check pages, tools and
     `context_for` still answer with the right `coverage`. See `docs/fabric.md` › Adding a service.
  6. A service key + colour pair in `brand/services.json` (AA-checked) and `cove.css`; follow
     `brand/BRAND.md` › Building a new service. Document-shaped records get a JSON Schema in `schemas/`.
- Never commit `.env`, `backups/`, or `*.vcf` (contact data).
- Backups: `docs/backups.md`. The age private key is NOT on this machine; `backups/` only ever holds `*.age` files.
