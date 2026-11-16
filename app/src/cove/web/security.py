"""HTTP hardening for the web app: security headers, request size limits, rate limits.

Implemented in the app (not only at a proxy) so they apply however cove is reached
(tailscale serve today, a reverse proxy / tunnel later) and are covered by tests.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque

from starlette.requests import Request
from starlette.responses import PlainTextResponse

# Strict CSP: no inline scripts anywhere (see static/cove.js). Inline *styles* are allowed: they
# can't execute code, and templates use style attributes for layout tweaks.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https:; font-src 'self'; connect-src 'self'; media-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
PUBLIC_ASSETS = ("/static/", "/brand/")

MB = 1024 * 1024
BODY_LIMITS = (("/api/notes/blobs/", 210 * MB), ("/import", 25 * MB),
               ("/api/v1/messages/ingest/whatsapp", 64 * MB),   # a chat export .zip (export "without media")
               ("/api/v1/messages/ingest/", 8 * MB),        # 1,000 messages per batch, long ones included
               ("/messages/import/whatsapp", 64 * MB))      # a chat export .zip (export "without media")
DEFAULT_BODY_LIMIT = 2 * MB


def body_limit(path: str) -> int:
    return next((n for prefix, n in BODY_LIMITS if path.startswith(prefix)), DEFAULT_BODY_LIMIT)


class RateLimiter:
    """Sliding-window limits per client, in memory (single web process)."""

    def __init__(self):
        self.hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, key: str, bucket: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        q = self.hits[(key, bucket)]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False, int(window - (now - q[0])) + 1
        q.append(now)
        if len(self.hits) > 50_000:   # bound memory under abuse
            self.hits.clear()
        return True, 0


LIMITS = {"api": 3000, "write": 240, "read": 1200, "auth": 30}   # per client per minute
limiter = RateLimiter()


def client_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return "tok:" + hashlib.sha256(auth.encode()).hexdigest()[:16]
    return "ip:" + (request.client.host if request.client else "?")


async def middleware(request: Request, call_next):
    path = request.url.path
    # 1) request size (checked from Content-Length before the body is read)
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > body_limit(path):
        return PlainTextResponse("request too large", status_code=413)
    # 2) rate limits
    if not path.startswith(PUBLIC_ASSETS) and path != "/healthz":
        if path == "/login" or path.startswith("/auth/"):
            bucket = "auth"           # sign-in attempts: tight, per IP
        elif path.startswith("/api/"):
            bucket = "api"
        else:
            bucket = "read" if request.method in ("GET", "HEAD") else "write"
        ok, retry = limiter.allow(client_key(request), bucket, LIMITS[bucket])
        if not ok:
            return PlainTextResponse("too many requests, slow down", status_code=429, headers={"Retry-After": str(retry)})
    # 3) security headers
    response = await call_next(request)
    for k, v in HEADERS.items():
        response.headers.setdefault(k, v)
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if path.startswith(PUBLIC_ASSETS):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
    else:
        response.headers.setdefault("Cache-Control", "no-store")   # pages hold private data
    return response
