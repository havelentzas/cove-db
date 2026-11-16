# cove-db — context for the managing agent

cove-db is the user's personal "life databases" system: Postgres in Docker, exposed to agents through
one MCP server and to the user through a web UI. Each life area is a **domain** (a Postgres schema).
Live domains: **People** (personal CRM, `crm` schema) and **Notes** (the user's Obsidian vault, synced
two ways, `notes` schema; protocol in `docs/notes-sync.md`).

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
| postgres | `pgvector/pgvector:pg17`, volume `cove_pgdata` | 127.0.0.1:5432, db `cove` (+ `cove_test`) |
| migrate  | dbmate, runs `db/migrations/*.sql` then exits | — |
| web      | FastAPI + Jinja2 + HTMX (`cove.web.app`): home page `/` (all services + endpoints), People at `/people`, catalog JSON at `/api/services` | 127.0.0.1:8080, and on the tailnet via `tailscale serve` (hostname: see `CLAUDE.local.md`) |
| mcp      | MCP SDK 2.x `MCPServer`, streamable HTTP (`cove.mcp_server`) | http://127.0.0.1:8765/mcp, and on the tailnet at `https://<host>:8443/mcp` (host allow-list: `COVE_MCP_EXTRA_HOSTS`) |
| backup   | nightly `pg_dump -Fc` → `./backups`, 14-day retention | — |

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
`cove_owner` (migrations, superuser) · `cove_app` (web+mcp; RW `crm.*`, `meta.agent_notes`; INSERT-only
`meta.audit_log`) · `cove_readonly` (`sql_query` tool; read-only session, 5s timeout).
Secrets (DB passwords, token pepper, session key) are Docker secrets in `./secrets/*.txt` (git-ignored; dir 700),
read via `cove/secrets.py`; create them with `scripts/init-secrets.sh`. `.env` holds only non-secret settings.

## Common tasks
- Rebuild & restart: `docker compose up -d --build`
- Tests: `docker compose --profile test run --rm test` (uses `cove_test`, resets schemas per test)
- Schema change: add `db/migrations/NNN_name.sql`, insert a row into `meta.changelog` in the same
  migration, grant to roles, then `docker compose run --rm migrate`.
- **Adding a new domain**: new schema migration (+ grants + `meta.domains` row), a `<domain>/service.py`
  following the CRM pattern (actor + audit + validation), MCP tools in `mcp_server.py`, web routes, and —
  if records are document-shaped — a JSON Schema in `schemas/` and a form template in `templates/`.
  Give it a service key + colour pair in `brand/services.json` and follow `brand/BRAND.md` › Building a new service.
- Never commit `.env`, `backups/`, or `*.vcf` (contact data).
