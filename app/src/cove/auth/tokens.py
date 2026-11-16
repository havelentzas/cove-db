"""API tokens (devices, and later personal/agent tokens).

- Tokens are random 256-bit values with a typed prefix (cove_dev_…, cove_pat_…) so secret
  scanners can recognise them; they are shown once and never stored.
- The database stores HMAC-SHA256(pepper, token). The pepper is a server secret, so a leaked
  database alone can't be used to test guesses offline.
- Tokens created before peppering (plain sha256) still verify once and are upgraded in place.
- Every token belongs to exactly one workspace (control.api_tokens.workspace_id); verifying a token
  tells you which.
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


def create(name: str, actor: str, scopes: list[str] | None = None, kind: str = "device") -> dict:
    """A new token for the current workspace (and user, if signed in). Returned once, never stored."""
    p = context.require()
    name = (name or "").strip()
    if not name:
        raise Invalid("give the device a name, e.g. 'laptop' or 'iphone'")
    if len(name) > 64:
        raise Invalid("name is too long")
    token = PREFIXES[kind] + _secrets.token_urlsafe(32)
    with db.ctl_conn() as c:
        row = c.execute("INSERT INTO control.api_tokens (workspace_id, user_id, kind, name, token_hash, scopes) "
                        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, name, scopes, created_at",
                        (p.workspace.id, p.user_id, kind, name, _hmac(token), scopes or ["notes"])).fetchone()
        _event(c, "token_created", row["id"], actor, name=name, token_kind=kind)
    return {**_plain(row), "token": token}


def verify(token: str, scope: str = "notes") -> dict | None:
    """The token's row plus its workspace, or None. Works without a current workspace (it finds it)."""
    if not token or not token.startswith(ANY_PREFIX) or len(token) > 200:
        return None
    peppered = _hmac(token)
    q = ("UPDATE control.api_tokens t SET {set} last_used_at = now() FROM control.workspaces w "
         "WHERE w.id = t.workspace_id AND t.token_hash = %s AND t.revoked_at IS NULL "
         "AND (t.expires_at IS NULL OR t.expires_at > now()) AND %s = ANY(t.scopes) "
         "AND w.status IN ('active', 'needs_attention') "
         "RETURNING t.id, t.name, t.kind, t.scopes, t.user_id, w.id AS ws_id, w.slug, w.name AS ws_name, "
         "w.db_name, w.role_name")
    with db.ctl_conn() as c:
        row = c.execute(q.format(set=""), (peppered, scope)).fetchone()
        if row is None:   # pre-pepper token: verify with the old hash once, then upgrade it
            row = c.execute(q.format(set="token_hash = %s,"), (peppered, _legacy(token), scope)).fetchone()
    if not row:
        return None
    row = _plain(row)
    row["workspace"] = context.Workspace(id=row["ws_id"], slug=row["slug"], name=row["ws_name"],
                                         db_name=row["db_name"], role_name=row["role_name"])
    return row


def list_all() -> list[dict]:
    p = context.require()
    with db.ctl_conn() as c:
        return _plain(c.execute(
            "SELECT id, name, kind, scopes, created_at, last_used_at, revoked_at FROM control.api_tokens "
            "WHERE workspace_id = %s ORDER BY revoked_at NULLS FIRST, created_at DESC", (p.workspace.id,)).fetchall())


def revoke(token_id: int, actor: str) -> None:
    p = context.require()
    with db.ctl_conn() as c:
        r = c.execute("UPDATE control.api_tokens SET revoked_at = coalesce(revoked_at, now()) "
                      "WHERE id = %s AND workspace_id = %s RETURNING name", (token_id, p.workspace.id)).fetchone()
        if not r:
            raise NotFound(f"no token {token_id}")
        _event(c, "token_revoked", token_id, actor, name=r["name"])
