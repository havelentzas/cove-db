import os
import re
from pathlib import Path

import psycopg
import pytest

MIGRATIONS = Path(os.environ.get("COVE_MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "db" / "migrations"))


def _up_sql(path: Path) -> str:
    text = path.read_text()
    return re.split(r"^-- migrate:down", text.split("-- migrate:up", 1)[1], flags=re.M)[0]


@pytest.fixture()
def database():
    """Fresh crm/meta schemas in cove_test for each test."""
    url = os.environ.get("COVE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("COVE_TEST_DATABASE_URL not set")
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS crm CASCADE")
        c.execute("DROP SCHEMA IF EXISTS meta CASCADE")
        for f in sorted(MIGRATIONS.glob("*.sql")):
            c.execute(_up_sql(f))
    from cove import db
    db.configure(url)
    yield url
    db.close()
