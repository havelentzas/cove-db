"""Secrets from Docker secrets (/run/secrets/<name>), with env-var fallbacks for tests and dev.

Never log or return these values.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("COVE_SECRETS_DIR", "/run/secrets"))


@lru_cache(maxsize=None)
def get(name: str, required: bool = True) -> str | None:
    """Read secret `name` from $COVE_SECRETS_DIR/name, else env COVE_SECRET_<NAME>."""
    p = SECRETS_DIR / name
    if p.is_file():
        value = p.read_text().strip()
        if value:
            return value
    value = os.environ.get(f"COVE_SECRET_{name.upper()}")
    if value:
        return value
    if required:
        raise RuntimeError(f"secret {name!r} is not configured (expected {p} or COVE_SECRET_{name.upper()})")
    return None


def database_url(role: str, database: str | None = None) -> str:
    """postgresql:// URL for one of cove's database roles, built from secrets."""
    host = os.environ.get("COVE_DB_HOST", "postgres")
    database = database or os.environ.get("COVE_DB_NAME", "cove")
    password = get({"cove_app": "app_password", "cove_readonly": "readonly_password",
                    "cove_owner": "postgres_password"}[role])
    return f"postgresql://{role}:{password}@{host}:5432/{database}"
