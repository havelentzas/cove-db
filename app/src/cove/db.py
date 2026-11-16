"""Database connections.

- `conn()` / `ro_conn()`: the **current workspace's** database (see cove.context). Connections log in as
  `cove_app` / `cove_readonly`, which hold no table privileges themselves, and immediately
  `SET ROLE ws_<id>` / `ws_<id>_ro`. Those roles have privileges only inside their own workspace
  database, and the login roles can only *switch* into them (membership WITH INHERIT FALSE). So a query in
  workspace A's connection can't read workspace B, even through a bug, and ad-hoc read-only SQL can't
  climb to write access.
- `ctl_conn()`: cove_control (users, sessions, workspaces, tokens, security events).

Pools are created lazily per workspace and the least recently used ones are closed beyond MAX_POOLS.
Tests call `configure(url)`: every connection then goes to that one database, without SET ROLE.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from cove import context

MAX_POOLS = 24
_pools: "OrderedDict[tuple, ConnectionPool]" = OrderedDict()
_lock = threading.Lock()
_test_urls: dict[str, str] = {}


def configure(url: str, readonly_url: str | None = None, control_url: str | None = None) -> None:
    """Test mode: send every connection to `url` (control tables share it unless `control_url` is given)."""
    close()
    _test_urls.update(rw=url, ro=readonly_url or url, ctl=control_url or url)


def test_mode() -> bool:
    return bool(_test_urls)


def _url(login_role: str, database: str) -> str:
    from cove import secrets
    return secrets.database_url(login_role, database)


def _make_pool(key: tuple) -> ConnectionPool:
    kind, database, set_role = key
    if _test_urls:
        url = _test_urls[kind]
    elif kind == "ctl":
        url = os.environ.get("COVE_CONTROL_DATABASE_URL") or _url("cove_app", "cove_control")
    else:
        url = _url("cove_app" if kind == "rw" else "cove_readonly", database)

    def configure_conn(c: psycopg.Connection) -> None:
        if set_role:
            c.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(set_role)))
        if kind == "ro":
            c.execute("SET default_transaction_read_only = on")
            c.execute("SET statement_timeout = '5s'")
        c.commit()

    # check=… validates each connection on checkout, so a Postgres restart never surfaces as a 500
    return ConnectionPool(url, min_size=1 if kind == "ctl" else 0, max_size=5, open=True,
                          kwargs={"row_factory": dict_row}, configure=configure_conn,
                          check=ConnectionPool.check_connection, name=f"{kind}:{database}")


def _pool(key: tuple) -> ConnectionPool:
    with _lock:
        pool = _pools.get(key)
        if pool is None:
            pool = _pools[key] = _make_pool(key)
            while len(_pools) > MAX_POOLS:   # close the least recently used pool, letting in-flight work finish
                _, old = _pools.popitem(last=False)
                threading.Thread(target=old.close, kwargs={"timeout": 30}, daemon=True).start()
        else:
            _pools.move_to_end(key)
        return pool


def _workspace_key(kind: str) -> tuple:
    if _test_urls:
        return (kind, "test", None)
    ws = context.require().workspace
    return (kind, ws.db_name, ws.role_name + ("_ro" if kind == "ro" else ""))


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """A pooled read/write connection to the current workspace; commits on success, rolls back on error."""
    with _pool(_workspace_key("rw")).connection() as c:
        yield c


@contextmanager
def ro_conn() -> Iterator[psycopg.Connection]:
    """A read-only connection to the current workspace (ad-hoc SQL; 5 s statement timeout)."""
    with _pool(_workspace_key("ro")).connection() as c:
        yield c


@contextmanager
def ctl_conn() -> Iterator[psycopg.Connection]:
    """A pooled connection to cove_control (identity and sessions); commits on success."""
    with _pool(("ctl", "cove_control", None)).connection() as c:
        yield c


def close() -> None:
    with _lock:
        for p in _pools.values():
            p.close()
        _pools.clear()
    _test_urls.clear()
