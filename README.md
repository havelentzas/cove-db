# cove-db

Personal "life databases" in Docker: Postgres, an MCP server so AI agents can read and update them,
and a small web UI. The first domain is a **personal CRM**: contacts, interactions, follow-up reminders,
tags and groups, and relationships.

Contacts are stored as **JSContact** cards ([RFC 9553](https://www.rfc-editor.org/rfc/rfc9553)), the
IETF's JSON successor to vCard. vCard import and export follow [RFC 9555](https://www.rfc-editor.org/rfc/rfc9555),
so Google Contacts, iOS and other address books still work with it.

```
postgres (pgvector/pg17) ── migrate (dbmate)
   ├── web  :8080  FastAPI + HTMX ──┐
   ├── mcp  :8765  MCP (HTTP)  ─────┤── both use cove.crm.service (validation, versioning, audit log)
   └── backup     nightly pg_dump → ./backups
```

## Setup
```sh
cp .env.example .env          # then set strong passwords
docker compose up -d --build
docker compose --profile test run --rm test
```
- Web UI: http://127.0.0.1:8080. To reach it from other devices on your tailnet, run
  `tailscale serve --bg --https=443 http://127.0.0.1:8080`.
- MCP: `http://127.0.0.1:8765/mcp`. To reach it over your tailnet, set `COVE_MCP_EXTRA_HOSTS=<machine>.<tailnet>.ts.net` in `.env`
  and run `tailscale serve --bg --https=8443 http://127.0.0.1:8765`. `.mcp.json` registers it for Claude Code in this folder. To use it
  from any folder, run `claude mcp add --scope user --transport http cove http://127.0.0.1:8765/mcp`.

## Reusable pieces
- `schemas/jscontact-card.schema.json`: JSON Schema for the JSContact subset used here. Vendor
  extensions (`example.com:prop`) are allowed.
- `templates/*.json` + `app/src/cove/templating.py`: form templates. Each field is a path into a JSON
  record (`name/components[kind=given]/value`, `emails{*}/address`, …). The web form is generated from the
  template, so a new field means editing JSON, not HTML. Other domains can use the same engine with their
  own schema and template.

See `CLAUDE.md` for the architecture and conventions.
