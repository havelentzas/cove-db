"""/settings/agents: personal access tokens for agents and scripts (MCP, Claude Code).

One token per agent, so its changes are labelled with its name in history and it can be revoked on its
own. New tokens are read-only plus the agent's own memory unless you tick more; a token can never have
more than you do (cove.auth.tokens / cove.auth.policy).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cove.auth import policy, tokens
from cove.crm.service import Invalid

router = APIRouter(tags=["core"])


def _page(request: Request, *, new=None, error=None, status=200):
    from cove.web.app import pages
    from cove.web.hub import CORE, mcp_urls
    p = request.state
    return pages.TemplateResponse(request, "agents.html", {
        "svc": CORE, "tokens": tokens.list_all("personal"), "new": new, "error": error,
        "scopes": policy.SCOPES, "defaults": policy.DEFAULT_AGENT_SCOPES, "kind_only": policy.KIND_ONLY_SCOPES,
        "mine": policy.effective(p.workspace["role"], None), "mcp_url": mcp_urls()[-1],
    }, status_code=status)


@router.get("/settings/agents", response_class=HTMLResponse)
def agents(request: Request):
    return _page(request)


@router.post("/settings/agents", response_class=HTMLResponse)
async def create(request: Request):
    from cove.web.app import actor
    form = await request.form()
    days = str(form.get("expires_days") or "").strip()
    try:
        new = tokens.create(str(form.get("name", "")), actor(request), scopes=form.getlist("scope"),
                            kind="personal", expires_days=int(days) if days.isdigit() else None)
    except (Invalid, policy.Forbidden) as e:
        return _page(request, error=str(e), status=422)
    return _page(request, new=new)


@router.post("/settings/agents/{token_id}/revoke")
def revoke(request: Request, token_id: int):
    from cove.web.app import actor
    tokens.revoke(token_id, actor(request))
    return RedirectResponse("/settings/agents", status_code=303)
