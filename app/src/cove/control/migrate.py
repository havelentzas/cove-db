"""Schema migrations for cove_control and every workspace database (`python -m cove.control.migrate`).

- Files are dbmate-style (`-- migrate:up` / `-- migrate:down`); applied versions are recorded in
  `schema_migrations(version)`, the same table dbmate used, so existing databases carry on seamlessly.
- Order: cove_control (db/control), then each workspace (db/migrations). One workspace failing is marked
  `needs_attention` with the error and doesn't stop the others; the exit code is non-zero if any failed.
- After migrating a workspace, `regrant` moves every privilege the migration files give to the shared
  `cove_app` / `cove_readonly` roles onto that workspace's own `ws_<id>` / `ws_<id>_ro`, and revokes them
  from the shared roles. Migration files stay simple (they grant to cove_app) and isolation still holds.

Runs as the database owner (superuser): it reads the owner password from Docker secrets.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from cove import secrets

DB_DIR = Path(os.environ.get("COVE_DB_DIR", "/srv/db"))
SHARED = {"cove_app": "", "cove_readonly": "_ro"}   # shared role -> suffix of the workspace role


def owner_url(database: str) -> str:
    return secrets.database_url("cove_owner", database)


def _connect(database: str) -> psycopg.Connection:
    return psycopg.connect(owner_url(database), autocommit=True, row_factory=dict_row)


def files(directory: Path) -> list[tuple[str, str]]:
    """[(version, up-sql)] sorted by version (the numeric prefix of the file name)."""
    out = []
    for f in sorted(directory.glob("*.sql")):
        text = f.read_text()
        up = re.split(r"^-- migrate:down", text.split("-- migrate:up", 1)[1], flags=re.M)[0]
        out.append((f.name.split("_", 1)[0], up))
    return out


def apply(c: psycopg.Connection, directory: Path) -> list[str]:
    """Apply pending migrations from `directory` on connection `c`; each in its own transaction."""
    c.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations (version varchar(128) PRIMARY KEY)")
    done = {r["version"] for r in c.execute("SELECT version FROM public.schema_migrations").fetchall()}
    applied = []
    for version, up in files(directory):
        if version in done:
            continue
        with c.transaction():
            c.execute(up)
            c.execute("INSERT INTO public.schema_migrations (version) VALUES (%s)", (version,))
        applied.append(version)
    return applied


def regrant(c: psycopg.Connection, role: str) -> int:
    """Move privileges held by the shared roles to this workspace's roles (`role`, `role_ro`)."""
    moved = 0
    rows = c.execute("""
        SELECT 'SCHEMA' AS kind, quote_ident(n.nspname) AS obj, a.privilege_type AS priv, g.rolname AS grantee
          FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a JOIN pg_roles g ON g.oid = a.grantee
         WHERE g.rolname = ANY(%(shared)s)
        UNION ALL
        SELECT CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, c.oid::regclass::text, a.privilege_type,
               g.rolname
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a JOIN pg_roles g ON g.oid = a.grantee
         WHERE g.rolname = ANY(%(shared)s)""", {"shared": list(SHARED)}).fetchall()
    with c.transaction():
        for r in rows:
            target = role + SHARED[r["grantee"]]
            c.execute(sql.SQL("GRANT {} ON {} {} TO {}").format(
                sql.SQL(r["priv"]), sql.SQL(r["kind"]), sql.SQL(r["obj"]), sql.Identifier(target)))
            c.execute(sql.SQL("REVOKE {} ON {} {} FROM {}").format(
                sql.SQL(r["priv"]), sql.SQL(r["kind"]), sql.SQL(r["obj"]), sql.Identifier(r["grantee"])))
            moved += 1
        # default privileges would hand future tables to the shared roles; they're re-mirrored after each
        # migration instead
        for d in c.execute("SELECT defaclrole::regrole::text AS owner, defaclnamespace::regnamespace::text AS nsp "
                           "FROM pg_default_acl WHERE defaclnamespace <> 0").fetchall():
            for objtype in ("TABLES", "SEQUENCES"):
                c.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE ALL ON {} FROM {}").format(
                    sql.SQL(d["owner"]), sql.SQL(d["nsp"]), sql.SQL(objtype),
                    sql.SQL(", ").join(sql.Identifier(s) for s in SHARED)))
    return moved


def migrate_workspace(ws: dict, migrations: Path | None = None, control_db: str | None = None) -> list[str]:
    with _connect(ws["db_name"]) as c:
        applied = apply(c, migrations or DB_DIR / "migrations")
        regrant(c, ws["role_name"])
        version = c.execute("SELECT max(version) AS v FROM public.schema_migrations").fetchone()["v"]
    with _connect(control_db or os.environ.get("COVE_CONTROL_DB", "cove_control")) as ctl:
        ctl.execute("UPDATE control.workspaces SET status = 'active', status_detail = NULL, schema_version = %s "
                    "WHERE id = %s", (version, ws["id"]))
    return applied


def run(control_db: str = "cove_control") -> int:
    with _connect(control_db) as ctl:
        applied = apply(ctl, DB_DIR / "control")
        print(f"{control_db}: {'applied ' + ', '.join(applied) if applied else 'up to date'}")
    return fan_out(control_db)


def fan_out(control_db: str = "cove_control") -> int:
    """Migrate every workspace; returns the number that failed (each is marked needs_attention)."""
    failures = 0
    with _connect(control_db) as ctl:
        workspaces = ctl.execute("SELECT id, slug, db_name, role_name FROM control.workspaces "
                                 "WHERE status <> 'archived' ORDER BY created_at").fetchall()
    for ws in workspaces:
        try:
            applied = migrate_workspace(ws, control_db=control_db)
            print(f"workspace {ws['slug']} ({ws['db_name']}): {'applied ' + ', '.join(applied) if applied else 'up to date'}")
        except Exception as e:   # one broken workspace must not block the rest
            failures += 1
            print(f"workspace {ws['slug']} ({ws['db_name']}): FAILED: {e}", file=sys.stderr)
            with _connect(control_db) as ctl:
                ctl.execute("UPDATE control.workspaces SET status = 'needs_attention', status_detail = %s WHERE id = %s",
                            (str(e)[:500], ws["id"]))
    if not workspaces:
        print("no workspaces yet: create one with `python -m cove.control.workspaces create <slug> <name>`")
    return failures


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
