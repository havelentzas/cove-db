"""Sign-in, sessions, CSRF and access control (cove.web.auth, cove.auth.*, cove.control.identity)."""

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from conftest import Browser
from cove import db, secrets
from cove.auth import oidc, sessions
from cove.web import auth

ISSUER, INTERNAL, PUBLIC = "https://id.test", "http://pocket-id.internal", "https://cove.test"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})


def anon():
    from cove.web.app import app
    return TestClient(app, base_url=PUBLIC, follow_redirects=False)


# ------------------------------------------------------------------ deny by default
def _all_routes():
    from cove.web.app import app
    from cove.web.hub import _walk
    for r, _ in _walk(app.routes):
        for m in sorted((getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"}):
            yield m, r.path


def test_every_route_requires_sign_in(database):
    c, checked = anon(), 0
    for method, path in _all_routes():
        if auth.is_public(path):
            continue
        url = re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000001", path)
        r = c.request(method, url, headers={"origin": PUBLIC})
        if path.startswith(auth.TOKEN_PREFIXES):
            assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer", (method, path)
        elif method == "GET":
            assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), (method, path)
        else:
            assert r.status_code == 401, (method, path, r.status_code)
        checked += 1
    assert checked > 30


def test_public_paths_are_only_the_expected_ones():
    assert auth.PUBLIC_PATHS == {"/healthz", "/login", "/auth/start", "/auth/callback"}
    assert auth.PUBLIC_PREFIXES == ("/static/", "/brand/")
    assert auth.TOKEN_PREFIXES == ("/api/notes/",)    # device tokens instead of sessions


def test_login_redirect_keeps_target_and_htmx_gets_401(database):
    r = anon().get("/people?due=1")
    assert parse_qs(urlparse(r.headers["location"]).query)["next"] == ["/people?due=1"]
    r = anon().get("/people", headers={"hx-request": "true"})
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"


def test_signed_in_pages_render(browser):
    for url in ("/", "/people", "/notes", "/settings/devices", "/account"):
        r = browser.get(url)
        assert r.status_code == 200, url
        assert "harry" in r.text   # header user menu


# ------------------------------------------------------------------ CSRF
def test_post_without_csrf_token_is_rejected(browser):
    r = browser.client.post("/settings/devices", data={"name": "phone"}, headers={"origin": PUBLIC})
    assert r.status_code == 403
    r = browser.client.post("/settings/devices", data={"name": "phone", "csrf": "0" * 64}, headers={"origin": PUBLIC})
    assert r.status_code == 403
    with db.ctl_conn() as c:
        assert c.execute("SELECT count(*) AS n FROM control.security_events WHERE kind='csrf_rejected'").fetchone()["n"] == 2


def test_post_with_csrf_token_works_and_records_the_user(browser):
    r = browser.post("/contacts/new", {"f__full_name": "Ada Lovelace"})
    assert r.status_code == 303
    with db.conn() as c:
        assert c.execute("SELECT actor FROM meta.audit_log ORDER BY id DESC LIMIT 1").fetchone()["actor"] == "web:harry"
    # header form works too (for scripts / htmx)
    r = browser.client.post("/settings/devices", data={"name": "tablet"},
                            headers={"origin": PUBLIC, "x-csrf-token": browser.csrf})
    assert r.status_code == 200


def test_csrf_token_is_per_session(database):
    a, b = Browser("ann"), Browser("bob")
    assert a.csrf != b.csrf
    r = a.client.post("/settings/devices", data={"name": "x", "csrf": b.csrf}, headers={"origin": PUBLIC})
    assert r.status_code == 403


def test_every_post_form_in_templates_has_csrf_field():
    templates = Path(auth.__file__).parent / "templates"
    for f in templates.glob("*.html"):
        for form in re.findall(r"<form[^>]*method=\"post\"[^>]*>.{0,40}", f.read_text(), flags=re.S):
            assert "csrf_field()" in form, (f.name, form)


# ------------------------------------------------------------------ sessions
def test_expired_idle_revoked_and_disabled_sessions_stop_working(database):
    for sql in ("UPDATE control.sessions SET expires_at = now() - interval '1 s'",
                "UPDATE control.sessions SET last_seen_at = now() - interval '8 days'",
                "UPDATE control.sessions SET revoked_at = now()",
                "UPDATE control.users SET disabled_at = now()"):
        b = Browser("u" + str(abs(hash(sql)) % 10000))
        assert b.get("/people").status_code == 200
        with db.ctl_conn() as c:
            c.execute(sql + " WHERE " + ("id" if "users" in sql else "user_id") + " = %s", (b.user["id"],))
        assert b.get("/people").status_code == 303, sql


def test_session_tokens_are_stored_hashed(browser):
    with db.ctl_conn() as c:
        hashes = [r["token_hash"] for r in c.execute("SELECT token_hash FROM control.sessions").fetchall()]
    assert browser.token not in hashes and len(hashes[0]) == 64


def test_logout_ends_the_session_server_side(browser):
    old = browser.token
    r = browser.post("/auth/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login?signed_out=1"
    c = anon()
    c.cookies.set(sessions.COOKIE, old)
    assert c.get("/people").status_code == 303     # a copied cookie is useless after sign-out


def test_logout_everywhere_and_revoke_others(database):
    a1 = Browser("ann")
    a2_token = sessions.create(a1.user, ip=None, user_agent="other")
    assert a1.post("/account/sessions/revoke-others").status_code == 303
    assert sessions.lookup(a2_token) is None and sessions.lookup(a1.token) is not None
    a1.post("/auth/logout", {"everywhere": "true"})
    assert sessions.lookup(a1.token) is None


def test_cannot_revoke_someone_elses_session(database):
    a, b = Browser("ann"), Browser("bob")
    a.post(f"/account/sessions/{b.user and sessions.lookup(b.token)['session_id']}/revoke")
    assert sessions.lookup(b.token) is not None


# ------------------------------------------------------------------ admin
def test_first_user_is_admin_and_admin_page_is_admin_only(database):
    first, second = Browser("first"), Browser("second")
    assert first.user["is_admin"] and not second.user["is_admin"]
    assert second.get("/admin").status_code == 403
    assert second.post(f"/admin/users/{first.user['id']}", {"action": "disable"}).status_code == 403
    assert first.get("/admin").status_code == 200


def test_admin_disable_signs_the_user_out(database):
    admin, bob = Browser("admin", admin=True), Browser("bob")
    assert admin.post(f"/admin/users/{bob.user['id']}", {"action": "disable"}).status_code == 303
    assert bob.get("/people").status_code == 303
    assert admin.post(f"/admin/users/{admin.user['id']}", {"action": "disable"}).status_code == 400  # not yourself


# ------------------------------------------------------------------ OIDC sign-in (fake provider)
@pytest.fixture()
def provider(monkeypatch, database):
    monkeypatch.setenv("COVE_AUTH_URL", ISSUER)
    monkeypatch.setenv("COVE_AUTH_INTERNAL_URL", INTERNAL)
    monkeypatch.setenv("COVE_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("COVE_OIDC_CLIENT_ID", "cove")
    monkeypatch.setenv("COVE_SECRET_OIDC_CLIENT_SECRET", "s3cret")
    secrets.get.cache_clear()
    oidc._cache.clear()
    state = {"claims": {}, "key": KEY, "codes": {}, "token_calls": []}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "pocket-id.internal"            # server-to-server traffic stays internal
        if req.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                                             "token_endpoint": ISSUER + "/api/oidc/token",
                                             "jwks_uri": ISSUER + "/.well-known/jwks.json"})
        if req.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json=KeySet([KEY]).as_dict(private=False))
        if req.url.path == "/api/oidc/token":
            form = parse_qs(req.content.decode())
            state["token_calls"].append(form)
            assert req.headers["authorization"].startswith("Basic ")
            nonce = state["codes"].get(form["code"][0])
            if nonce is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": "cove", "sub": "user-1", "iat": now, "exp": now + 300, "nonce": nonce,
                      "preferred_username": "harry", "name": "Harry", "email": "h@example.test", "email_verified": True,
                      **state["claims"]}
            return httpx.Response(200, json={"id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, state["key"]),
                                             "access_token": "x", "token_type": "Bearer"})
        return httpx.Response(404)

    monkeypatch.setattr(oidc, "_TRANSPORT", httpx.MockTransport(handler))
    yield state
    secrets.get.cache_clear()
    oidc._cache.clear()


def _sign_in(provider, next_path="/people", tamper=None):
    c = anon()
    r = c.get("/auth/start", params={"next": next_path})
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith(ISSUER + "/authorize?")
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == [PUBLIC + "/auth/callback"]
    provider["codes"]["code-1"] = q["nonce"][0]
    params = {"code": "code-1", "state": q["state"][0]}
    if tamper:
        tamper(params, c)
    return c, c.get("/auth/callback", params=params)


def test_full_sign_in_sets_a_hardened_session_cookie(provider):
    c, r = _sign_in(provider)
    assert r.status_code == 303 and r.headers["location"] == "/people"
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith(sessions.COOKIE + "=")][0].lower()
    for flag in ("secure", "httponly", "samesite=lax", "path=/", "max-age="):
        assert flag in cookie
    assert "domain=" not in cookie
    assert provider["token_calls"][0]["code_verifier"]              # PKCE verifier sent
    assert c.get("/people").status_code == 200
    with db.ctl_conn() as conn:
        kinds = [e["kind"] for e in conn.execute("SELECT kind FROM control.security_events ORDER BY id").fetchall()]
    assert kinds == ["user_created", "workspaces_adopted", "login"]   # first user owns the existing workspace


def test_sign_in_twice_reuses_the_user_but_not_the_session(provider):
    _, r1 = _sign_in(provider)
    _, r2 = _sign_in(provider)
    assert r1.cookies[sessions.COOKIE] != r2.cookies[sessions.COOKIE]
    with db.ctl_conn() as c:
        assert c.execute("SELECT count(*) AS n FROM control.users").fetchone()["n"] == 1


@pytest.mark.parametrize("claims,label", [
    ({"iss": "https://evil.test"}, "issuer"),
    ({"aud": "another-app"}, "audience"),
    ({"exp": int(time.time()) - 3600}, "expired"),
    ({"nonce": "replayed"}, "nonce"),
])
def test_bad_id_tokens_are_rejected(provider, claims, label):
    provider["claims"] = claims
    _, r = _sign_in(provider)
    assert r.status_code == 400, label
    assert sessions.COOKIE not in r.cookies


def test_token_signed_by_another_key_is_rejected(provider):
    provider["key"] = OTHER_KEY
    _, r = _sign_in(provider)
    assert r.status_code == 400


def test_state_mismatch_and_missing_login_cookie_are_rejected(provider):
    _, r = _sign_in(provider, tamper=lambda p, c: p.update(state="forged"))
    assert r.status_code == 400
    _, r = _sign_in(provider, tamper=lambda p, c: c.cookies.delete(oidc.LOGIN_COOKIE))
    assert r.status_code == 400 and "expired" in r.text


def test_next_cannot_redirect_off_site(provider):
    for bad in ("//evil.test/x", "https://evil.test", "/\\evil.test"):
        _, r = _sign_in(provider, next_path=bad)
        assert r.headers["location"] == "/", bad


def test_disabled_user_cannot_sign_in(provider):
    _sign_in(provider)
    with db.ctl_conn() as c:
        c.execute("UPDATE control.users SET disabled_at = now()")
    _, r = _sign_in(provider)
    assert r.status_code == 403


def test_sign_in_fails_closed_when_not_configured(database, monkeypatch):
    monkeypatch.delenv("COVE_SECRET_OIDC_CLIENT_SECRET", raising=False)
    secrets.get.cache_clear()
    assert anon().get("/auth/start").status_code == 503
    assert "isn't set up" in anon().get("/login").text


# ------------------------------------------------------------------ workspaces in the web UI
def test_user_without_a_workspace_only_reaches_account_pages(database):
    b = Browser("loner", workspace=False)
    with db.ctl_conn() as c:
        c.execute("DELETE FROM control.memberships WHERE user_id = %s", (b.user["id"],))
    assert b.get("/people").status_code == 403 and "No workspace yet" in b.get("/people").text
    assert b.get("/account").status_code == 200


def test_csrf_token_is_bound_to_the_workspace(browser):
    with db.ctl_conn() as c:
        other = c.execute("INSERT INTO control.workspaces (slug, name, db_name, role_name, status) VALUES "
                          "('other', 'Other', 'cove_other', 'other_ws', 'active') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO control.memberships (workspace_id, user_id, role) VALUES (%s, %s, 'owner')",
                  (other, browser.user["id"]))
    old_csrf = browser.csrf
    assert browser.post("/workspace", {"workspace_id": str(other)}).status_code == 303
    r = browser.client.post("/settings/devices", data={"name": "x", "csrf": old_csrf}, headers={"origin": PUBLIC})
    assert r.status_code == 403        # a form from the previous workspace can't post into this one


def test_cannot_switch_to_a_workspace_you_are_not_in(browser):
    with db.ctl_conn() as c:
        other = c.execute("INSERT INTO control.workspaces (slug, name, db_name, role_name, status) VALUES "
                          "('theirs', 'Theirs', 'cove_theirs', 'their_ws', 'active') RETURNING id").fetchone()["id"]
    assert browser.post("/workspace", {"workspace_id": str(other)}).status_code == 403
