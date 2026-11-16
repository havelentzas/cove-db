# Security

cove-db is a personal, self-hosted system for private data: contacts, notes and agent memory. This file
describes how it is secured and how to report problems.

## Reporting a vulnerability
Please **don't open a public issue.** Use GitHub's private vulnerability reporting
(**Security → Report a vulnerability** on this repo). You'll get a reply within a week.

## What this repository contains
- Code, schemas, design and documentation only. **No secrets and no personal data:** `.env`, `secrets/`,
  `backups/`, `*.vcf`, `*.dump` and `CLAUDE.local.md` are git-ignored.
- Hostnames and other environment details belong in `.env` or `CLAUDE.local.md`, never in committed files.
  (An earlier commit contained a Tailscale tailnet hostname. It's reachable only with tailnet membership,
  so the git history was left as is.)

## Security model (current)
- **Network:** the web UI and MCP server listen on localhost only and are published to a private Tailscale
  tailnet. There are no inbound ports on the host.
- **Sync API:** `/api/notes/*` requires per-device bearer tokens. They are shown once, stored hashed, and
  revocable at `/settings/devices`.
- **Data integrity:**
  - every write goes through one service layer, with validation and an append-only audit log
  - notes are versioned, and conflicting edits are kept as copies
  - deletes are tombstones
- **Content safety:** rendered notes are sanitised with nh3. Attachments are served with a sandboxing CSP,
  and active types (HTML, SVG) are served as downloads.
- **Database:** separate roles for owner (migrations), app (read/write) and read-only (ad-hoc SQL with a
  5-second timeout).

## In progress
A security overhaul is underway:
- passkey sign-in through a self-hosted OIDC provider
- a separate database per workspace
- scoped tokens and OAuth 2.1 for MCP
- container hardening, pinned dependencies, CI security scanning, and encrypted off-site backups

Until that's complete, cove must **not** be exposed to the public internet.
