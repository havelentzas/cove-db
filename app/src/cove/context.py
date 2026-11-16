"""Who is acting, and in which workspace, for the current request or tool call.

Set once per request by the web auth middleware (signed-in user or device token) and by the MCP server's
token middleware; read by `cove.db` to choose the workspace database and by `cove.auth.policy`. Nothing chooses a workspace on its own: if
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
    role: str = "owner"    # membership role (owner / editor / viewer)
    scopes: frozenset[str] = field(default_factory=frozenset)   # effective: role ∩ token (cove.auth.policy)
    agent: bool = False    # MCP or personal-token API caller: hidden and sensitive data is left out for it


_current: ContextVar[Principal | None] = ContextVar("cove_principal", default=None)


def current() -> Principal | None:
    return _current.get()


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

