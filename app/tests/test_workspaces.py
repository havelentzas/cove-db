"""Workspaces: provisioning, database-level isolation, migration fan-out, adopting the original database.

These tests create real databases and roles in the cluster (prefixed cove_twst_ / twst_) and drop them
afterwards. Control tables live in cove_test (schema `control`), as in the other tests.
"""

import os
import uuid

import psycopg
import pytest

from cove import context, db, secrets
from cove.control import migrate, workspaces
from cove.crm import service as svc
from cove.notes import service as notes


def _owner(database: str) -> psycopg.Connection:
    return psycopg.connect(secrets.database_url("cove_owner", database), autocommit=True)


def _login(role: str, database: str) -> psycopg.Connection:
    return psycopg.connect(secrets.database_url(role, database), autocommit=True)


def _cleanup():
    db.close()
    with _owner("postgres") as c:
        for (name,) in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'cove\\_twst\\_%'").fetchall():
            c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        for (name,) in c.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'twst\\_%'").fetchall():
            c.execute(f'DROP ROLE "{name}"')


@pytest.fixture()
def real(database, monkeypatch, tmp_path):
    """Leave test mode: real per-workspace pools, control tables in cove_test."""
    if not os.path.exists("/run/secrets/app_password"):
        pytest.skip("needs the cluster's login roles (run inside the test container)")
    monkeypatch.setattr(workspaces, "DB_PREFIX", "cove_twst_")
    monkeypatch.setattr(workspaces, "ROLE_PREFIX", "twst_")
    monkeypatch.setattr(workspaces, "CONTROL_DB", "cove_test")
    monkeypatch.setattr(workspaces, "BLOB_ROOT", tmp_path / "blobs")
    monkeypatch.setattr(notes, "BLOB_DIR", tmp_path / "blobs")
    monkeypatch.setenv("COVE_CONTROL_DATABASE_URL", database)
    _cleanup()
    with _owner("cove_test") as c:   # only real workspaces from here on
        c.execute("DELETE FROM control.workspaces")
    db.close()   # leave test mode
    token = context.set_current(None)
    yield
    context._current.reset(token)
    _cleanup()
    db.configure(database)


def principal(ws: dict) -> context.Principal:
    from cove.auth import policy
    return context.Principal(actor="web:test", workspace=workspaces.as_context(ws), scopes=policy.ALL)


def card(name: str) -> dict:
    return {"@type": "Card", "version": "1.0", "name": {"full": name}}


def test_workspaces_are_isolated(real):
    a, b = workspaces.create("ws-a", "A"), workspaces.create("ws-b", "B")
    assert a["db_name"].startswith("cove_twst_") and a["db_name"] != b["db_name"]
    with context.use(principal(a)):
        svc.create_contact(card("Alice"), "web:test")
        sha = notes.sha256(b"a-secret")
        notes.store_blob(sha, b"a-secret")
    with context.use(principal(b)):
        assert [c["display_name"] for c in svc.search_contacts()] == []
        svc.create_contact(card("Bob"), "web:test")
        assert not notes.has_blob(sha)                 # can't even probe A's attachments
        with pytest.raises(svc.NotFound):
            notes.blob_file(sha)
    with context.use(principal(a)):
        assert [c["display_name"] for c in svc.search_contacts()] == ["Alice"]
        assert notes.blob_file(sha).read_bytes() == b"a-secret"


def test_no_workspace_means_no_data_access(real):
    with pytest.raises(context.NoWorkspace):
        svc.search_contacts()


def test_database_roles_enforce_isolation(real):
    a, b = workspaces.create("ws-a", "A"), workspaces.create("ws-b", "B")
    # the login role has no privileges of its own in a workspace database
    with _login("cove_app", a["db_name"]) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("SELECT * FROM crm.contacts")
    # ...and switching into A's role inside B's database doesn't help
    with _login("cove_app", b["db_name"]) as c:
        c.execute(f'SET ROLE "{a["role_name"]}"')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM crm.contacts")
    # the right role in the right database works
    with _login("cove_app", a["db_name"]) as c:
        c.execute(f'SET ROLE "{a["role_name"]}"')
        c.execute("SELECT count(*) FROM crm.contacts")
    # the read-only login can't climb to the read/write role, or write with its own
    with _login("cove_readonly", a["db_name"]) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(f'SET ROLE "{a["role_name"]}"')
        c.execute(f'SET ROLE "{a["role_name"]}_ro"')
        c.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO meta.agent_notes (topic, note, author) VALUES ('x', 'y', 'z')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM meta.api_tokens")


def test_read_only_pool_cannot_write(real):
    a = workspaces.create("ws-a", "A")
    with context.use(principal(a)), db.ro_conn() as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("INSERT INTO meta.agent_notes (topic, note, author) VALUES ('x', 'y', 'z')")


def test_migration_fan_out_isolates_failures(real):
    a, b = workspaces.create("ws-a", "A"), workspaces.create("ws-b", "B")
    with _owner("cove_test") as c:   # a third workspace whose database has gone missing
        c.execute("INSERT INTO control.workspaces (slug, name, db_name, role_name, status) "
                  "VALUES ('ws-c', 'C', 'cove_twst_missing', 'twst_missing', 'active')")
    assert migrate.fan_out("cove_test") == 1
    with _owner("cove_test") as c:
        status = dict(c.execute("SELECT slug, status FROM control.workspaces WHERE slug LIKE 'ws-%'").fetchall())
    assert status == {"ws-a": "active", "ws-b": "active", "ws-c": "needs_attention"}
    # re-running is a no-op, and regranting leaves nothing on the shared roles
    assert migrate.fan_out("cove_test") == 1
    with _owner(a["db_name"]) as c:
        leftover = c.execute("SELECT count(*) FROM pg_class c, aclexplode(c.relacl) x JOIN pg_roles r ON r.oid = x.grantee "
                             "WHERE r.rolname IN ('cove_app', 'cove_readonly')").fetchone()[0]
    assert leftover == 0


def test_adopt_renames_the_database_and_moves_tokens_and_blobs(real):
    legacy = "cove_twst_legacy"
    with _owner("postgres") as c:
        c.execute(f'CREATE DATABASE "{legacy}"')
    with _owner(legacy) as c:
        migrate.apply(c, migrate.DB_DIR / "migrations")
        c.execute("INSERT INTO meta.api_tokens (name, token_hash, scopes) VALUES ('laptop', %s, '{notes}')",
                  (__import__("cove.auth.tokens", fromlist=["x"])._hmac("cove_dev_legacy-token"),))
    old_blob = workspaces.BLOB_ROOT / "ab" / "cd" / ("abcd" + "0" * 60)
    old_blob.parent.mkdir(parents=True)
    old_blob.write_bytes(b"x")
    ws = workspaces.adopt(legacy, "main", "Main")
    with _owner("postgres") as c:
        names = {r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'cove\\_twst\\_%'")}
    assert legacy not in names and ws["db_name"] in names
    from cove.auth import tokens
    dev = tokens.verify("cove_dev_legacy-token")
    assert dev and dev["workspace"].slug == "main"     # the Obsidian plugin keeps working
    short = uuid.UUID(ws["id"]).hex[:12]
    assert (workspaces.BLOB_ROOT / short / "ab" / "cd" / old_blob.name).read_bytes() == b"x"
    assert not old_blob.exists()


# ------------------------------------------------------------------ messages under the real roles
def _ingest_and_read(ws: dict) -> None:
    """A synthetic batch in, read back through the service, a worker round, all as the real workspace role."""
    from cove.fabric import worker
    from cove.messages import ingest, service as messages
    batch = {"format": "cove.messages.ingest/v1",
             "source": {"key": "roles", "platform": "sms", "label": "Roles", "collector": "pytest/1"},
             "conversations": [{"external_id": "c1", "platform": "sms", "kind": "direct",
                                "participants": [{"platform": "sms", "value": "+31612345678"}]}],
             "messages": [{"external_id": "r1", "conversation": "c1", "is_from_me": False,
                           "sender": {"platform": "sms", "value": "+31612345678"},
                           "sent_at": "2026-11-01T09:00:00Z", "body": "synthetic hello"}]}
    with context.use(principal(ws)):
        svc.create_contact(svc.build_card(full_name="Ada", phones=["+31612345678"]), "web:test")
        assert ingest.ingest_batch(batch, "web:test")["accepted"] == 1
        assert ingest.ingest_batch(batch, "web:test")["duplicates"] == 1
        assert messages.search_messages("hello")["items"]
    worker.run_workspace(workspaces.as_context(ws), prune=True)


def _assert_grants(ws: dict) -> None:
    with _login("cove_readonly", ws["db_name"]) as c:
        c.execute(f'SET ROLE "{ws["role_name"]}_ro"')
        for table in ("messages.messages", "messages.handles", "messages.conversations", "meta.events",
                      "meta.event_cursors"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"SELECT * FROM {table} LIMIT 1")
        c.execute("SELECT key FROM meta.services")          # enablement is fine to see
    with _owner(ws["db_name"]) as c:
        leftover = c.execute("SELECT count(*) FROM pg_class c, aclexplode(c.relacl) x JOIN pg_roles r "
                             "ON r.oid = x.grantee WHERE r.rolname IN ('cove_app', 'cove_readonly')").fetchone()[0]
        assert leftover == 0
        assert c.execute("SELECT count(*) FROM meta.services").fetchone()[0] == 3


def test_messages_under_real_roles_in_a_new_workspace(real):
    ws = workspaces.create("ws-m", "M")
    _ingest_and_read(ws)
    _assert_grants(ws)


def test_upgrading_an_existing_workspace_to_the_fabric_and_messages(real, monkeypatch, tmp_path):
    """The live path: a workspace migrated up to 005 (default privileges already moved away) gets 006 and 007."""
    import shutil
    old = tmp_path / "db" / "migrations"
    old.mkdir(parents=True)
    for f in sorted((migrate.DB_DIR / "migrations").glob("*.sql")):
        if f.name < "006":
            shutil.copy(f, old)
    full = migrate.DB_DIR
    monkeypatch.setattr(migrate, "DB_DIR", tmp_path / "db")
    ws = workspaces.create("ws-u", "U")
    with _owner(ws["db_name"]) as c:
        assert not c.execute("SELECT to_regclass('meta.services')").fetchone()[0]
    monkeypatch.setattr(migrate, "DB_DIR", full)
    assert migrate.fan_out("cove_test") == 0
    _ingest_and_read(ws)
    _assert_grants(ws)


def test_admin_cli_makes_ingest_tokens_that_only_ingest(real):
    from cove.auth import tokens
    from cove.crm.service import Invalid
    workspaces.create("ws-i", "I")
    tok = workspaces.admin_token("ws-i", "mac", kind="ingest")
    assert tok.startswith("cove_ing_")
    row = tokens.verify(tok)
    assert row["kind"] == "ingest" and row["effective"] == {"messages:ingest"}
    with pytest.raises(Invalid):     # collector permissions never go on an agent token
        workspaces.admin_token("ws-i", "agent", ["messages:ingest"])
    with pytest.raises(Invalid):
        workspaces.admin_token("ws-i", "x", kind="device")
