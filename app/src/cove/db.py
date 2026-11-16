"""Connection pools. ``COVE_DATABASE_URL`` (read/write app role) and
``COVE_READONLY_URL`` (read-only role used for ad-hoc SQL)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pools: dict[str, ConnectionPool] = {}
_urls: dict[str, str | None] = {}


def configure(url: str, readonly_url: str | None = None) -> None:
    """Point the module at specific databases (used by tests)."""
    close()
    _urls["rw"] = url
    _urls["ro"] = readonly_url or url


def _pool(kind: str) -> ConnectionPool:
    if kind not in _pools:
        url = _urls.get(kind) or os.environ.get("COVE_DATABASE_URL" if kind == "rw" else "COVE_READONLY_URL")
        if not url:
            raise RuntimeError(f"database URL for {kind!r} is not configured")
        _pools[kind] = ConnectionPool(url, min_size=1, max_size=5, kwargs={"row_factory": dict_row}, open=True)
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


def close() -> None:
    for p in _pools.values():
        p.close()
    _pools.clear()
