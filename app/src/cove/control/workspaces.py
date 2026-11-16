"""Workspaces: provisioning (privileged CLI) and lookups (app).

Provisioning creates, for a workspace with id <id> (first 12 hex digits of its uuid):
  - roles `ws_<id>` and `ws_<id>_ro` (NOLOGIN). `cove_app` / `cove_readonly` may only SET ROLE into them
    (GRANT … WITH INHERIT FALSE, SET TRUE), so they never hold the privileges implicitly
  - database `cove_ws_<id>`: CONNECT only for cove_app/cove_readonly, then all domain migrations, with
    privileges moved onto the workspace roles (cove.control.migrate.regrant)
  - a blob folder /data/blobs/<id>

    python -m cove.control.workspaces list
    python -m cove.control.workspaces create <slug> <name> [--owner <username>]
    python -m cove.control.workspaces adopt <existing-db> <slug> <name> [--owner <username>]
    python -m cove.control.workspaces add-member <slug> <username> [owner|editor|viewer]
    python -m cove.control.workspaces token <slug> <name> [--scopes a,b,…] [--kind ingest]
        an agent token made by the admin (before anyone can sign in, or for automation); default
        scopes are read-only plus agent memory. Printed once.

`adopt` turns an existing single-tenant database (the original `cove`) into a workspace by renaming it,
then moves its device tokens into cove_control and its attachments into the workspace's blob folder.
Run these with the owner's credentials: `docker compose run --rm migrate python -m cove.control.workspaces …`.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from psycopg import sql

from cove import context, db
from cove.control import migrate
from cove.crm.service import Invalid, NotFound, _plain

DB_PREFIX = os.environ.get("COVE_WS_DB_PREFIX", "cove_ws_")
ROLE_PREFIX = os.environ.get("COVE_WS_ROLE_PREFIX", "ws_")
BLOB_ROOT = Path(os.environ.get("COVE_BLOB_DIR", "/data/blobs"))
CONTROL_DB = os.environ.get("COVE_CONTROL_DB", "cove_control")
COLS = "id, slug, name, db_name, role_name, status, status_detail, schema_version, created_at"


# ------------------------------------------------------------------ app-side lookups (cove_app)
def as_context(row: dict) -> context.Workspace:
    return context.Workspace(id=str(row["id"]), slug=row["slug"], name=row["name"], db_name=row["db_name"],
                             role_name=row["role_name"])


def for_user(user_id) -> list[dict]:
    """Active workspaces the user is a member of, with their role, oldest first."""
    with db.ctl_conn() as c:
        return _plain(c.execute(
            f"SELECT {', '.join('w.' + x for x in COLS.split(', '))}, m.role FROM control.memberships m "
            "JOIN control.workspaces w ON w.id = m.workspace_id "
            "WHERE m.user_id = %s AND w.status IN ('active', 'needs_attention') ORDER BY w.created_at",
            (str(user_id),)).fetchall())


def by_slug(slug: str) -> dict:
    with db.ctl_conn() as c:
        row = c.execute(f"SELECT {COLS} FROM control.workspaces WHERE slug = %s", (slug,)).fetchone()
    if not row:
        raise NotFound(f"no workspace '{slug}'")
    return _plain(row)


def list_all() -> list[dict]:
    with db.ctl_conn() as c:
        return _plain(c.execute(
            f"SELECT {COLS}, (SELECT count(*) FROM control.memberships m WHERE m.workspace_id = w.id) AS members "
            "FROM control.workspaces w ORDER BY created_at").fetchall())


# ------------------------------------------------------------------ provisioning (owner credentials)
def _names(ws_id: uuid.UUID) -> tuple[str, str]:
    short = ws_id.hex[:12]
    return DB_PREFIX + short, ROLE_PREFIX + short


def _check_slug(slug: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", slug):
        raise Invalid("slug: 2–31 characters, lowercase letters, digits and dashes, starting with a letter")


def _roles(c, role: str, db_name: str) -> None:
    """Create the workspace roles (if missing) and let the shared login roles switch into them only."""
    for name, login in ((role, "cove_app"), (role + "_ro", "cove_readonly")):
        if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
            c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
        c.execute(sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(
            sql.Identifier(name), sql.Identifier(login)))
    c.execute(sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db_name)))
    c.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO cove_app, cove_readonly").format(sql.Identifier(db_name)))


def _register(ctl, ws_id, slug, name, db_name, role) -> dict:
    return ctl.execute(f"INSERT INTO control.workspaces (id, slug, name, db_name, role_name) "
                       f"VALUES (%s, %s, %s, %s, %s) RETURNING {COLS}",
                       (ws_id, slug, name, db_name, role)).fetchone()


def _add_owner(ctl, ws_id, username: str | None) -> None:
    if not username:
        return
    user = ctl.execute("SELECT id FROM control.users WHERE username = %s", (username,)).fetchone()
    if not user:
        raise NotFound(f"no cove user '{username}' (they appear here after their first sign-in)")
    ctl.execute("INSERT INTO control.memberships (workspace_id, user_id, role) VALUES (%s, %s, 'owner') "
                "ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = 'owner'", (ws_id, user["id"]))


def create(slug: str, name: str, owner: str | None = None) -> dict:
    _check_slug(slug)
    ws_id = uuid.uuid4()
    db_name, role = _names(ws_id)
    with migrate._connect(CONTROL_DB) as ctl:
        ws = _register(ctl, ws_id, slug, name, db_name, role)
        _add_owner(ctl, ws_id, owner)
    with migrate._connect("postgres") as c:
        c.execute(sql.SQL("CREATE DATABASE {} OWNER cove_owner").format(sql.Identifier(db_name)))
        _roles(c, role, db_name)
    (BLOB_ROOT / ws_id.hex[:12]).mkdir(parents=True, exist_ok=True)
    migrate.migrate_workspace(ws, control_db=CONTROL_DB)
    return _plain({**ws, "status": "active"})


def adopt(existing_db: str, slug: str, name: str, owner: str | None = None) -> dict:
    """Turn an existing database into a workspace: rename it, move its tokens and attachments."""
    _check_slug(slug)
    ws_id = uuid.uuid4()
    db_name, role = _names(ws_id)
    with migrate._connect("postgres") as c:
        busy = c.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE datname = %s", (existing_db,)).fetchone()["n"]
        if busy:
            raise RuntimeError(f"{busy} connection(s) to {existing_db}; stop web, mcp and backup first")
        c.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(existing_db), sql.Identifier(db_name)))
        _roles(c, role, db_name)
    with migrate._connect(CONTROL_DB) as ctl:
        ws = _register(ctl, ws_id, slug, name, db_name, role)
        _add_owner(ctl, ws_id, owner)
    with migrate._connect(db_name) as wc, migrate._connect(CONTROL_DB) as ctl:
        has = wc.execute("SELECT to_regclass('meta.api_tokens') IS NOT NULL AS t").fetchone()["t"]
        for t in (wc.execute("SELECT name, token_hash, scopes, created_at, last_used_at, revoked_at "
                             "FROM meta.api_tokens").fetchall() if has else []):
            ctl.execute("INSERT INTO control.api_tokens (workspace_id, kind, name, token_hash, scopes, created_at, "
                        "last_used_at, revoked_at) VALUES (%s, 'device', %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (token_hash) DO NOTHING",
                        (ws_id, t["name"], t["token_hash"], t["scopes"], t["created_at"], t["last_used_at"],
                         t["revoked_at"]))
    target = BLOB_ROOT / ws_id.hex[:12]
    target.mkdir(parents=True, exist_ok=True)
    for d in BLOB_ROOT.iterdir():   # legacy layout: /data/blobs/aa/bb/<sha256>
        if d.is_dir() and len(d.name) == 2 and d != target:
            shutil.move(str(d), str(target / d.name))
    migrate.migrate_workspace(ws, control_db=CONTROL_DB)
    return _plain({**ws, "status": "active"})


def add_member(slug: str, username: str, role: str = "editor") -> None:
    if role not in ("owner", "editor", "viewer"):
        raise Invalid("role must be owner, editor or viewer")
    with migrate._connect(CONTROL_DB) as ctl:
        ws = ctl.execute("SELECT id FROM control.workspaces WHERE slug = %s", (slug,)).fetchone()
        user = ctl.execute("SELECT id FROM control.users WHERE username = %s", (username,)).fetchone()
        if not ws or not user:
            raise NotFound("no such workspace or user")
        ctl.execute("INSERT INTO control.memberships (workspace_id, user_id, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role", (ws["id"], user["id"], role))


def admin_token(slug: str, name: str, scopes: list[str] | None = None, kind: str = "personal") -> str:
    """A token for a workspace without signing in (bootstrap and scripts). kind: personal, or ingest (a write-only
    message collector token carrying only messages:ingest)."""
    from cove.auth import policy, tokens
    if kind == "ingest":
        wanted = policy.INGEST_SCOPES
    else:
        wanted = policy.expand(scopes) if scopes else policy.DEFAULT_AGENT_SCOPES
        if wanted & policy.INGEST_SCOPES:
            raise Invalid("messages:ingest goes on an ingest token: use --kind ingest")
    if not wanted or kind not in ("personal", "ingest"):
        raise Invalid(f"unknown scopes or kind; choose from {', '.join(sorted(policy.ALL))}")
    token = tokens.PREFIXES[kind] + __import__("secrets").token_urlsafe(32)
    with migrate._connect(CONTROL_DB) as ctl:
        ws = ctl.execute("SELECT id FROM control.workspaces WHERE slug = %s", (slug,)).fetchone()
        if not ws:
            raise NotFound(f"no workspace '{slug}'")
        tid = ctl.execute("INSERT INTO control.api_tokens (workspace_id, kind, name, token_hash, scopes) "
                          "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                          (ws["id"], kind, name, tokens._hmac(token), sorted(wanted))).fetchone()["id"]
        ctl.execute("INSERT INTO control.security_events (kind, detail) VALUES ('token_created', %s)",
                    (__import__("json").dumps({"token_id": tid, "name": name, "by": "admin-cli", "workspace": slug}),))
    return token


def _main(argv: list[str]) -> int:
    owner = None
    scopes = None
    if "--scopes" in argv:
        i = argv.index("--scopes")
        scopes = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
        argv = argv[:i] + argv[i + 2:]
    kind = "personal"
    if "--kind" in argv:
        i = argv.index("--kind")
        kind = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if "--owner" in argv:
        i = argv.index("--owner")
        owner = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    match argv:
        case ["list"]:
            with migrate._connect(CONTROL_DB) as ctl:
                for w in ctl.execute(f"SELECT {COLS} FROM control.workspaces ORDER BY created_at").fetchall():
                    print(f"{w['slug']:16} {w['status']:16} {w['db_name']:24} v{w['schema_version']}  {w['name']}")
        case ["create", slug, name]:
            print(create(slug, name, owner))
        case ["adopt", existing, slug, name]:
            print(adopt(existing, slug, name, owner))
        case ["token", slug, name]:
            print(admin_token(slug, name, scopes, kind))
        case ["add-member", slug, username, *role]:
            add_member(slug, username, *(role or ["editor"]))
        case _:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
