"""OpenID Connect sign-in against Pocket ID (authorization code flow + PKCE).

Two base URLs, because the browser and the server reach the provider differently:
- COVE_AUTH_URL: the public address (and the token issuer). The browser is sent here to sign in.
- COVE_AUTH_INTERNAL_URL: how this container reaches it directly (token exchange, signing keys).

The state, nonce and PKCE verifier for a login in progress travel in a short-lived, HMAC-signed,
HttpOnly cookie, so starting a login writes nothing to the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets
import time
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from cove import secrets

LOGIN_COOKIE = "__Host-cove_login"
LOGIN_TTL = 600            # seconds to complete sign-in at the provider
ALGORITHMS = ["RS256", "ES256", "PS256", "EdDSA"]
SCOPES = "openid profile email"


class LoginError(Exception):
    """Sign-in failed; the message is safe to show."""


class NotConfigured(LoginError):
    pass


def settings() -> dict:
    issuer = os.environ.get("COVE_AUTH_URL", "").rstrip("/")
    return {
        "issuer": issuer,
        "internal": (os.environ.get("COVE_AUTH_INTERNAL_URL") or issuer).rstrip("/"),
        "client_id": os.environ.get("COVE_OIDC_CLIENT_ID", "cove"),
        "client_secret": secrets.get("oidc_client_secret", required=False) or "",
        "redirect_uri": os.environ.get("COVE_PUBLIC_URL", "").rstrip("/") + "/auth/callback",
    }


def configured() -> bool:
    s = settings()
    return bool(s["issuer"] and s["client_secret"] and s["redirect_uri"].startswith("http"))


# ------------------------------------------------------------------ provider metadata (cached)
_cache: dict = {}
_TRANSPORT: httpx.BaseTransport | None = None   # tests swap in a fake provider


def _client() -> httpx.Client:
    return httpx.Client(timeout=10, transport=_TRANSPORT, follow_redirects=False)


def _internal(url: str, s: dict) -> str:
    """Rewrite a public provider URL to the internal address (server-to-server calls)."""
    return s["internal"] + url[len(s["issuer"]):] if url.startswith(s["issuer"]) else url


def metadata() -> dict:
    s = settings()
    hit = _cache.get("meta")
    if hit and hit[0] > time.time() and hit[1]["issuer"] == s["issuer"]:
        return hit[1]
    try:
        with _client() as c:
            meta = c.get(s["internal"] + "/.well-known/openid-configuration").raise_for_status().json()
    except (httpx.HTTPError, ValueError) as e:
        raise LoginError("the sign-in service isn't reachable right now") from e
    if meta.get("issuer", "").rstrip("/") != s["issuer"]:
        raise LoginError("the sign-in service is misconfigured (issuer mismatch)")
    _cache["meta"] = (time.time() + 3600, meta)
    return meta


def _keys(force: bool = False) -> KeySet:
    hit = _cache.get("jwks")
    if hit and not force and hit[0] > time.time():
        return hit[1]
    if force and hit and hit[2] > time.time() - 60:   # refetch at most once a minute
        return hit[1]
    s = settings()
    with _client() as c:
        jwks = c.get(_internal(metadata()["jwks_uri"], s)).raise_for_status().json()
    ks = KeySet.import_key_set(jwks)
    _cache["jwks"] = (time.time() + 3600, ks, time.time())
    return ks


# ------------------------------------------------------------------ login state cookie
def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_state_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _unsign(value: str | None) -> dict | None:
    if not value or "." not in value or len(value) > 2000:
        return None
    body, mac = value.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_state_key(), body.encode(), hashlib.sha256).hexdigest(), mac):
        return None
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return payload if payload.get("exp", 0) > time.time() else None


def _state_key() -> bytes:
    return hmac.new(secrets.get("session_key").encode(), b"oidc-login-state", hashlib.sha256).digest()


def safe_next(target: str | None) -> str:
    """Only same-site relative paths, so the login can't be used as an open redirect."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def begin(next_path: str | None) -> tuple[str, str]:
    """Returns (provider URL to redirect the browser to, value for the login state cookie)."""
    if not configured():
        raise NotConfigured("sign-in isn't set up yet: run scripts/setup-auth.sh")
    s, meta = settings(), metadata()
    verifier = _secrets.token_urlsafe(48)
    state, nonce = _secrets.token_urlsafe(24), _secrets.token_urlsafe(24)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = meta["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": s["client_id"], "redirect_uri": s["redirect_uri"],
        "scope": SCOPES, "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    cookie = _sign({"state": state, "nonce": nonce, "verifier": verifier, "next": safe_next(next_path),
                    "exp": int(time.time()) + LOGIN_TTL})
    return url, cookie


def finish(params: dict, cookie: str | None) -> tuple[dict, str]:
    """Validate the provider's redirect; returns (verified ID token claims, where to go next)."""
    pending = _unsign(cookie)
    if pending is None:
        raise LoginError("this sign-in attempt expired or was started in another browser; please try again")
    if params.get("error"):
        raise LoginError(f"the sign-in service said: {params.get('error_description') or params['error']}"[:200])
    if not params.get("state") or not hmac.compare_digest(str(params["state"]), pending["state"]):
        raise LoginError("sign-in state didn't match; please try again")
    if not params.get("code"):
        raise LoginError("the sign-in service didn't return a code")
    s, meta = settings(), metadata()
    try:
        with _client() as c:
            r = c.post(_internal(meta["token_endpoint"], s), auth=(s["client_id"], s["client_secret"]), data={
                "grant_type": "authorization_code", "code": params["code"], "redirect_uri": s["redirect_uri"],
                "code_verifier": pending["verifier"]})
    except httpx.HTTPError as e:
        raise LoginError("the sign-in service isn't reachable right now") from e
    if r.status_code != 200 or "id_token" not in r.json():
        raise LoginError("the sign-in service rejected the login code")
    return verify_id_token(r.json()["id_token"], pending["nonce"]), pending["next"]


def verify_id_token(id_token: str, nonce: str) -> dict:
    s = settings()
    try:
        try:
            token = jwt.decode(id_token, _keys(), algorithms=ALGORITHMS)
        except JoseError:   # perhaps the provider rotated its keys
            token = jwt.decode(id_token, _keys(force=True), algorithms=ALGORITHMS)
        jwt.JWTClaimsRegistry(
            leeway=60,
            iss={"essential": True, "value": s["issuer"]},
            aud={"essential": True, "value": s["client_id"]},
            sub={"essential": True},
            exp={"essential": True},
            iat={"essential": True},
            nonce={"essential": True, "value": nonce},
        ).validate(token.claims)
    except (JoseError, ValueError) as e:
        raise LoginError("the sign-in token didn't verify") from e
    claims = token.claims
    aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if len(aud) > 1 and claims.get("azp") != s["client_id"]:
        raise LoginError("the sign-in token was issued for another app")
    return claims
