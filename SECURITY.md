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
- **Network:** the web UI, MCP server and sign-in service listen on localhost only and are published to a
  private Tailscale tailnet. There are no inbound ports on the host.
- **Sign-in:** passkeys only, through a self-hosted Pocket ID (invite-only, user verification required).
  The web UI is deny-by-default: server-side sessions (`__Host-` cookie, HMAC-stored tokens, 7-day idle and
  30-day absolute limits), CSRF tokens on every form, and an admin page with a security event log.
  See `docs/auth.md`. The MCP server isn't behind sign-in yet (scoped tokens are next).
- **Sync API:** `/api/notes/*` requires per-device bearer tokens. They are shown once, stored hashed, and
  revocable at `/settings/devices`.
- **Data integrity:**
  - every write goes through one service layer, with validation and an append-only audit log
  - notes are versioned, and conflicting edits are kept as copies
  - deletes are tombstones
- **Content safety:** rendered notes are sanitised with nh3. Attachments are served with a sandboxing CSP,
  and active types (HTML, SVG) are served as downloads.
- **Backups:** nightly, encrypted with age to a key kept off the server, checked by a restore drill
  every night, and copied off-site with restic. See `docs/backups.md`.
- **Supply chain:** locked dependencies, images pinned by digest, and CI that runs pip-audit, npm audit,
  gitleaks and Trivy (fixable HIGH/CRITICAL issues fail the build).
- **Database:** separate roles for owner (migrations), app (read/write) and read-only (ad-hoc SQL with a
  5-second timeout).

## In progress
A security overhaul is underway:
- a separate database per workspace
- scoped tokens and OAuth 2.1 for MCP

Until that's complete, cove must **not** be exposed to the public internet.
