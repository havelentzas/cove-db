"""Messages pages: /messages (conversations), /messages/c/{id} (a thread), /messages/waiting, /messages/senders
(linking senders to People) and /messages/settings (collectors, ingest tokens, WhatsApp upload, agent
visibility, filters). Every read and write goes through cove.messages.service, so it's the same rules as MCP.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from cove import brand, fabric
from cove.auth import policy, tokens
from cove.crm.service import Invalid, NotFound, LOCAL_TZ
from cove.messages import service as messages
from cove.web import hub
from cove.web.api_v1 import service_on

router = APIRouter(tags=["messages"], dependencies=[service_on("messages")])


def _svc() -> dict:
    return brand.service("messages")


def _pages():
    from cove.web.app import pages
    return pages


def _actor(request: Request) -> str:
    from cove.web.app import actor
    return actor(request)


def _render(request: Request, name: str, status: int = 200, **ctx):
    return _pages().TemplateResponse(request, name, {"svc": _svc(), "can_write": policy.allows("messages:write"),
                                                      **ctx}, status_code=status)


def _home_stats() -> list:
    s = messages.stats()
    return [(s["conversations"], "conversations"), (s["messages"], "messages"), (s["to_confirm"], "to confirm")]


hub.STATS["messages"] = _home_stats


# ---------------------------------------------------------------- conversations
@router.get("/messages", response_class=HTMLResponse)
def conversations(request: Request, q: str = "", person: str = "", platform: str = "", since: str = "",
                  unresolved: bool = False, archived: bool = False, cursor: str = ""):
    try:
        convs = messages.list_conversations(person or None, q or None, platform or None, since or None, archived,
                                            unresolved, cursor or None, 50)
    except Invalid as e:
        convs, error = {"items": [], "next_cursor": None, "coverage": fabric.coverage()}, str(e)
    else:
        error = None
    hits = messages.search_messages(q, limit=20) if q.strip() else None
    return _render(request, "messages_list.html", convs=convs, hits=hits, q=q, person=person, platform=platform,
                   since=since, unresolved=unresolved, archived=archived, error=error, stats=messages.stats(),
                   platforms=("imessage", "sms", "rcs", "whatsapp"))


@router.get("/messages/c/{conversation_id}", response_class=HTMLResponse)
def thread(request: Request, conversation_id: str, around: str = "", date: str = "", before: int = 80):
    pivot = around or (date if date else None)
    try:
        t = messages.get_conversation(conversation_id, None, pivot, min(before, 100), 60 if pivot else 0)
    except NotFound:
        return HTMLResponse("Conversation not found", status_code=404)
    days, last = [], None
    for m in t["messages"]:
        day = datetime.fromisoformat(m["sent_at"]).astimezone(LOCAL_TZ).date()
        if day != last:
            days.append({"day": day, "messages": []})
            last = day
        days[-1]["messages"].append(m)
    return _render(request, "messages_thread.html", t=t, days=days, date=date, around=around)


@router.post("/messages/c/{conversation_id}/agents")
def agents_toggle(request: Request, conversation_id: str, visible: bool = Form(...), back: str = Form("")):
    messages.set_conversation_agent_visible(conversation_id, visible, _actor(request))
    return RedirectResponse(back if back.startswith("/messages") else f"/messages/c/{conversation_id}", status_code=303)


@router.post("/messages/c/{conversation_id}/archive")
def archive(request: Request, conversation_id: str, archived: bool = Form(True)):
    messages.archive_conversation(conversation_id, _actor(request), archived)
    return RedirectResponse(f"/messages/c/{conversation_id}", status_code=303)


@router.get("/messages/waiting", response_class=HTMLResponse)
def waiting(request: Request, days: int = 14):
    return _render(request, "messages_waiting.html", res=messages.awaiting_reply(days, 100), days=days)


# ---------------------------------------------------------------- senders (linking to People)
def _contacts() -> list[dict]:
    """People to link to, if People is there and readable; otherwise none (the page still works)."""
    if fabric.state("people") != "enabled" or not policy.allows("people:read"):
        return []
    from cove.crm import service as people
    return people.search_contacts(limit=2000)


@router.get("/messages/senders", response_class=HTMLResponse)
def senders(request: Request, state: str = "suggested", q: str = ""):
    res = messages.list_handles(state or None, q or None, 300)
    return _render(request, "messages_senders.html", res=res, state=state, q=q, contacts=_contacts(),
                   counts=messages.stats())


def _contact_ref(value: str) -> str:
    """A contact id/ref as is; a name typed into the box becomes the one contact with exactly that name."""
    import uuid as _uuid
    if not value or fabric.parse_ref(value):
        return value
    try:
        return str(_uuid.UUID(value))
    except ValueError:
        pass
    exact = [c for c in _contacts() if c["display_name"].lower() == value.lower()]
    if len(exact) != 1:
        raise Invalid(f"pick someone from the list ({'nobody' if not exact else 'several people'} is called {value!r})")
    return exact[0]["id"]


@router.post("/messages/senders/{handle_id}")
def sender_action(request: Request, handle_id: str, action: str = Form(...), contact: str = Form(""),
                  state: str = Form("suggested")):
    who = _actor(request)
    contact = _contact_ref(contact.strip()) if action == "link" else contact
    if action == "link" and contact:
        messages.link_handle(handle_id, contact, who)
    elif action == "dismiss" and contact:
        messages.dismiss_suggestion(handle_id, contact, who)
    elif action == "unlink":
        messages.unlink_handle(handle_id, who)
    elif action in ("me", "not-me"):
        messages.set_my_handle(handle_id, action == "me", who)
    else:
        raise Invalid("unknown action")
    return RedirectResponse(f"/messages/senders?state={state}", status_code=303)


@router.post("/messages/senders/rematch")
def rematch(request: Request, rebuild: bool = Form(False)):
    messages.resolve_handles(_actor(request), rebuild)
    return RedirectResponse("/messages/senders?state=suggested", status_code=303)


# ---------------------------------------------------------------- settings
def _settings_page(request: Request, status: int = 200, **extra):
    hidden = messages.list_conversations(include_archived=True, limit=200)
    return _render(request, "messages_settings.html", status=status, sources=messages.list_sources(),
                   tokens=[t for t in tokens.list_all("ingest")], settings=messages.settings(),
                   hidden=[c for c in hidden["items"] if not c["agent_visible"]],
                   recent=[c for c in hidden["items"] if c["agent_visible"]][:30],
                   owner=request.state.workspace and request.state.workspace["role"] == "owner",
                   can_ingest=policy.allows("messages:ingest"), **extra)


@router.get("/messages/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return _settings_page(request)


@router.post("/messages/settings", response_class=HTMLResponse)
def save_settings(request: Request, otp_filter: bool = Form(False), default_region: str = Form(""),
                  whatsapp_name: str = Form("")):
    from cove.fabric import services
    try:
        messages.update_settings(_actor(request), otp_filter=otp_filter, default_region=default_region)
        services.update_settings("messages", {"whatsapp_name": whatsapp_name.strip() or None}, _actor(request))
    except (Invalid, policy.Forbidden) as e:
        return _settings_page(request, 422, error=str(e))
    return RedirectResponse("/messages/settings", status_code=303)


@router.post("/messages/settings/tokens", response_class=HTMLResponse)
def create_token(request: Request, name: str = Form(...)):
    try:
        new = tokens.create(name, _actor(request), kind="ingest")
    except (Invalid, policy.Forbidden) as e:
        return _settings_page(request, 422, error=str(e))
    return _settings_page(request, new_token=new)


@router.post("/messages/settings/tokens/{token_id}/revoke")
def revoke_token(request: Request, token_id: int):
    tokens.revoke(token_id, _actor(request))
    return RedirectResponse("/messages/settings", status_code=303)


@router.post("/messages/import/whatsapp", response_class=HTMLResponse)
async def import_whatsapp(request: Request, file: UploadFile, my_name: str = Form(""), title: str = Form("")):
    from starlette.concurrency import run_in_threadpool
    from cove.messages.importers import whatsapp
    data = await file.read()
    try:
        result = await run_in_threadpool(whatsapp.import_export, file.filename or "chat.txt", data, _actor(request),
                                         my_name.strip() or None, title.strip() or None)
    except (Invalid, policy.Forbidden) as e:
        return _settings_page(request, 422, error=f"Couldn't import that file: {e}")
    return _settings_page(request, imported=result)
