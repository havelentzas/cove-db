"""Who is acting, and in which workspace, for the current request or tool call.

Set once per request by the web auth middleware (signed-in user or device token) and per process by the
MCP server; read by `cove.db` to choose the workspace database. Nothing chooses a workspace on its own: if
no principal is set, database access fails closed.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


class NoWorkspace(RuntimeError):
    """Database access was attempted without a workspace (a bug, or an unbound client)."""


@dataclass(frozen=True)
class Workspace:
    id: str
    slug: str
    name: str
    db_name: str
    role_name: str         # ws_<id>; the read-only role is f"{role_name}_ro"


@dataclass(frozen=True)
class Principal:
    actor: str             # audit label: web:<username>, device:<name>, mcp:<client>
    workspace: Workspace
    user_id: str | None = None
    role: str = "owner"    # membership role (owner / editor / viewer); enforced from Phase 4
    scopes: frozenset[str] = field(default_factory=frozenset)


_current: ContextVar[Principal | None] = ContextVar("cove_principal", default=None)
_process_default: Principal | None = None   # MCP server only (until it has per-token principals)


def current() -> Principal | None:
    return _current.get() or _process_default


def require() -> Principal:
    p = current()
    if p is None:
        raise NoWorkspace("no workspace selected for this request")
    return p


def set_current(principal: Principal | None):
    return _current.set(principal)


@contextmanager
def use(principal: Principal) -> Iterator[Principal]:
    token = _current.set(principal)
    try:
        yield principal
    finally:
        _current.reset(token)


def set_process_default(principal: Principal | None) -> None:
    global _process_default
    _process_default = principal
