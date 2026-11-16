"""/settings/services: turn services on or off in this workspace (owners), and see how they connect.

Turning a service off hides its pages, MCP tools and capabilities; its data is kept (cove.fabric.services).
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cove import brand, fabric
from cove.fabric import events, services

router = APIRouter(tags=["core"])


@router.get("/settings/services", response_class=HTMLResponse)
def services_page(request: Request):
    from cove.web.app import pages
    from cove.web.hub import CORE
    names = {s["key"]: s for s in brand.registry()["services"]}
    caps = {s["service"]: s for s in fabric.describe()}
    rows = [{**r, "brand": names.get(r["key"], {"key": r["key"], "name": r["key"].title()}),
             "fabric": caps.get(r["key"], {})} for r in services.list_services()]
    return pages.TemplateResponse(request, "services.html", {
        "svc": CORE, "rows": rows, "cursors": events.cursors(),
        "owner": request.state.workspace and request.state.workspace["role"] == "owner"})


@router.post("/settings/services/{key}")
def toggle(request: Request, key: str, enabled: bool = Form(...)):
    from cove.web.app import actor
    services.set_enabled(key, enabled, actor(request))
    return RedirectResponse("/settings/services", status_code=303)
