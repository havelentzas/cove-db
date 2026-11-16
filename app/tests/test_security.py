from fastapi.testclient import TestClient

from cove.web import security


def client():
    from cove.web.app import app
    return TestClient(app, base_url="https://cove.test")


def test_security_headers_and_no_store(browser):
    r = browser.get("/people")
    assert r.status_code == 200
    h = r.headers
    assert "script-src 'self'" in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"].split("script-src")[1].split(";")[0]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["strict-transport-security"].startswith("max-age=")
    assert h["cache-control"] == "no-store"
    assert "<script>" not in r.text and "onclick=" not in r.text     # CSP would block inline code
    assert client().get("/static/cove.js").headers["cache-control"].startswith("public")


def test_request_size_limit(database):
    r = client().post("/contacts/new", content=b"x" * (3 * 1024 * 1024),
                      headers={"content-type": "application/x-www-form-urlencoded", "origin": "https://cove.test"})
    assert r.status_code == 413


def test_rate_limit_returns_429_with_retry_after(monkeypatch, browser):
    monkeypatch.setattr(security, "limiter", security.RateLimiter())
    monkeypatch.setitem(security.LIMITS, "read", 5)
    c = browser
    codes = [c.get("/people").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    assert int(c.get("/people").headers["retry-after"]) >= 1


def test_cross_site_post_blocked(database):
    r = client().post("/contacts/new", data={"f__full_name": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
