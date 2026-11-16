"""The JSON API, /api/v1/<service>/… (docs/fabric.md › HTTP API), and the fabric endpoints.

Authentication is done by cove.web.auth (personal token or session); authorization by the service layer's
scopes, exactly as for the web UI and MCP. Every object carries its `ref`; errors use one envelope,
{"error": {"code", "message"}}. Lists page with an opaque `cursor` and `limit`.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.openapi.utils import get_openapi

from cove import fabric
from cove.crm.service import Invalid
from cove.fabric import aggregate
from cove.fabric import events as fabric_events

PREFIX = "/api/v1"


# ---------------------------------------------------------------- shared helpers
class ServiceOff(Exception):
    """A request reached a service that is turned off (or not installed) in this workspace."""

    def __init__(self, key: str, state: str):
        super().__init__(f"{key} is {state.replace('_', ' ')} in this workspace")
        self.key, self.state = key, state


def service_on(key: str):
    """Router dependency: the service must be enabled in the current workspace."""
    def check(request: Request) -> None:
        st = fabric.state(key)
        if st != "enabled":
            raise ServiceOff(key, st)
    return Depends(check)


def encode_cursor(*values: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, default=str).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, n: int) -> list | None:
    if not cursor:
        return None
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except ValueError:
        raise Invalid("bad cursor") from None
    if not isinstance(values, list) or len(values) != n:
        raise Invalid("bad cursor")
    return values


def page(items: list[dict], limit: int, key) -> dict:
    """Trim a limit+1 fetch to `limit` and build the next cursor from the last item's sort key."""
    more = len(items) > limit
    items = items[:limit]
    return {"items": items, "next_cursor": encode_cursor(*key(items[-1])) if more and items else None}


# ---------------------------------------------------------------- fabric endpoints
fabric_router = APIRouter(prefix=f"{PREFIX}/fabric", tags=["core"])


@fabric_router.get("/capabilities")
def capabilities() -> dict:
    """What each service provides and consumes, and what is available to you right now."""
    return {"services": fabric.describe()}


@fabric_router.get("/context")
def context_endpoint(ref: str | None = None, q: str | None = None, since: str | None = None,
                     until: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict:
    """Everything about a person (or any ref), across services, with coverage. Pass `ref`, or `q` (a name,
    phone number or email)."""
    if not (ref or q):
        raise Invalid("pass ref=cove:… or q=<name, phone or email>")
    return aggregate.context_for(ref or q, since, until, limit)


@fabric_router.get("/search")
def search(q: str = Query(..., min_length=1, max_length=500), limit: int = Query(20, ge=1, le=100)) -> dict:
    """Search every service at once, with coverage."""
    return aggregate.search_everything(q, limit)


@fabric_router.get("/events")
def events(since: int = Query(0, ge=0), types: str = "", limit: int = Query(500, ge=1, le=1000)) -> dict:
    """Change events after `since` from services you can read (comma-separated `types` to filter)."""
    return fabric_events.events(since, [t.strip() for t in types.split(",") if t.strip()], limit)


# ---------------------------------------------------------------- OpenAPI for /api/v1 only
meta_router = APIRouter(prefix=PREFIX, tags=["core"])


@meta_router.get("/openapi.json", include_in_schema=False)
def openapi(request: Request) -> dict:
    from cove.web.hub import _walk
    routes = [r for r, _ in _walk(request.app.routes)
              if getattr(r, "path", "").startswith(PREFIX + "/") and getattr(r, "include_in_schema", False)]
    doc = get_openapi(title="cove API", version="1",
                      description="cove's JSON API. Auth: Authorization: Bearer cove_pat_… (or a signed-in "
                                  "session). Errors: {\"error\": {\"code\", \"message\"}}. See docs/fabric.md.",
                      routes=routes)
    doc["components"] = {**doc.get("components", {}), "securitySchemes": {
        "bearer": {"type": "http", "scheme": "bearer", "description": "cove_pat_… personal access token"}}}
    doc["security"] = [{"bearer": []}]
    return doc
