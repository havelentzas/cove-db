# cove-db — context for the managing agent

cove-db is the user's personal "life databases" system: Postgres in Docker, exposed to agents through
one MCP server and to the user through a web UI. Each life area is a **domain** (a Postgres schema).
The first domain is a personal CRM.

## Live context lives in the database, not here
At the start of any session that touches data, call the MCP tools **`describe_system`** (domains, tables,
counts, recent schema changes) and **`read_notes`** (durable notes from earlier sessions about the user's
preferences). Save new durable learnings with `write_note`. This file covers *how the system is built*;
`meta.*` covers *what's in it*.

## Services (`docker compose`, project name `cove`)
| service  | what | where |
|---|---|---|
| postgres | `pgvector/pgvector:pg17`, volume `cove_pgdata` | 127.0.0.1:5432, db `cove` (+ `cove_test`) |
| migrate  | dbmate, runs `db/migrations/*.sql` then exits | — |
| web      | FastAPI + Jinja2 + HTMX (`cove.web.app`) | 127.0.0.1:8080, and on the tailnet via `tailscale serve` at https://cachyos-x8664.tail4fdffd.ts.net |
| mcp      | MCP SDK 2.x `MCPServer`, streamable HTTP (`cove.mcp_server`) | http://127.0.0.1:8765/mcp, and on the tailnet at https://cachyos-x8664.tail4fdffd.ts.net:8443/mcp (host allow-list: `COVE_MCP_EXTRA_HOSTS`) |
| backup   | nightly `pg_dump -Fc` → `./backups`, 14-day retention | — |

`web` and `mcp` are the same image (`app/Dockerfile`, build context = repo root).
The user isn't in the `docker` group in older shells; use `sudo docker compose …` if `docker` is denied.

## Code map
- `app/src/cove/crm/service.py` — **the only code that writes CRM data.** Web and MCP both call it, so
  validation (JSON Schema), optimistic locking (`version`) and audit logging (`meta.audit_log`, actor =
  `web[:<tailscale login>]` / `mcp:<client name>`) are identical. Add features here first, then expose them.
- `app/src/cove/crm/vcard.py` — JSContact ⇄ vCard (RFC 9555 subset). vCard import 3.0/4.0, export 4.0.
- `app/src/cove/templating.py` — generic path-based form templates (domain-agnostic).
- `app/src/cove/mcp_server.py` — thin async MCP adapter over the service layer.
- `app/src/cove/web/` — routes (`app.py`), Jinja templates, vendored `htmx.min.js`.
- `schemas/jscontact-card.schema.json` — validation for cards. `templates/contact-default.json` — the
  contact form, driven entirely by this file.
- `db/migrations/` — dbmate SQL (`-- migrate:up` / `-- migrate:down`). `db/init/00-roles.sh` creates login
  roles on first volume init only.

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
`meta.audit_log`) · `cove_readonly` (`sql_query` tool; read-only session, 5s timeout). Passwords in `.env`.

## Common tasks
- Rebuild & restart: `docker compose up -d --build`
- Tests: `docker compose --profile test run --rm test` (uses `cove_test`, resets schemas per test)
- Schema change: add `db/migrations/NNN_name.sql`, insert a row into `meta.changelog` in the same
  migration, grant to roles, then `docker compose run --rm migrate`.
- **Adding a new domain**: new schema migration (+ grants + `meta.domains` row), a `<domain>/service.py`
  following the CRM pattern (actor + audit + validation), MCP tools in `mcp_server.py`, web routes, and —
  if records are document-shaped — a JSON Schema in `schemas/` and a form template in `templates/`.
- Never commit `.env`, `backups/`, or `*.vcf` (contact data).
