"""cove web UI: FastAPI + Jinja2 + HTMX. All writes go through cove.crm.service."""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, FastAPI, Form, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from cove import brand, context
from cove.auth import policy
from cove.web import agents, api_v1, auth, hub, notes_api, notes_web, security, services_web
from cove.crm import service as svc
from cove.templating import FormError, form_to_record, load_template, record_to_form

HERE = Path(__file__).parent
TEMPLATE_NAME = "contact-default"

app = FastAPI(title="cove", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)   # the vault tree makes Notes pages large
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/brand", StaticFiles(directory=brand.BRAND_DIR), name="brand")
people = APIRouter(tags=["people"], dependencies=[api_v1.service_on("people")])   # every People page
pages = Jinja2Templates(directory=HERE / "templates")
pages.env.globals.update(INTERACTION_KINDS=svc.INTERACTION_KINDS, RELATION_TYPES=svc.RELATION_TYPES,
                         SERVICE=brand.service("people"), SERVICES=brand.registry()["services"],
                         MARK=brand.mark_svg())
pages.env.globals["csrf_field"] = pass_context(auth.csrf_field)


def _initials(name: str) -> str:
    parts = [p for p in (name or "").replace("@", " ").split() if p[:1].isalnum()]
    if not parts:
        return "·"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


pages.env.filters["initials"] = _initials


@pass_context
def _who(ctx, actor: str) -> str:
    """Friendly audit actor: your own 'web:<you>' -> 'you', 'web:<someone>' -> 'someone',
    'mcp:claude-code' -> 'agent (claude-code)'."""
    actor = actor or ""
    suffix = " · import" if actor.endswith(":import") else ""
    if actor.startswith("web"):
        name = actor.removesuffix(":import").partition(":")[2]
        request = ctx.get("request")
        me = getattr(getattr(request, "state", None), "user", None) if request is not None else None
        return ("you" if not name or (me and name == me["username"]) else name) + suffix
    if actor.startswith("mcp:"):
        return f"agent ({actor[4:]})"
    if actor.startswith("device:"):
        return f"device ({actor[7:]})"
    return actor


pages.env.filters["who"] = _who


def _local(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v))
    return dt.astimezone(svc.LOCAL_TZ) if dt.tzinfo else dt


def _short_dt(v):
    dt = _local(v)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _short_d(v):
    if v and len(str(v)) == 10:
        return str(v)
    dt = _local(v)
    return dt.strftime("%Y-%m-%d") if dt else ""


pages.env.filters["dt"] = _short_dt
pages.env.globals["now_iso"] = lambda: datetime.now(timezone.utc).isoformat()
pages.env.filters["d"] = _short_d


async def same_origin_posts(request: Request, call_next):
    """Reject cross-site form posts. A second line of defence next to the CSRF tokens checked in
    cove.web.auth (other web pages can't submit forms through your browser)."""
    bearer = request.url.path.startswith("/api/") and request.headers.get("authorization", "").startswith("Bearer ")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not bearer:   # token auth isn't CSRF-able
        origin = request.headers.get("origin") or request.headers.get("referer")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if origin and urlparse(origin).netloc != host:
            return PlainTextResponse("cross-origin request blocked", status_code=403)
    return await call_next(request)


# Registration order matters: the last one added runs first. Requests pass through
# security (size, rate limits, headers) → same-origin check → sign-in + CSRF → the route.
app.middleware("http")(auth.middleware)
app.middleware("http")(same_origin_posts)
app.middleware("http")(security.middleware)


def actor(request: Request) -> str:
    """Audit actor for a web request: web:<username of the signed-in user>."""
    user = getattr(request.state, "user", None)
    return f"web:{user['username']}" if user else "web"


def back(url: str, **params) -> RedirectResponse:
    q = {k: v for k, v in params.items() if v}
    return RedirectResponse(url + ("?" + urlencode(q) if q else ""), status_code=303)


def _template() -> dict:
    return load_template(TEMPLATE_NAME)


def _submitted_values(template: dict, form) -> dict:
    """Rebuild render values from a failed submission so the user keeps their input."""
    fields, repeats = {}, {}
    for section in template["sections"]:
        rep = section.get("repeat")
        if not rep:
            for f in section["fields"]:
                name = f"f__{f['key']}"
                fields[f["key"]] = form.getlist(name) if f.get("type") == "set" else form.get(name, "")
            continue
        prefix = f"r__{rep}__"
        idx = sorted({int(k[len(prefix):].split("__")[0]) for k in form.keys()
                      if k.startswith(prefix) and k[len(prefix):].split("__")[0].isdigit()})
        rows = []
        for i in idx:
            row = {"_id": form.get(f"{prefix}{i}___id", "")}
            for f in section["fields"]:
                name = f"{prefix}{i}__{f['key']}"
                row[f["key"]] = form.getlist(name) if f.get("type") == "set" else form.get(name, "")
            rows.append(row)
        repeats[rep] = rows
    return {"fields": fields, "repeats": repeats}


def _detail_sections(template: dict, card: dict) -> list[dict]:
    """Card fields to display, labelled by the template."""
    values = record_to_form(template, card)
    out = []
    for section in template["sections"]:
        items = []
        if rep := section.get("repeat"):
            for row in values["repeats"].get(rep, []):
                main = [row[f["key"]] for f in section["fields"] if f.get("type") != "set" and row[f["key"]]]
                extra = [v for f in section["fields"] if f.get("type") == "set" for v in row[f["key"]]]
                if main:
                    items.append({"value": ", ".join(main) if section["repeat"] == "addresses" else main[0],
                                  "extra": main[1:] if section["repeat"] != "addresses" else [], "tags": extra,
                                  "kind": section["fields"][0].get("type")})
        else:
            for f in section["fields"]:
                if values["fields"].get(f["key"]):
                    items.append({"label": f["label"], "value": values["fields"][f["key"]], "kind": f.get("type")})
        if items:
            out.append({"title": section["title"], "items": items, "repeat": bool(section.get("repeat"))})
    return out


# ================================================================ list
@people.get("/people", response_class=HTMLResponse)
def index(request: Request, q: str = "", tag: str = "", due: bool = False, archived: bool = False):
    rows = svc.search_contacts(q or None, tag or None, due, archived, limit=500)
    ctx = {"rows": rows, "q": q, "tag": tag, "due": due, "archived": archived}
    if request.headers.get("hx-request") and request.headers.get("hx-target") == "rows":
        return pages.TemplateResponse(request, "_rows.html", ctx)
    ctx.update(tags=svc.list_tags(), due_list=svc.follow_ups_due(10))
    return pages.TemplateResponse(request, "index.html", ctx)


# ================================================================ contact form
def _form_page(request: Request, *, contact=None, values=None, errors=None, status=200, tags_text=""):
    t = _template()
    if values is None:
        values = record_to_form(t, contact["card"] if contact else {})
    return pages.TemplateResponse(request, "form.html", {
        "t": t, "values": values, "contact": contact, "errors": errors or [], "tags_text": tags_text,
    }, status_code=status)


@people.get("/contacts/new", response_class=HTMLResponse)
def new_contact(request: Request):
    return _form_page(request)


@people.post("/contacts/new")
async def create_contact(request: Request):
    form = await request.form()
    t = _template()
    try:
        card = form_to_record(t, form, {"@type": "Card", "version": "1.0", "kind": "individual"})
        tags = [x.strip() for x in str(form.get("tags", "")).split(",") if x.strip()]
        if tags:
            card["keywords"] = {x: True for x in tags}
        c = svc.create_contact(card, actor(request))
    except (FormError, svc.Invalid) as e:
        errs = e.errors if isinstance(e, FormError) else [str(e)]
        return _form_page(request, values=_submitted_values(t, form), errors=errs, status=422,
                          tags_text=form.get("tags", ""))
    return back(f"/contacts/{c['id']}")


@people.get("/contacts/{cid}/edit", response_class=HTMLResponse)
def edit_contact(request: Request, cid: str):
    return _form_page(request, contact=svc.get_contact(cid, interactions=0))


@people.post("/contacts/{cid}/edit")
async def save_contact(request: Request, cid: str):
    form = await request.form()
    t = _template()
    current = svc.get_contact(cid, interactions=0)
    try:
        version = int(form.get("version", "0"))
        # Rebuild from the version the user was editing; the version check catches concurrent edits.
        card = form_to_record(t, form, current["card"])
        svc.replace_card(cid, card, version, actor(request))
    except (FormError, svc.Invalid, svc.Conflict) as e:
        errs = e.errors if isinstance(e, FormError) else [str(e)]
        if isinstance(e, svc.Conflict):
            errs = ["Someone (or an agent) changed this contact while you were editing. "
                    "Your changes are shown below; the latest version has been loaded underneath — "
                    "re-submit to overwrite, or reload to start again."]
            current = svc.get_contact(cid, interactions=0)
        return _form_page(request, contact=current, values=_submitted_values(t, form), errors=errs, status=409
                          if isinstance(e, svc.Conflict) else 422)
    return back(f"/contacts/{cid}")


# ================================================================ detail
@people.get("/contacts/{cid}", response_class=HTMLResponse)
def contact_detail(request: Request, cid: str):
    try:
        c = svc.get_contact(cid, interactions=50)
    except svc.NotFound:
        return HTMLResponse("Contact not found", status_code=404)
    others = [r for r in svc.search_contacts(limit=500) if r["id"] != c["id"]]
    return pages.TemplateResponse(request, "contact.html", {
        "c": c, "sections": _detail_sections(_template(), c["card"]), "others": others,
        "audit": svc.audit_trail(c["id"], 15), "all_tags": svc.list_tags(),
        "photo": next((m["uri"] for m in (c["card"].get("media") or {}).values() if m.get("kind") == "photo"), None),
    })


@people.post("/contacts/{cid}/archive")
def archive(request: Request, cid: str, archived: bool = Form(True)):
    svc.archive_contact(cid, actor(request), archived)
    return back(f"/contacts/{cid}")


@people.post("/contacts/{cid}/interactions")
def add_interaction(request: Request, cid: str, summary: str = Form(...), kind: str = Form("other"),
                    occurred_at: str = Form(""), notes: str = Form(""), also: list[str] = Form([])):
    svc.log_interaction([cid, *also], summary, actor(request), kind, occurred_at or None, notes or None)
    return back(f"/contacts/{cid}")


@people.post("/interactions/{iid}/delete")
def remove_interaction(request: Request, iid: int, contact: str = Form(...)):
    svc.delete_interaction(iid, actor(request))
    return back(f"/contacts/{contact}")


@people.post("/contacts/{cid}/follow-up")
def follow_up(request: Request, cid: str, every_days: str = Form(""), snoozed_until: str = Form("")):
    svc.set_follow_up(cid, int(every_days) if every_days.strip() else None, actor(request), snoozed_until or None)
    return back(f"/contacts/{cid}")


@people.post("/contacts/{cid}/tags")
def set_tags(request: Request, cid: str, tags: str = Form("")):
    svc.set_contact_tags(cid, [t.strip() for t in tags.split(",") if t.strip()], actor(request))
    return back(f"/contacts/{cid}")


@people.post("/contacts/{cid}/relationships")
def add_relationship(request: Request, cid: str, to_id: str = Form(...), relation: str = Form(...),
                     note: str = Form("")):
    svc.relate_contacts(cid, to_id, relation, actor(request), note or None)
    return back(f"/contacts/{cid}")


@people.post("/relationships/{rid}/delete")
def remove_relationship(request: Request, rid: int, contact: str = Form(...)):
    svc.delete_relationship(rid, actor(request))
    return back(f"/contacts/{contact}")


# ================================================================ tags
@people.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request):
    return pages.TemplateResponse(request, "tags.html", {"tags": svc.list_tags()})


@people.post("/tags/{tid}")
def update_tag(request: Request, tid: int, name: str = Form(""), kind: str = Form(""), color: str = Form("")):
    svc.update_tag(tid, actor(request), name or None, kind or None, color or None)
    return back("/tags")


@people.post("/tags/{tid}/delete")
def delete_tag(request: Request, tid: int):
    svc.delete_tag(tid, actor(request))
    return back("/tags")


# ================================================================ import / export
@people.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return pages.TemplateResponse(request, "import.html", {"preview": None})


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):  # older (2.1) exports are often Windows-1252
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# Parsed uploads waiting for confirmation, kept server-side: posting every card (with photos)
# back through a hidden form field would hit the 1 MB form-field limit for real address books.
_PENDING: dict[str, tuple[float, list[dict]]] = {}
_PENDING_TTL = 3600


def _stash(cards: list[dict]) -> str:
    now = time.time()
    for k in [k for k, (t, _, _) in _PENDING.items() if now - t > _PENDING_TTL]:
        del _PENDING[k]
    token = secrets.token_urlsafe(16)
    _PENDING[token] = (now, context.require().workspace.id, cards)   # confirmable only in this workspace
    return token


@people.post("/import", response_class=HTMLResponse)
async def import_preview(request: Request, file: UploadFile):
    try:
        preview = svc.preview_import(_decode(await file.read()))
    except svc.Invalid as e:
        return pages.TemplateResponse(request, "import.html", {"preview": None, "error": str(e)}, status_code=422)
    token = _stash([p["card"] for p in preview["items"]])
    return pages.TemplateResponse(request, "import.html", {"preview": preview, "token": token})


@people.post("/import/confirm", response_class=HTMLResponse)
async def import_confirm(request: Request):
    form = await request.form()
    pending = _PENDING.pop(str(form.get("token", "")), None)
    if pending and pending[1] != context.require().workspace.id:
        pending = None
    if not pending:
        return pages.TemplateResponse(request, "import.html", {
            "preview": None, "error": "This preview expired (or the server restarted). Please upload the file again."},
            status_code=410)
    cards = pending[2]
    chosen = [cards[int(i)] for i in form.getlist("pick") if str(i).isdigit() and int(i) < len(cards)]
    result = svc.import_cards(chosen, f"{actor(request)}:import")
    if result["failed"]:
        return pages.TemplateResponse(request, "import.html", {"preview": None, "result": result})
    return back("/people", imported=str(len(result["created"])))


@people.get("/export.vcf")
def export(tag: str = "", archived: bool = False):
    body = svc.export_vcard(tag=tag or None, include_archived=archived)
    name = f"cove-{tag or 'contacts'}.vcf"
    return Response(body, media_type="text/vcard; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _api(request: Request) -> bool:
    return request.url.path.startswith(api_v1.PREFIX + "/")


@app.exception_handler(svc.NotFound)
def not_found(request: Request, exc: svc.NotFound):
    if _api(request):
        return auth.api_error("not_found", str(exc), 404)
    return PlainTextResponse(str(exc), status_code=404)


@app.exception_handler(policy.Forbidden)
def forbidden(request: Request, exc: policy.Forbidden):
    if _api(request):
        return auth.api_error("forbidden", str(exc), 403)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": str(exc)}, status_code=403)
    return PlainTextResponse(f"You don't have permission to do that here ({exc}). Ask the workspace owner.",
                             status_code=403)


@app.exception_handler(svc.Invalid)
def invalid(request: Request, exc: svc.Invalid):
    if _api(request):
        return auth.api_error("invalid", str(exc), 422)
    return PlainTextResponse(f"Invalid input: {exc}", status_code=422)


@app.exception_handler(svc.Conflict)
def conflict(request: Request, exc: svc.Conflict):
    if _api(request):
        return auth.api_error("conflict", str(exc), 409)
    return PlainTextResponse(f"Conflict: {exc}", status_code=409)


@app.exception_handler(api_v1.ServiceOff)
def service_off(request: Request, exc: api_v1.ServiceOff):
    if _api(request) or request.url.path.startswith("/api/"):
        return auth.api_error("service_off", str(exc), 404)
    return pages.TemplateResponse(request, "service_off.html", {"svc": hub.CORE, "off": exc}, status_code=404)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if _api(request):
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
                413: "too_large"}.get(exc.status_code, "error")
        return auth.api_error(code, str(exc.detail), exc.status_code, getattr(exc, "headers", None))
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if _api(request):
        first = (exc.errors() or [{}])[0]
        where = ".".join(str(x) for x in first.get("loc", ()) if x != "body")
        return auth.api_error("invalid", f"{where}: {first.get('msg', 'invalid input')}".strip(": "), 422)
    return await request_validation_exception_handler(request, exc)


app.include_router(auth.router)
app.include_router(agents.router)
app.include_router(people)
app.include_router(hub.router)
app.include_router(notes_web.router)
app.include_router(notes_web.settings)
app.include_router(notes_api.router)
app.include_router(services_web.router)
app.include_router(api_v1.fabric_router)
app.include_router(api_v1.meta_router)
app.include_router(api_v1.people_router)
app.include_router(api_v1.notes_router)


@app.get("/healthz")
def healthz():
    return {"ok": True}
