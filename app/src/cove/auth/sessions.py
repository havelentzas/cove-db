"""Server-side browser sessions (cove_control.sessions).

- The cookie holds 256 random bits; the database stores only HMAC(token_pepper, token), so a database
  leak doesn't yield usable cookies.
- A session ends after 7 days without use, or 30 days after sign-in, whichever comes first; a new
  session (new ID) is created at every sign-in, and sign-out ends it server-side.
- The CSRF token is derived from the session token, so it's per-session and needs no storage.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets
from datetime import timedelta
from typing import Any

from cove import db, secrets
from cove.control import identity
from cove.crm.service import _plain

COOKIE = "__Host-cove_session"     # __Host-: Secure, Path=/, no Domain — can't be set by a subdomain
IDLE = timedelta(days=7)
ABSOLUTE = timedelta(days=30)
TOUCH_EVERY = timedelta(minutes=5)  # write last_seen_at at most this often


def _key(purpose: str) -> bytes:
    return hmac.new(secrets.get("session_key").encode(), purpose.encode(), hashlib.sha256).digest()


def _hash(token: str) -> str:
    return hmac.new(secrets.get("token_pepper").encode(), ("session:" + token).encode(), hashlib.sha256).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.new(_key("csrf"), token.encode(), hashlib.sha256).hexdigest()


def check_csrf(token: str, submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(csrf_token(token), submitted)


def create(user: dict, *, ip: str | None, user_agent: str | None) -> str:
    token = _secrets.token_urlsafe(32)
    with db.ctl_conn() as c:
        sid = c.execute("INSERT INTO control.sessions (token_hash, user_id, expires_at, ip, user_agent) "
                        "VALUES (%s, %s, now() + %s, %s, %s) RETURNING id",
                        (_hash(token), user["id"], ABSOLUTE, ip, (user_agent or "")[:300])).fetchone()["id"]
        identity.event("login", user_id=user["id"], session_id=sid, ip=ip, user_agent=user_agent, conn=c)
    return token


def lookup(token: str | None) -> dict | None:
    """The live session and its user for a cookie value, or None. Expired sessions are revoked."""
    if not token or len(token) > 100:
        return None
    with db.ctl_conn() as c:
        row = c.execute(
            "SELECT s.id AS session_id, s.created_at, s.last_seen_at, s.expires_at, "
            "       s.expires_at < now() OR s.last_seen_at < now() - %s AS expired, "
            "       s.last_seen_at < now() - %s AS stale, "
            "       u.id, u.username, u.email, u.display_name, u.is_admin, u.disabled_at "
            "FROM control.sessions s JOIN control.users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s AND s.revoked_at IS NULL", (IDLE, TOUCH_EVERY, _hash(token))).fetchone()
        if row is None:
            return None
        if row["expired"] or row["disabled_at"]:
            c.execute("UPDATE control.sessions SET revoked_at = now() WHERE id = %s", (row["session_id"],))
            return None
        if row["stale"]:
            c.execute("UPDATE control.sessions SET last_seen_at = now() WHERE id = %s", (row["session_id"],))
    return _plain(row)


def revoke(session_id: Any, *, user_id: Any, by: Any = None, ip: str | None = None) -> bool:
    """End one of `user_id`'s sessions. Returns False if there was no such live session."""
    with db.ctl_conn() as c:
        r = c.execute("UPDATE control.sessions SET revoked_at = now() WHERE id = %s AND user_id = %s "
                      "AND revoked_at IS NULL RETURNING id", (str(session_id), str(user_id))).fetchone()
        if r:
            identity.event("session_revoked", user_id=user_id, session_id=session_id, ip=ip, conn=c,
                           by=str(by or user_id))
    return r is not None


def revoke_all(user_id: Any, *, except_session: Any = None, by: Any = None, ip: str | None = None) -> int:
    with db.ctl_conn() as c:
        n = c.execute("UPDATE control.sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL "
                      "AND id IS DISTINCT FROM %s", (str(user_id), except_session and str(except_session))).rowcount
        identity.event("sessions_revoked_all", user_id=user_id, ip=ip, conn=c, count=n, by=str(by or user_id),
                       kept=str(except_session) if except_session else None)
    return n


def list_for(user_id: Any) -> list[dict]:
    with db.ctl_conn() as c:
        return _plain(c.execute(
            "SELECT id, created_at, last_seen_at, expires_at, ip::text AS ip, user_agent FROM control.sessions "
            "WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now() AND last_seen_at > now() - %s "
            "ORDER BY last_seen_at DESC", (str(user_id), IDLE)).fetchall())
