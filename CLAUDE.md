# cove-db — context for the managing agent

cove-db is the user's personal "life databases" system: Postgres in Docker, exposed to agents through
one MCP server and to the user through a web UI. Each life area is a **domain** (a Postgres schema).
Live domains: **People** (personal CRM, `crm` schema) and **Notes** (the user's Obsidian vault, synced
two ways, `notes` schema; protocol in `docs/notes-sync.md`).

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
`cove/auth/policy.py` has the scopes. Every **public function in a service module** must be listed in the
`policy.guard(...)` table at the bottom of that module (with its scope, `None` for any member, or `PURE`), and
every **MCP tool** must be in `TOOL_SCOPES` in `mcp_server.py`. `tests/test_policy.py` fails otherwise. MCP
requests authenticate with a `cove_pat_` token (`/settings/agents`); the token's name is the agent's actor label.

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
| web      | FastAPI + Jinja2 + HTMX (`cove.web.app`): home page `/` (all services + endpoints), People at `/people`, catalog JSON at `/api/services` | 127.0.0.1:8080, and on the tailnet via `tailscale serve` (hostname: see `CLAUDE.local.md`) |
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
`ws_<id>_ro` (per workspace: SELECT, no token hashes) · `pocketid` (owns only the `pocketid` database).
CONNECT is granted per database (`db/roles.sh`, `cove.control.workspaces`).
Secrets (DB passwords, token pepper, session key) are Docker secrets in `./secrets/*.txt` (git-ignored; dir 700),
read via `cove/secrets.py`; create them with `scripts/init-secrets.sh`. `.env` holds only non-secret settings.

## Common tasks
- Rebuild & restart: `docker compose up -d --build`
- Tests: `docker compose --profile test run --rm test` (uses `cove_test`, resets schemas per test)
- Identity schema change: `db/control/NNN_name.sql` (database `cove_control`, schema `control`).
- Schema change: add `db/migrations/NNN_name.sql`, insert a row into `meta.changelog` in the same
  migration, grant to `cove_app`/`cove_readonly` as usual, then `docker compose run --rm migrate` (applies it to
  every workspace and moves the grants onto the workspace roles).
- **Adding a new domain**: new schema migration (+ grants + `meta.domains` row), a `<domain>/service.py`
  following the CRM pattern (actor + audit + validation), MCP tools in `mcp_server.py`, web routes, and —
  if records are document-shaped — a JSON Schema in `schemas/` and a form template in `templates/`.
  Give it a service key + colour pair in `brand/services.json` and follow `brand/BRAND.md` › Building a new service.
- Never commit `.env`, `backups/`, or `*.vcf` (contact data).
- Backups: `docs/backups.md`. The age private key is NOT on this machine; `backups/` only ever holds `*.age` files.
