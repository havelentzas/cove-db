"""cove home page: every service, with its MCP tools and web routes.

The catalog is built from the running code, never hand-maintained:
- services come from brand/services.json
- MCP tools come from the MCP server's registry (each tool carries meta={"service": ...})
- web routes come from FastAPI routers tagged with the service key
It is also served as JSON at /api/services for agents and scripts.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from cove import brand
from cove.crm import service as svc

router = APIRouter(tags=["core"])

CORE = {"key": "", "name": "cove", "status": "live", "path": "/",
        "blurb": "System tools shared by every service: orientation, agent memory and read-only SQL."}
CORE_TOOLS = {"describe_system", "read_notes", "write_note", "sql_query"}


def mcp_urls() -> list[str]:
    raw = os.environ.get("COVE_MCP_URLS", "http://127.0.0.1:8765/mcp")
    return [u.strip() for u in raw.split(",") if u.strip()]


async def _mcp_tools() -> list[dict]:
    from cove.mcp_server import TOOL_SCOPES, mcp  # importing only registers tools; it does not start a server
    out = []
    for t in await mcp.all_tools():
        meta = getattr(t, "meta", None) or {}
        service = meta.get("service") or ("core" if t.name in CORE_TOOLS else "people")
        schema = t.input_schema or {}
        required = set(schema.get("required", []))
        params = [{"name": n, "required": n in required} for n in (schema.get("properties") or {}) if n != "ctx"]
        ann = t.annotations
        out.append({
            "name": t.name,
            "service": service,
            "summary": (t.description or "").strip().split("\n\n")[0].replace("\n", " "),
            "params": params,
            "read_only": bool(ann and ann.read_only_hint),
            "scope": TOOL_SCOPES.get(t.name),
            "destructive": bool(ann and ann.destructive_hint),
        })
    return out


def _walk(routes, inherited: list[str] = ()):
    """Yield (route, tags), descending into included routers. Newer FastAPI keeps them as
    wrappers with `original_router` instead of flattening; older versions flatten."""
    for r in routes:
        sub = getattr(r, "original_router", None)
        if sub is not None:
            yield from _walk(sub.routes, [*inherited, *(getattr(sub, "tags", None) or [])])
        else:
            yield r, [*inherited, *(getattr(r, "tags", None) or [])]


def _routes(request: Request) -> list[dict]:
    out = []
    for r, tags in _walk(request.app.routes):
        methods = sorted(m for m in (getattr(r, "methods", None) or []) if m not in ("HEAD", "OPTIONS"))
        if tags and methods:
            out.append({"service": "core" if "core" in tags else tags[0], "methods": methods, "path": r.path,
                        "name": (r.name or "").replace("_", " ")})
    return sorted(out, key=lambda x: (x["path"], x["methods"]))


async def catalog(request: Request) -> dict:
    tools, routes = await _mcp_tools(), _routes(request)
    services = []
    for s in [CORE, *brand.registry()["services"]]:
        key = s["key"] or "core"
        services.append({**s, "id": key,
                         "mcp_tools": [t for t in tools if t["service"] == key],
                         "web_routes": [r for r in routes if r["service"] == key]})
    return {"mcp": {"transport": "streamable-http", "urls": mcp_urls()}, "services": services}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    from cove.web.app import pages  # shared Jinja environment
    cat = await catalog(request)
    counts = svc.describe_system()["counts"]
    from cove.notes import service as notes
    ns = notes.stats()
    stats = {"people": [(counts["active_contacts"], "people"), (counts["follow_ups_due"], "to reach out"),
                        (counts["interactions"], "conversations")],
             "notes": [(ns["notes"], "notes"), (ns["attachments"], "attachments"), (ns["devices"], "devices")]}
    return pages.TemplateResponse(request, "hub.html", {"cat": cat, "stats": stats, "svc": CORE})


@router.get("/api/services")
async def services_json(request: Request) -> dict:
    """Machine-readable catalog of services, MCP tools and web routes."""
    return await catalog(request)
