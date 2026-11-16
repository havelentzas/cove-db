"""Authorization: what the current principal may do, decided in one place.

- **Workspace roles** (control.memberships.role): owner and editor may read and write; viewer may only read.
  (Managing members and deleting a workspace are owner-only CLI operations for now.)
- **Scopes** narrow it further for tokens: a token can never do more than its user's role allows.
  Effective scopes = role scopes ∩ token scopes (a browser session has no token, so it gets the role's).
- Every public function in the service modules is wrapped by `guard`, so the check happens at the one
  layer the web UI, the sync API and MCP all share; tests/test_policy.py fails if a function is left out.
"""

from __future__ import annotations

import functools
import inspect
import sys
from typing import Callable, Iterable

from cove import context

SCOPES = {
    "people:read": "Read contacts, conversations and circles",
    "people:write": "Add and change contacts, conversations and circles",
    "notes:read": "Read notes and attachments",
    "notes:write": "Create and edit notes",
    "notes:sync": "Two-way sync of the whole vault (the Obsidian plugin)",
    "system:memory": "Read and write the agent's own memory notes",
    "system:sql": "Run read-only SQL over everything in the workspace",
}
ALL = frozenset(SCOPES)
READ_ONLY = frozenset({"people:read", "notes:read"})
ROLE_SCOPES = {"owner": ALL, "editor": ALL, "viewer": READ_ONLY}
# What a new agent token gets unless you tick more: read everything, keep its own memory.
DEFAULT_AGENT_SCOPES = frozenset({"people:read", "notes:read", "system:memory"})
DEVICE_SCOPES = frozenset({"notes:read", "notes:write", "notes:sync"})
LEGACY = {"notes": DEVICE_SCOPES}   # scope names stored before Phase 4


class Forbidden(PermissionError):
    """The principal lacks a scope; the message is safe to show."""


def expand(scopes: Iterable[str]) -> frozenset[str]:
    out: set[str] = set()
    for s in scopes:
        out |= LEGACY.get(s, {s})
    return frozenset(out & ALL)


def effective(role: str, token_scopes: Iterable[str] | None) -> frozenset[str]:
    allowed = ROLE_SCOPES.get(role, frozenset())
    return allowed if token_scopes is None else allowed & expand(token_scopes)


def allows(scope: str | None, principal: context.Principal | None = None) -> bool:
    p = principal or context.current()
    if p is None:
        return False
    return scope is None or scope in p.scopes


def require(scope: str | None) -> context.Principal:
    p = context.require()
    if not allows(scope, p):
        raise Forbidden(f"this needs the '{scope}' permission" if scope else "not allowed")
    return p


def requires(scope: str | None) -> Callable:
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            require(scope)
            return fn(*args, **kwargs)
        inner.__cove_scope__ = scope
        return inner
    return wrap


PURE = "pure"   # helpers that touch no data (parsing, hashing); listed so the coverage test knows


def guard(module_name: str, table: dict[str | None, Iterable[str]]) -> None:
    """Wrap the named public functions of `module_name` with `requires(scope)`.

    Key None means "any principal in a workspace" (e.g. describe_system); key PURE marks functions that
    don't touch workspace data. Every public function must appear exactly once (checked by tests).
    """
    mod = sys.modules[module_name]
    seen: set[str] = set()
    for scope, names in table.items():
        for name in names:
            if name in seen:
                raise ValueError(f"{module_name}.{name} listed twice")
            seen.add(name)
            if scope == PURE:
                getattr(mod, name).__cove_scope__ = PURE
                continue
            setattr(mod, name, requires(scope)(getattr(mod, name)))
    mod.__cove_guarded__ = seen


def unguarded(module) -> list[str]:
    """Public functions defined in `module` that `guard` didn't cover."""
    return sorted(n for n, f in vars(module).items()
                  if inspect.isfunction(f) and not n.startswith("_") and f.__module__ == module.__name__
                  and n not in getattr(module, "__cove_guarded__", set()))
