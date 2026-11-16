"""Which services are installed and turned on in the current workspace (meta.services).

A service's migration inserts its row. Turning a service off hides its tools, pages and capabilities but
keeps its data. A service is `not_installed` when it has no row, or when its code isn't present.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cove import context, db
from cove.auth import policy


def _rows() -> list[dict]:
    try:
        with db.conn() as c:
            return c.execute("SELECT key, enabled, installed_version, settings, updated_at FROM meta.services "
                             "ORDER BY key").fetchall()
    except psycopg.errors.UndefinedTable:   # a workspace that hasn't been migrated yet
        return []


def states() -> dict[str, str]:
    """{service: 'enabled' | 'disabled' | 'not_installed'} for every service with code or a row."""
    from cove.fabric.core import manifests
    known = manifests()
    out = {k: "not_installed" for k in known}
    for r in _rows():
        out[r["key"]] = ("enabled" if r["enabled"] else "disabled") if r["key"] in known else "not_installed"
    return out


def state(key: str) -> str:
    return states().get(key, "not_installed")


def is_enabled(key: str) -> bool:
    return state(key) == "enabled"


def list_services() -> list[dict]:
    from cove.crm.service import _plain
    from cove.fabric.core import manifests
    known = manifests()
    return _plain([{**r, "state": ("enabled" if r["enabled"] else "disabled") if r["key"] in known else "not_installed"}
                   for r in _rows()])


def settings(key: str) -> dict:
    for r in _rows():
        if r["key"] == key:
            return dict(r["settings"] or {})
    return {}


def _owner() -> context.Principal:
    p = context.require()
    if p.role != "owner":
        raise policy.Forbidden("only the workspace owner can change services")
    return p


def set_enabled(key: str, enabled: bool, actor: str) -> dict:
    """Turn a service on or off in this workspace (owner only). Its data is always kept."""
    from cove.crm.service import NotFound, _audit, _plain
    _owner()
    with db.conn() as c:
        row = c.execute("UPDATE meta.services SET enabled = %s, updated_at = now() WHERE key = %s "
                        "RETURNING key, enabled", (bool(enabled), key)).fetchone()
        if not row:
            raise NotFound(f"no installed service '{key}'")
        _audit(c, actor, "meta.service", key, "enable" if enabled else "disable")
    return _plain(row)


def update_settings(key: str, patch: dict[str, Any], actor: str) -> dict:
    """Merge `patch` into a service's settings (owner only); keys set to None are removed."""
    from cove.crm.service import NotFound, _audit
    _owner()
    with db.conn() as c:
        row = c.execute("SELECT settings FROM meta.services WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if not row:
            raise NotFound(f"no installed service '{key}'")
        new = {**(row["settings"] or {}), **patch}
        new = {k: v for k, v in new.items() if v is not None}
        c.execute("UPDATE meta.services SET settings = %s, updated_at = now() WHERE key = %s", (Jsonb(new), key))
        _audit(c, actor, "meta.service", key, "settings", {"keys": sorted(patch)})
    return new


from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {
    None: ["states", "state", "is_enabled", "list_services", "settings", "set_enabled", "update_settings"],
})
