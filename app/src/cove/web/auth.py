"""Sign-in, sessions and CSRF for the web UI, plus the Account and Admin pages.

Access is **deny by default**: `middleware` requires a valid session for every path except the few
listed in PUBLIC_PATHS / PUBLIC_PREFIXES (assets, health check, the sign-in flow itself, and the notes
sync API, which authenticates every request with its own bearer token). New routes are therefore
protected without anyone remembering to add a dependency; tests/test_auth.py checks every route.

Every state-changing browser request must also carry the session's CSRF token (form field `csrf`, or
header `X-CSRF-Token`). Templates add it with `{{ csrf_field() }}`.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool

from cove.auth import oidc, sessions
from cove.control import identity

PUBLIC_PATHS = {"/healthz", "/login", "/auth/start", "/auth/callback"}
PUBLIC_PREFIXES = ("/static/", "/brand/", "/api/notes/")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

router = APIRouter(tags=["core"])


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def client_ip(request: Request) -> str | None:
    """The client's IP (after uvicorn's proxy-header handling), or None if it isn't an IP address."""
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


def _cookie(response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


async def middleware(request: Request, call_next):
    request.state.user = None
    request.state.csrf = ""
    path = request.url.path
    if is_public(path):
        return await call_next(request)

    token = request.cookies.get(sessions.COOKIE)
    session = await run_in_threadpool(sessions.lookup, token) if token else None
    if session is None:
        htmx = bool(request.headers.get("hx-request"))
        if request.method in ("GET", "HEAD") and not htmx:
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)
        resp = PlainTextResponse("sign in required", status_code=401)
        if htmx:
            resp.headers["HX-Redirect"] = "/login"
        return resp

    request.state.user = session
    request.state.csrf = sessions.csrf_token(token)
    if request.method not in SAFE_METHODS:
        submitted = request.headers.get("x-csrf-token")
        ctype = request.headers.get("content-type", "")
        if not submitted and ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            await request.body()           # cache the body so the route can read it again
            form = await request.form()
            submitted = form.get("csrf")
            await form.close()
        if not sessions.check_csrf(token, submitted if isinstance(submitted, str) else None):
            await run_in_threadpool(identity.event, "csrf_rejected", user_id=session["id"],
                                    session_id=session["session_id"], ip=client_ip(request),
                                    user_agent=request.headers.get("user-agent"), path=path)
            return PlainTextResponse("This form expired. Go back, reload the page and try again.", status_code=403)
    return await call_next(request)


def csrf_field(ctx) -> Markup:
    request = ctx.get("request")
    value = request.state.csrf if request is not None else ""
    return Markup(f'<input type="hidden" name="csrf" value="{escape(value)}">')


def current_user(request: Request) -> dict:
    return request.state.user


def _pages():
    from cove.web.app import pages
    return pages


def _login_page(request: Request, *, error: str | None = None, status: int = 200, signed_out: bool = False):
    return _pages().TemplateResponse(request, "login.html", {
        "error": error, "signed_out": signed_out, "next": oidc.safe_next(request.query_params.get("next")),
        "configured": oidc.configured()}, status_code=status)


# ================================================================ sign in / out
@router.get("/login", response_class=HTMLResponse)
def login(request: Request, signed_out: bool = False):
    if request.cookies.get(sessions.COOKIE) and sessions.lookup(request.cookies[sessions.COOKIE]):
        return RedirectResponse(oidc.safe_next(request.query_params.get("next")), status_code=303)
    return _login_page(request, signed_out=signed_out)


@router.get("/auth/start")
def start(request: Request, next: str = "/"):
    try:
        url, state = oidc.begin(next)
    except oidc.LoginError as e:
        return _login_page(request, error=str(e), status=503)
    resp = RedirectResponse(url, status_code=303)
    _cookie(resp, oidc.LOGIN_COOKIE, state, oidc.LOGIN_TTL)
    return resp


@router.get("/auth/callback")
def callback(request: Request):
    ip, ua = client_ip(request), request.headers.get("user-agent")
    try:
        claims, next_path = oidc.finish(dict(request.query_params), request.cookies.get(oidc.LOGIN_COOKIE))
        user = identity.sign_in(claims["iss"], claims["sub"], claims, ip=ip, user_agent=ua)
    except identity.Disabled:
        return _login_page(request, error="This account has been disabled. Ask the admin of this cove.", status=403)
    except oidc.LoginError as e:
        identity.event("login_failed", ip=ip, user_agent=ua, reason=str(e))
        return _login_page(request, error=str(e), status=400)
    token = sessions.create(user, ip=ip, user_agent=ua)   # always a fresh session ID at sign-in
    resp = RedirectResponse(next_path, status_code=303)
    _cookie(resp, sessions.COOKIE, token, int(sessions.ABSOLUTE.total_seconds()))
    resp.delete_cookie(oidc.LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return resp


@router.post("/auth/logout")
def logout(request: Request, everywhere: bool = Form(False)):
    me = current_user(request)
    if everywhere:
        sessions.revoke_all(me["id"], ip=client_ip(request))
    else:
        sessions.revoke(me["session_id"], user_id=me["id"], ip=client_ip(request))
    resp = RedirectResponse("/login?signed_out=1", status_code=303)
    resp.delete_cookie(sessions.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return resp


# ================================================================ account
@router.get("/account", response_class=HTMLResponse)
def account(request: Request):
    me = current_user(request)
    return _pages().TemplateResponse(request, "account.html", {
        "svc": _core(), "me": me, "sessions": sessions.list_for(me["id"]),
        "events": identity.events(me["id"], limit=25), "auth_url": oidc.settings()["issuer"]})


@router.post("/account/sessions/{sid}/revoke")
def revoke_session(request: Request, sid: str):
    me = current_user(request)
    sessions.revoke(sid, user_id=me["id"], ip=client_ip(request))
    if sid == me["session_id"]:
        return RedirectResponse("/login?signed_out=1", status_code=303)
    return RedirectResponse("/account", status_code=303)


@router.post("/account/sessions/revoke-others")
def revoke_others(request: Request):
    me = current_user(request)
    sessions.revoke_all(me["id"], except_session=me["session_id"], ip=client_ip(request))
    return RedirectResponse("/account", status_code=303)


# ================================================================ admin
def _admin(request: Request) -> dict | None:
    me = current_user(request)
    return me if me and me["is_admin"] else None


@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if not _admin(request):
        return PlainTextResponse("Only admins can open this page.", status_code=403)
    return _pages().TemplateResponse(request, "admin.html", {
        "svc": _core(), "me": current_user(request), "users": identity.list_users(),
        "events": identity.events(limit=100), "auth_url": oidc.settings()["issuer"]})


@router.post("/admin/users/{uid}")
def admin_user(request: Request, uid: str, action: str = Form(...)):
    me = _admin(request)
    if not me:
        return PlainTextResponse("Only admins can do this.", status_code=403)
    try:
        if action in ("disable", "enable"):
            identity.set_disabled(uid, action == "disable", by=me, ip=client_ip(request))
        elif action in ("make-admin", "remove-admin"):
            identity.set_admin(uid, action == "make-admin", by=me, ip=client_ip(request))
        elif action == "sign-out":
            sessions.revoke_all(uid, by=me["id"], ip=client_ip(request))
        else:
            return PlainTextResponse("unknown action", status_code=400)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=400)
    return RedirectResponse("/admin", status_code=303)


def _core() -> dict:
    from cove.web.hub import CORE
    return CORE
