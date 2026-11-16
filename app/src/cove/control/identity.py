"""Users, their links to the identity provider, and the security event log (cove_control).

Users are matched on (issuer, subject) from the ID token, never on email, so an address change at the
provider (or a reused address) can't hand someone else's account over.
"""

from __future__ import annotations

import json
from typing import Any

from cove import db
from cove.crm.service import NotFound, _plain

USER_COLS = "id, username, email, display_name, is_admin, created_at, last_login_at, disabled_at"


class Disabled(Exception):
    """The account exists but has been disabled by an admin."""


def event(kind: str, *, user_id: Any = None, session_id: Any = None, ip: str | None = None,
          user_agent: str | None = None, conn=None, **detail) -> None:
    """Append to control.security_events (append-only for the app role)."""
    sql = ("INSERT INTO control.security_events (kind, user_id, session_id, ip, user_agent, detail) "
           "VALUES (%s, %s, %s, %s, %s, %s)")
    args = (kind, user_id, session_id, ip, (user_agent or "")[:300] or None, json.dumps(detail, default=str))
    if conn is not None:
        conn.execute(sql, args)
    else:
        with db.ctl_conn() as c:
            c.execute(sql, args)


def sign_in(issuer: str, subject: str, claims: dict, *, ip: str | None, user_agent: str | None) -> dict:
    """Find or create the user for a verified ID token and record the login.

    The very first user becomes the instance admin. That's safe because the identity provider is
    invite-only and the cove OIDC client is restricted to the provider's `cove` group.
    """
    username = (claims.get("preferred_username") or claims.get("email") or subject)[:100]
    email = claims.get("email") if claims.get("email_verified", True) else None
    name = claims.get("name") or username
    with db.ctl_conn() as c:
        row = c.execute(f"SELECT {USER_COLS} FROM control.users WHERE id = (SELECT user_id FROM control.identities "
                        "WHERE issuer = %s AND subject = %s)", (issuer, subject)).fetchone()
        if row is None:
            c.execute("LOCK TABLE control.users IN SHARE ROW EXCLUSIVE MODE")   # one 'first user' only
            first = c.execute("SELECT NOT EXISTS (SELECT 1 FROM control.users) AS first").fetchone()["first"]
            row = c.execute(f"INSERT INTO control.users (username, email, display_name, is_admin) "
                            f"VALUES (%s, %s, %s, %s) RETURNING {USER_COLS}", (username, email, name, first)).fetchone()
            c.execute("INSERT INTO control.identities (issuer, subject, user_id) VALUES (%s, %s, %s)",
                      (issuer, subject, row["id"]))
            event("user_created", user_id=row["id"], ip=ip, user_agent=user_agent, conn=c,
                  username=username, admin=first)
            if first:   # bootstrap: the first admin owns the workspaces that were set up before anyone signed in
                adopted = c.execute("INSERT INTO control.memberships (workspace_id, user_id, role) "
                                    "SELECT w.id, %s, 'owner' FROM control.workspaces w WHERE NOT EXISTS "
                                    "(SELECT 1 FROM control.memberships m WHERE m.workspace_id = w.id)",
                                    (row["id"],)).rowcount
                if adopted:
                    event("workspaces_adopted", user_id=row["id"], conn=c, count=adopted)
        if row["disabled_at"]:
            event("login_refused", user_id=row["id"], ip=ip, user_agent=user_agent, conn=c, reason="disabled")
            raise Disabled(username)
        # keep profile fields in step with the provider
        row = c.execute(f"UPDATE control.users SET username=%s, email=%s, display_name=%s, last_login_at=now() "
                        f"WHERE id=%s RETURNING {USER_COLS}", (username, email, name, row["id"])).fetchone()
    return _plain(row)


def get_user(user_id: Any) -> dict:
    with db.ctl_conn() as c:
        row = c.execute(f"SELECT {USER_COLS} FROM control.users WHERE id = %s", (str(user_id),)).fetchone()
    if not row:
        raise NotFound(f"no user {user_id}")
    return _plain(row)


def list_users() -> list[dict]:
    with db.ctl_conn() as c:
        return _plain(c.execute(
            f"SELECT {USER_COLS}, (SELECT count(*) FROM control.sessions s WHERE s.user_id = u.id "
            "AND s.revoked_at IS NULL AND s.expires_at > now()) AS active_sessions "
            "FROM control.users u ORDER BY created_at").fetchall())


def set_disabled(user_id: Any, disabled: bool, *, by: dict, ip: str | None = None) -> None:
    """Disable (and sign out everywhere) or re-enable a user. Admins can't disable themselves."""
    if str(user_id) == str(by["id"]):
        raise PermissionError("you can't disable your own account")
    with db.ctl_conn() as c:
        r = c.execute("UPDATE control.users SET disabled_at = CASE WHEN %s THEN coalesce(disabled_at, now()) END "
                      "WHERE id = %s RETURNING username", (disabled, str(user_id))).fetchone()
        if not r:
            raise NotFound(f"no user {user_id}")
        if disabled:
            c.execute("UPDATE control.sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                      (str(user_id),))
        event("user_disabled" if disabled else "user_enabled", user_id=user_id, ip=ip, conn=c,
              by=by["username"], username=r["username"])


def set_admin(user_id: Any, admin: bool, *, by: dict, ip: str | None = None) -> None:
    if str(user_id) == str(by["id"]) and not admin:
        raise PermissionError("you can't remove your own admin rights")
    with db.ctl_conn() as c:
        r = c.execute("UPDATE control.users SET is_admin = %s WHERE id = %s RETURNING username",
                      (admin, str(user_id))).fetchone()
        if not r:
            raise NotFound(f"no user {user_id}")
        event("admin_granted" if admin else "admin_revoked", user_id=user_id, ip=ip, conn=c,
              by=by["username"], username=r["username"])


def events(user_id: Any = None, limit: int = 100) -> list[dict]:
    with db.ctl_conn() as c:
        return _plain(c.execute(
            "SELECT e.id, e.at, e.kind, e.ip::text AS ip, e.user_agent, e.detail, u.username "
            "FROM control.security_events e LEFT JOIN control.users u ON u.id = e.user_id "
            "WHERE %s::uuid IS NULL OR e.user_id = %s::uuid ORDER BY e.at DESC, e.id DESC LIMIT %s",
            (user_id and str(user_id), user_id and str(user_id), limit)).fetchall())

