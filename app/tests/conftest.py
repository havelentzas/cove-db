import os
import re
from pathlib import Path

import psycopg
import pytest

MIGRATIONS = Path(os.environ.get("COVE_MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "db" / "migrations"))
CONTROL_MIGRATIONS = MIGRATIONS.parent / "control"


def _up_sql(path: Path) -> str:
    text = path.read_text()
    return re.split(r"^-- migrate:down", text.split("-- migrate:up", 1)[1], flags=re.M)[0]


@pytest.fixture()
def database():
    """Fresh crm/meta/notes schemas, and the control schema (normally its own database), in cove_test."""
    url = os.environ.get("COVE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("COVE_TEST_DATABASE_URL not set")
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS crm CASCADE")
        c.execute("DROP SCHEMA IF EXISTS meta CASCADE")
        c.execute("DROP SCHEMA IF EXISTS notes CASCADE")
        c.execute("DROP SCHEMA IF EXISTS control CASCADE")
        for f in [*sorted(MIGRATIONS.glob("*.sql")), *sorted(CONTROL_MIGRATIONS.glob("*.sql"))]:
            c.execute(_up_sql(f))
    from cove import db
    db.configure(url)
    yield url
    db.close()


@pytest.fixture()
def blobs(tmp_path, monkeypatch):
    from cove.notes import service
    monkeypatch.setattr(service, "BLOB_DIR", tmp_path / "blobs")
    return tmp_path / "blobs"


class Browser:
    """A signed-in browser: TestClient with a session cookie, plus the session's CSRF token."""

    def __init__(self, username="harry", admin=None):
        from fastapi.testclient import TestClient
        from cove.auth import sessions
        from cove.control import identity
        from cove.web.app import app
        self.user = identity.sign_in("https://id.test", "sub-" + username,
                                     {"preferred_username": username, "name": username.title()}, ip=None, user_agent="pytest")
        if admin is not None:
            with __import__("cove").db.ctl_conn() as c:
                c.execute("UPDATE control.users SET is_admin=%s WHERE id=%s", (admin, self.user["id"]))
        self.token = sessions.create(self.user, ip="127.0.0.1", user_agent="pytest")
        self.csrf = sessions.csrf_token(self.token)
        self.client = TestClient(app, base_url="https://cove.test", follow_redirects=False)
        self.client.cookies.set(sessions.COOKIE, self.token)

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, data=None, **kw):
        data = {"csrf": self.csrf, **(data or {})}
        kw.setdefault("headers", {}).setdefault("origin", "https://cove.test")
        return self.client.post(url, data=data, **kw)


@pytest.fixture()
def browser(database):
    return Browser()


@pytest.fixture(autouse=True)
def _fresh_rate_limits(monkeypatch):
    """Rate limits are per process; start every test with an empty window."""
    from cove.web import security
    monkeypatch.setattr(security, "limiter", security.RateLimiter())
