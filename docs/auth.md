# Sign-in

The cove web UI requires sign-in with a **passkey**. Passkeys are managed by
[Pocket ID](https://pocket-id.org), a small self-hosted OpenID Connect provider that runs as the
`pocket-id` service. There are no passwords anywhere.

```
browser ──► cove /login ──► Pocket ID /authorize (passkey) ──► cove /auth/callback ──► session
                                      ▲ token exchange + signing keys go server-to-server (pocket-id:1411)
```

## What protects what
- **Deny by default.** A single middleware (`cove/web/auth.py`) requires a session for every path except
  these: `/healthz`, the sign-in flow (`/login`, `/auth/start`, `/auth/callback`), static assets, and the
  notes sync API (`/api/notes/*`), which checks its own per-device bearer token. A test calls every route
  without a session and expects to be refused, so a new route can't be left open by accident.
- **Sessions** are stored server-side in `cove_control.sessions`.
  - The cookie is `__Host-cove_session`: Secure, HttpOnly, SameSite=Lax, and it holds a random 256-bit
    token. Only an HMAC of the token is stored.
  - A session ends after 7 days without use or 30 days after sign-in. Every sign-in creates a new session.
  - Signing out ends the session on the server, so a copied cookie stops working.
- **CSRF:** every form carries a per-session token (`{{ csrf_field() }}`), checked on every POST. The
  older same-origin check also stays in place.
- **OIDC:**
  - Sign-in uses the authorization code flow with PKCE (S256), `state` and `nonce`. A login in progress is
    kept in a signed 10-minute cookie.
  - The ID token's signature, issuer, audience, expiry and nonce are all verified (`cove/auth/oidc.py`).
  - Redirects after sign-in only go to paths on this site.
- **Who can sign in:** Pocket ID is invite-only (sign-ups are disabled), passkeys must use user
  verification (biometrics or PIN), and the `cove` client only admits members of Pocket ID's `cove` group.
- **The first person to sign in becomes cove's admin.** The `/admin` page lists users and lets an admin
  disable an account (which signs it out everywhere), grant admin rights or end sessions. It also shows
  every security event.
- **Security events** go to `cove_control.security_events`, which the app can only append to. They cover
  user created, login, failed login, CSRF rejected, session revoked, user disabled, and admin granted or revoked.
- **Rate limits:** sign-in routes allow 30 requests per minute per IP.

## Permissions
- **Workspace roles:** owner and editor can read and write; viewer can only read.
- **Scopes** (`cove/auth/policy.py`): `people:read`, `people:write`, `notes:read`, `notes:write`, `notes:sync`,
  `system:memory`, `system:sql`. What a request may do is its role's scopes, narrowed by its token's scopes.
  A token can never do more than its user's role, and if that user is demoted or removed, the token loses
  access immediately.
- **One check, at the service layer:** every public function in `crm/service.py` and `notes/service.py` is
  wrapped by `policy.guard`, so the web UI, the sync API and MCP get the same check. A test fails if a
  function isn't covered.

## Agents (MCP)
- Every MCP request needs `Authorization: Bearer cove_pat_…`, a personal access token created at
  **/settings/agents**. The token chooses the workspace, labels the agent's changes in history
  (`agent (name)`), and decides which tools it sees: `tools/list` only shows permitted tools, and calls to
  others are refused.
- New agent tokens are read-only plus the agent's own memory (`system:memory`). Write scopes and `system:sql`
  have to be ticked explicitly. Tokens can expire.
- **Prompt-injection hygiene:**
  - the server tells agents that stored content is data, not instructions
  - agents have no delete tools
  - multi-contact calls are capped at 100 contacts
  - SQL is read-only and off by default
- **Before anyone can sign in** (or for automation), an admin can create a token from the shell:
  `docker compose run --rm migrate python -m cove.control.workspaces token <workspace> <name> [--scopes a,b]`.
- Claude Code: `claude mcp add --scope user --transport http cove <url> --header "Authorization: Bearer <token>"`.
  In this repo, `.mcp.json` reads the token from `$COVE_MCP_TOKEN`.

Coming in Phase 5: OAuth 2.1 for remote connectors (claude.ai), with sign-in and consent instead of
pasted tokens.

## Setup
1. In `.env`, set where your browser reaches each service:
   ```
   COVE_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
   COVE_AUTH_URL=https://<machine>.<tailnet>.ts.net:9443
   ```
   Publish Pocket ID on the tailnet with `tailscale serve --bg --https=9443 http://127.0.0.1:1411`.
2. `docker compose up -d`, then `scripts/setup-auth.py`. This creates the `cove` OIDC client and the `cove`
   group, and writes the client secret. Then run `docker compose up -d --force-recreate web`.
3. Create your account and open the one-time link it prints, then register a passkey. Register a
   second passkey on another device too.
   ```
   scripts/setup-auth.py add-user <username> <email> --admin
   ```
4. Open cove and sign in. The first account to sign in becomes cove's admin.

**Lost every passkey?** Run `scripts/setup-auth.py login-link <username>`. It prints a one-time link,
valid for 15 minutes, that signs you in so you can register a new passkey. It needs shell access to this
machine (the Pocket ID API key is in `secrets/`).

**Inviting someone:** run `scripts/setup-auth.py add-user <username> <email>` and send them the link.
You can also use Pocket ID's admin UI, but add them to the `cove` group there.

## Notes
- Passkeys are bound to the hostname in `COVE_AUTH_URL`. If that changes (for example when cove moves to a
  public domain in Phase 6), everyone registers their passkeys again.
- On the tailnet, cove and Pocket ID share a hostname on different ports, and browsers share cookies
  across ports. All cookies are HttpOnly, and both services are yours, so this is acceptable for now. The
  public setup puts Pocket ID on its own subdomain.
- Pocket ID keeps everything, including uploaded logos, in its own Postgres database (`pocketid`). The
  nightly backup covers it together with `cove` and `cove_control`.
