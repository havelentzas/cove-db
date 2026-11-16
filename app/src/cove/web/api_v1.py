"""The JSON API, /api/v1/<service>/… (docs/fabric.md › HTTP API), and the fabric endpoints.

Authentication is done by cove.web.auth (personal token or session); authorization by the service layer's
scopes, exactly as for the web UI and MCP. Every object carries its `ref`; errors use one envelope,
{"error": {"code", "message"}}. Lists page with an opaque `cursor` and `limit`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.openapi.utils import get_openapi

from cove import cursors, fabric
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


encode_cursor, decode_cursor = cursors.encode, cursors.decode


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


# ---------------------------------------------------------------- People
people_router = APIRouter(prefix=f"{PREFIX}/people", tags=["people"], dependencies=[service_on("people")])


def _contact(row: dict) -> dict:
    return {"ref": fabric.ref("people", "contact", row["id"]), **row}


@people_router.get("/contacts")
def list_contacts(q: str | None = None, tag: str | None = None, since: str | None = None,
                  cursor: str | None = None, limit: int = Query(100, ge=1, le=500),
                  include_archived: bool = False) -> dict:
    """Contacts. `q`/`tag` search (one page, best matches first); `since` is a change feed (updated at or
    after, oldest first, archived included); otherwise alphabetical. Page with `cursor`."""
    from cove.crm import service as svc
    if q or tag:
        return {"items": [_contact(r) for r in svc.search_contacts(q, tag, False, include_archived, limit)],
                "next_cursor": None}
    res = svc.list_contacts(since, cursor, limit, include_archived)
    return {"items": [_contact(r) for r in res["items"]], "next_cursor": res["next_cursor"]}


@people_router.get("/contacts/{contact_id}")
def get_contact(contact_id: str, interactions: int = Query(10, ge=0, le=200)) -> dict:
    """One contact: JSContact card, version, circles, relationships, recent conversations, rhythm."""
    from cove.crm import service as svc
    return _contact(svc.get_contact(contact_id, interactions=interactions))


@people_router.get("/interactions")
def list_interactions(contact: str | None = None, since: str | None = None, until: str | None = None,
                      cursor: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict:
    """Logged conversations, newest first, optionally for one contact."""
    from cove.crm import fabric as people
    subject = fabric.ref("people", "contact", contact) if contact else None
    from cove.auth import policy
    policy.require("people:read")
    return people.timeline(subject, since, until, limit, cursor)


@people_router.get("/follow-ups")
def follow_ups(limit: int = Query(50, ge=1, le=500)) -> dict:
    """Who you're due to reach out to, most overdue first."""
    from cove.crm import service as svc
    return {"items": [_contact(r) for r in svc.follow_ups_due(limit)], "next_cursor": None}


# ---------------------------------------------------------------- Notes
notes_router = APIRouter(prefix=f"{PREFIX}/notes", tags=["notes"], dependencies=[service_on("notes")])


def _file(row: dict) -> dict:
    return {"ref": fabric.ref("notes", "file", row["id"]), **row}


@notes_router.get("/files")
def list_files(q: str | None = None, tag: str | None = None, folder: str | None = None,
               since: int | None = Query(None, ge=0), cursor: str | None = None,
               limit: int = Query(100, ge=1, le=500)) -> dict:
    """Notes. `since` is the sync change feed (a `seq`, tombstones included); otherwise search by `q`, `tag`
    or `folder`, newest first."""
    from cove.notes import service as notes
    if since is not None or cursor:
        start = int(decode_cursor(cursor, 1)[0]) if cursor else since
        res = notes.changes(start, limit)
        return {"items": [_file(r) for r in res["changes"]],
                "next_cursor": encode_cursor(res["last_seq"]) if res["more"] else None, "head_seq": res["head_seq"]}
    return {"items": [_file(r) for r in notes.search(q, tag, folder, limit)], "next_cursor": None}


@notes_router.get("/files/{file_id}")
def get_file(file_id: str) -> dict:
    """One note by id, with its Markdown `content`, backlinks and version."""
    from cove.notes import service as notes
    f = notes.get(file_id)
    return _file({**f, "backlinks": notes.backlinks(f["id"])})
