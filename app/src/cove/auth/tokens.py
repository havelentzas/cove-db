"""API tokens (devices, and later personal/agent tokens).

- Tokens are random 256-bit values with a typed prefix (cove_dev_…, cove_pat_…) so secret
  scanners can recognise them; they are shown once and never stored.
- The database stores HMAC-SHA256(pepper, token). The pepper is a server secret, so a leaked
  database alone can't be used to test guesses offline.
- Tokens created before peppering (plain sha256) still verify once and are upgraded in place.
- Every token belongs to exactly one workspace (control.api_tokens.workspace_id); verifying a token
  tells you which, and what it may do there: its scopes ∩ its user's current role (cove.auth.policy).
  Removing someone from a workspace therefore disables their tokens at once.
- Kinds: `device` (cove_dev_, notes sync for the Obsidian plugin) and `personal` (cove_pat_, for agents
  and scripts: MCP, Claude Code). Agent tokens default to read-only plus the agent's own memory.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets

from cove import context, db, secrets
from cove.crm.service import Invalid, NotFound, _plain

PREFIXES = {"device": "cove_dev_", "personal": "cove_pat_"}
ANY_PREFIX = "cove_"


def _hmac(token: str) -> str:
    return hmac.new(secrets.get("token_pepper").encode(), token.encode(), hashlib.sha256).hexdigest()


def _legacy(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _event(c, kind: str, token_id, actor: str, **detail) -> None:
    from cove.control import identity
    p = context.current()
    identity.event(kind, user_id=p.user_id if p else None, conn=c, token_id=token_id, by=actor,
                   workspace=p.workspace.slug if p else None, **detail)


def create(name: str, actor: str, scopes: list[str] | None = None, kind: str = "device",
           expires_days: int | None = None) -> dict:
    """A new token for the current workspace and user. Returned once, never stored.

    A token can't be given a scope its creator doesn't have."""
    from cove.auth import policy
    p = context.require()
    name = (name or "").strip()
    if not name:
        raise Invalid("give it a name, e.g. 'laptop' or 'claude-code'")
    if len(name) > 64:
        raise Invalid("name is too long")
    if kind == "device":
        policy.require("notes:sync")
        wanted = policy.DEVICE_SCOPES
    else:
        wanted = policy.expand(scopes) if scopes else policy.DEFAULT_AGENT_SCOPES
        if not wanted:
            raise Invalid("pick at least one permission")
    if not wanted <= p.scopes:
        raise policy.Forbidden("a token can't have permissions you don't have: " + ", ".join(sorted(wanted - p.scopes)))
    if expires_days is not None and not 1 <= expires_days <= 3650:
        raise Invalid("expiry must be between 1 and 3650 days")
    token = PREFIXES[kind] + _secrets.token_urlsafe(32)
    with db.ctl_conn() as c:
        row = c.execute("INSERT INTO control.api_tokens (workspace_id, user_id, kind, name, token_hash, scopes, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, now() + %s * interval '1 day') "
                        "RETURNING id, name, kind, scopes, created_at, expires_at",
                        (p.workspace.id, p.user_id, kind, name, _hmac(token), sorted(wanted), expires_days)).fetchone()
        _event(c, "token_created", row["id"], actor, name=name, token_kind=kind)
    return {**_plain(row), "token": token}


def verify(token: str, kind: str | None = None) -> dict | None:
    """The token, its workspace and its effective scopes; or None. Needs no current workspace (it finds it).

    `kind` restricts which kind of token is accepted here (device tokens for sync, personal for MCP)."""
    from cove.auth import policy
    if not token or not token.startswith(ANY_PREFIX) or len(token) > 200:
        return None
    peppered = _hmac(token)
    q = ("UPDATE control.api_tokens t SET {set} last_used_at = now() FROM control.workspaces w "
         "WHERE w.id = t.workspace_id AND t.token_hash = %s AND t.revoked_at IS NULL "
         "AND (t.expires_at IS NULL OR t.expires_at > now()) AND (%s::text IS NULL OR t.kind = %s) "
         "AND w.status IN ('active', 'needs_attention') "
         "RETURNING t.id, t.name, t.kind, t.scopes, t.user_id, w.id AS ws_id, w.slug, w.name AS ws_name, "
         "w.db_name, w.role_name, CASE WHEN t.user_id IS NULL THEN 'owner' ELSE (SELECT m.role FROM "
         "control.memberships m JOIN control.users u ON u.id = m.user_id WHERE m.workspace_id = w.id "
         "AND m.user_id = t.user_id AND u.disabled_at IS NULL) END AS role")
    with db.ctl_conn() as c:
        row = c.execute(q.format(set=""), (peppered, kind, kind)).fetchone()
        if row is None:   # pre-pepper token: verify with the old hash once, then upgrade it
            row = c.execute(q.format(set="token_hash = %s,"), (peppered, _legacy(token), kind, kind)).fetchone()
    if not row or not row["role"]:   # user left the workspace or was disabled
        return None
    row = _plain(row)
    row["effective"] = policy.effective(row["role"], row["scopes"])
    row["workspace"] = context.Workspace(id=row["ws_id"], slug=row["slug"], name=row["ws_name"],
                                         db_name=row["db_name"], role_name=row["role_name"])
    return row


def list_all(kind: str | None = None) -> list[dict]:
    """Tokens in the current workspace: all of them for owners, otherwise your own."""
    p = context.require()
    with db.ctl_conn() as c:
        return _plain(c.execute(
            "SELECT t.id, t.name, t.kind, t.scopes, t.created_at, t.last_used_at, t.expires_at, t.revoked_at, "
            "u.username FROM control.api_tokens t LEFT JOIN control.users u ON u.id = t.user_id "
            "WHERE t.workspace_id = %s AND (%s::text IS NULL OR t.kind = %s) "
            "AND (%s OR t.user_id = %s::uuid) ORDER BY t.revoked_at NULLS FIRST, t.created_at DESC",
            (p.workspace.id, kind, kind, p.role == "owner", p.user_id)).fetchall())


def revoke(token_id: int, actor: str) -> None:
    p = context.require()
    with db.ctl_conn() as c:
        r = c.execute("UPDATE control.api_tokens SET revoked_at = coalesce(revoked_at, now()) "
                      "WHERE id = %s AND workspace_id = %s AND (%s OR user_id = %s::uuid) RETURNING name",
                      (token_id, p.workspace.id, p.role == "owner", p.user_id)).fetchone()
        if not r:
            raise NotFound(f"no token {token_id}")
        _event(c, "token_revoked", token_id, actor, name=r["name"])
