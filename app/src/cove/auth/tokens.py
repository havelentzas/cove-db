"""API tokens (devices, and later personal/agent tokens).

- Tokens are random 256-bit values with a typed prefix (cove_dev_…, cove_pat_…) so secret
  scanners can recognise them; they are shown once and never stored.
- The database stores HMAC-SHA256(pepper, token). The pepper is a server secret, so a leaked
  database alone can't be used to test guesses offline.
- Tokens created before peppering (plain sha256) still verify once and are upgraded in place.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets

from cove import db, secrets
from cove.crm.service import Invalid, NotFound, _audit, _plain

PREFIXES = {"device": "cove_dev_", "personal": "cove_pat_"}
ANY_PREFIX = "cove_"


def _hmac(token: str) -> str:
    return hmac.new(secrets.get("token_pepper").encode(), token.encode(), hashlib.sha256).hexdigest()


def _legacy(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(name: str, actor: str, scopes: list[str] | None = None, kind: str = "device") -> dict:
    name = (name or "").strip()
    if not name:
        raise Invalid("give the device a name, e.g. 'laptop' or 'iphone'")
    if len(name) > 64:
        raise Invalid("name is too long")
    token = PREFIXES[kind] + _secrets.token_urlsafe(32)
    with db.conn() as c:
        row = c.execute("INSERT INTO meta.api_tokens (name, token_hash, scopes) VALUES (%s,%s,%s) "
                        "RETURNING id, name, scopes, created_at", (name, _hmac(token), scopes or ["notes"])).fetchone()
        _audit(c, actor, "meta.api_token", row["id"], "create", {"name": name})
    return {**_plain(row), "token": token}


def verify(token: str, scope: str = "notes") -> dict | None:
    if not token or not token.startswith(ANY_PREFIX) or len(token) > 200:
        return None
    peppered = _hmac(token)
    with db.conn() as c:
        row = c.execute("UPDATE meta.api_tokens SET last_used_at = now() WHERE token_hash=%s AND revoked_at IS NULL "
                        "AND %s = ANY(scopes) RETURNING id, name, scopes", (peppered, scope)).fetchone()
        if row is None:   # pre-pepper token: verify with the old hash once, then upgrade it
            row = c.execute("UPDATE meta.api_tokens SET token_hash=%s, last_used_at = now() WHERE token_hash=%s "
                            "AND revoked_at IS NULL AND %s = ANY(scopes) RETURNING id, name, scopes",
                            (peppered, _legacy(token), scope)).fetchone()
            if row:
                _audit(c, "system", "meta.api_token", row["id"], "rehash", {"name": row["name"]})
    return _plain(row) if row else None


def list_all() -> list[dict]:
    with db.conn() as c:
        return _plain(c.execute("SELECT id, name, scopes, created_at, last_used_at, revoked_at FROM meta.api_tokens "
                                "ORDER BY revoked_at NULLS FIRST, created_at DESC").fetchall())


def revoke(token_id: int, actor: str) -> None:
    with db.conn() as c:
        r = c.execute("UPDATE meta.api_tokens SET revoked_at = coalesce(revoked_at, now()) WHERE id=%s RETURNING name",
                      (token_id,)).fetchone()
        if not r:
            raise NotFound(f"no token {token_id}")
        _audit(c, actor, "meta.api_token", token_id, "revoke", {"name": r["name"]})
