"""Connection pools. ``COVE_DATABASE_URL`` (read/write app role), ``COVE_READONLY_URL`` (read-only role
used for ad-hoc SQL) and ``COVE_CONTROL_DATABASE_URL`` (cove_control: users, sessions, security events).
Without the env vars, URLs are built from Docker secrets."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pools: dict[str, ConnectionPool] = {}
_urls: dict[str, str | None] = {}


ENV = {"rw": "COVE_DATABASE_URL", "ro": "COVE_READONLY_URL", "ctl": "COVE_CONTROL_DATABASE_URL"}


def configure(url: str, readonly_url: str | None = None, control_url: str | None = None) -> None:
    """Point the module at specific databases (used by tests; control tables then share `url`)."""
    close()
    _urls["rw"] = url
    _urls["ro"] = readonly_url or url
    _urls["ctl"] = control_url or url


def _pool(kind: str) -> ConnectionPool:
    if kind not in _pools:
        url = _urls.get(kind) or os.environ.get(ENV[kind])
        if not url:   # normal deployment: build it from Docker secrets
            from cove import secrets
            url = secrets.database_url("cove_readonly" if kind == "ro" else "cove_app",
                                       "cove_control" if kind == "ctl" else None)
        if not url:
            raise RuntimeError(f"database URL for {kind!r} is not configured")
        # check=… validates each connection on checkout, so a Postgres restart never surfaces as a 500
        _pools[kind] = ConnectionPool(url, min_size=1, max_size=5, kwargs={"row_factory": dict_row}, open=True,
                                      check=ConnectionPool.check_connection)
    return _pools[kind]


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """A pooled read/write connection; commits on success, rolls back on error."""
    with _pool("rw").connection() as c:
        yield c


@contextmanager
def ro_conn() -> Iterator[psycopg.Connection]:
    with _pool("ro").connection() as c:
        yield c


@contextmanager
def ctl_conn() -> Iterator[psycopg.Connection]:
    """A pooled connection to cove_control (identity and sessions); commits on success."""
    with _pool("ctl").connection() as c:
        yield c


def close() -> None:
    for p in _pools.values():
        p.close()
    _pools.clear()
