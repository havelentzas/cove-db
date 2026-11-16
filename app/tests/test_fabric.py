"""The fabric (cove.fabric): optional integrations, coverage, permissions, events, enablement, /api/v1."""

import logging
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import MIGRATIONS, TEST_WS, Browser
from cove import context, db, fabric
from cove.auth import policy, tokens
from cove.fabric import aggregate, events, services, worker

PUBLIC = "https://cove.test"
SECRET = "the quick brown fox said the secret"   # stands in for message text: must never be logged


# ------------------------------------------------------------------ fake services
def _hit(service, q):
    return {"ref": f"cove:{service}/thing/1", "service": service, "title": f"{service} hit", "excerpt": q,
            "score": 0.5, "occurred_at": None}


@pytest.fixture()
def fakes(database):
    """alpha answers, beta raises, gamma is slow; delta is registered but has no meta.services row."""
    seen = []

    def alpha(q, limit=20):
        return [_hit("alpha", q)]

    def beta(q, limit=20):
        raise RuntimeError(SECRET)

    def gamma(q, limit=20):
        time.sleep(1.5)
        return [_hit("gamma", q)]

    def consume(evs):
        seen.extend(evs)

    mans = [
        fabric.Manifest("alpha", 1, "people:read", {"search.query": fabric.Capability("search.query", alpha, "people:read")},
                        consumers=(fabric.Consumer("alpha.listen", ("beta.thing.changed",), consume),)),
        fabric.Manifest("beta", 1, "notes:read", {"search.query": fabric.Capability("search.query", beta, "notes:read")}),
        fabric.Manifest("gamma", 1, "notes:read", {"search.query": fabric.Capability("search.query", gamma, "notes:read",
                                                                                     timeout=0.3)}),
        fabric.Manifest("delta", 1, "notes:read", {"search.query": fabric.Capability("search.query", alpha, "notes:read")}),
    ]
    real = fabric.manifests()
    saved = dict(fabric.core._registry)
    fabric.core._registry.clear()          # only the fakes, so the assertions are exact
    for m in mans:
        fabric.register(m)
    with db.conn() as c:
        c.execute("INSERT INTO meta.services (key) VALUES ('alpha'), ('beta'), ('gamma')")
    yield seen
    fabric.core._registry.clear()
    fabric.core._registry.update(saved)
    assert real is not None


def _as(scopes, agent=False, role="owner"):
    return context.Principal(actor="t", workspace=TEST_WS, role=role, scopes=frozenset(scopes), agent=agent)


# ------------------------------------------------------------------ ask / gather
def test_ask_returns_a_value_or_an_explicit_reason(fakes):
    r = fabric.ask("search.query", provider="alpha", q="x")
    assert r.ok and r.value[0]["ref"] == "cove:alpha/thing/1"
    assert fabric.ask("search.query", provider="beta", q="x").reason == "error"
    assert fabric.ask("search.query", provider="gamma", q="x").reason == "timeout"
    assert fabric.ask("search.query", provider="delta", q="x").reason == "not_installed"
    assert fabric.ask("search.query", provider="nobody", q="x").reason == "not_installed"
    services.set_enabled("alpha", False, "t")
    assert fabric.ask("search.query", provider="alpha", q="x").reason == "disabled"


def test_capabilities_check_the_callers_scopes(fakes):
    with context.use(_as({"notes:read"})):
        r = fabric.ask("search.query", provider="alpha", q="x")
    assert not r.ok and r.reason == "no_permission"


def test_gather_merges_and_reports_coverage(fakes, caplog):
    caplog.set_level(logging.INFO, "cove.fabric")
    started = time.monotonic()
    g = fabric.gather("search.query", q=SECRET)
    assert time.monotonic() - started < 1.2          # the slow provider doesn't hold everyone up
    assert set(g.values) == {"alpha"}
    assert g.coverage == {"included": ["alpha"], "missing": [
        {"service": "beta", "reason": "error"}, {"service": "delta", "reason": "not_installed"},
        {"service": "gamma", "reason": "timeout"}]}
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text   # never log arguments or messages


def test_coverage_merge_prefers_missing():
    c = fabric.coverage([fabric.Result("a", 1), fabric.Unavailable("b", "disabled")],
                        {"included": ["b", "c"], "missing": []})
    assert c == {"included": ["a", "c"], "missing": [{"service": "b", "reason": "disabled"}]}


def test_refs_round_trip():
    r = fabric.ref("people", "contact", "8c6f4f1e-1111-4222-8333-444455556666")
    assert fabric.parse_ref(r) == ("people", "contact", "8c6f4f1e-1111-4222-8333-444455556666")
    assert fabric.parse_ref("people/contact/1") is None and fabric.parse_ref("cove:x/y/a b") is None


def test_every_contract_has_input_and_output_schemas():
    import jsonschema
    names = set(fabric.contracts())
    assert {"identity.resolve", "identity.describe", "timeline.items", "search.query",
            "activity.last_contact"} <= names
    for doc in fabric.contracts().values():
        jsonschema.Draft202012Validator.check_schema({**doc["input"], "$defs": doc["$defs"]})
        jsonschema.Draft202012Validator.check_schema({**doc["output"], "$defs": doc["$defs"]})


def test_register_refuses_a_capability_without_a_contract():
    with pytest.raises(ValueError):
        fabric.register(fabric.Manifest("x", 1, None, {"made.up": fabric.Capability("made.up", print, None)}))


def test_search_everything_is_a_200_with_coverage_even_when_providers_fail(fakes):
    b = Browser()
    r = b.get("/api/v1/fabric/search", params={"q": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert [h["service"] for h in body["hits"]] == ["alpha"]
    assert {m["service"] for m in body["coverage"]["missing"]} == {"beta", "gamma", "delta"}


def test_capabilities_endpoint_says_what_is_available_to_you(fakes):
    b = Browser()
    caps = {s["service"]: s for s in b.get("/api/v1/fabric/capabilities").json()["services"]}
    assert caps["alpha"]["state"] == "enabled" and caps["alpha"]["provides"][0]["available"]
    assert caps["delta"]["state"] == "not_installed" and not caps["delta"]["provides"][0]["available"]


# ------------------------------------------------------------------ events
def test_events_publish_consume_and_failing_consumers_only_hurt_themselves(fakes):
    with db.conn() as c:
        fabric.publish(c, "beta", "beta.thing.changed", "cove:beta/thing/1", {"n": 1})
        fabric.publish(c, "beta", "beta.other.thing", "cove:beta/thing/2")
    assert worker.run_workspace(TEST_WS) == 1
    assert [e["ref"] for e in fakes] == ["cove:beta/thing/1"]
    assert worker.run_workspace(TEST_WS) == 0           # cursor moved on

    def broken(evs):
        raise ValueError(SECRET)
    with db.conn() as c:
        fabric.publish(c, "beta", "beta.thing.changed", "cove:beta/thing/3")
    assert events.consume("broken", ["beta.thing.changed"], broken) == 0
    cur = {c["consumer"]: c for c in events.cursors()}
    assert cur["broken"]["last_seq"] == 0 and cur["broken"]["last_error"] == "ValueError"
    assert events.consume("alpha.listen", ["beta.thing.changed"], fakes.extend) == 1   # others carry on


def test_event_feed_only_shows_services_you_can_read(fakes):
    with db.conn() as c:
        fabric.publish(c, "alpha", "alpha.thing.changed", "cove:alpha/thing/1")
        fabric.publish(c, "beta", "beta.thing.changed", "cove:beta/thing/1")
    with context.use(_as({"notes:read"})):
        assert [e["service"] for e in events.events()["events"]] == ["beta"]
    with context.use(_as({"people:read", "notes:read"})):
        assert len(events.events()["events"]) == 2


# ------------------------------------------------------------------ enablement
def test_only_owners_can_turn_services_off(fakes):
    with context.use(_as(policy.ALL, role="editor")), pytest.raises(policy.Forbidden):
        services.set_enabled("alpha", False, "t")


def test_a_disabled_service_hides_its_pages_and_keeps_its_data(database):
    b = Browser()
    assert b.get("/people").status_code == 200
    assert b.post("/settings/services/people", {"enabled": "false"}).status_code == 303
    r = b.get("/people")
    assert r.status_code == 404 and "turned off" in r.text
    assert b.get("/settings/services").status_code == 200
    b.post("/settings/services/people", {"enabled": "true"})
    assert b.get("/people").status_code == 200


def test_disabled_services_tools_disappear_from_mcp(database):
    from test_policy import _pat, _rpc
    from cove.mcp_server import build_app
    tok = _pat(["people:read", "notes:read"])
    with TestClient(build_app(8765), base_url="http://127.0.0.1:8765") as c:
        names = {t["name"] for t in _rpc(c, "tools/list", token=tok).json()["result"]["tools"]}
        assert "search_contacts" in names
        services.set_enabled("people", False, "t")
        names = {t["name"] for t in _rpc(c, "tools/list", token=tok).json()["result"]["tools"]}
        assert "search_contacts" not in names and "search_notes" in names and "describe_system" in names
        r = _rpc(c, "tools/call", {"name": "search_contacts", "arguments": {}}, token=tok).json()
        assert r["result"]["isError"] and "turned off" in r["result"]["content"][0]["text"]


# ------------------------------------------------------------------ /api/v1 auth
def _token(kind="personal", scopes=None):
    with context.use(_as(policy.ALL)):
        return tokens.create("api-test", "web:t", scopes=scopes, kind=kind)["token"]


def test_api_v1_takes_personal_tokens_and_sessions_but_not_device_tokens(database):
    c = TestClient(__import__("cove.web.app", fromlist=["app"]).app, base_url=PUBLIC)
    assert c.get("/api/v1/fabric/capabilities").status_code == 401
    pat = _token()
    assert c.get("/api/v1/fabric/capabilities", headers={"Authorization": f"Bearer {pat}"}).status_code == 200
    dev = _token("device")
    r = c.get("/api/v1/fabric/capabilities", headers={"Authorization": f"Bearer {dev}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert Browser().get("/api/v1/fabric/capabilities").status_code == 200
    # the Obsidian plugin's API is unchanged: device tokens only
    assert c.get("/api/notes/whoami", headers={"Authorization": f"Bearer {pat}"}).status_code == 401
    assert c.get("/api/notes/whoami", headers={"Authorization": f"Bearer {dev}"}).status_code == 200


def test_api_v1_errors_use_one_envelope(database):
    b = Browser()
    r = b.get("/api/v1/fabric/search")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid"
    r = b.get("/api/v1/fabric/context")
    assert r.status_code == 422 and set(r.json()["error"]) == {"code", "message"}
    r = b.client.post("/api/v1/fabric/search", headers={"origin": PUBLIC, "x-csrf-token": b.csrf})
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


def test_api_v1_session_writes_need_the_csrf_header(database):
    b = Browser()
    r = b.client.post("/api/v1/fabric/search", headers={"origin": PUBLIC})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"


def test_openapi_lists_the_v1_api_by_service(database):
    doc = Browser().get("/api/v1/openapi.json").json()
    assert "/api/v1/fabric/context" in doc["paths"]
    assert all(p.startswith("/api/v1/") for p in doc["paths"])
    assert doc["paths"]["/api/v1/fabric/search"]["get"]["tags"] == ["core"]


# ------------------------------------------------------------------ schema independence
def _statements(sql: str):
    return [s for s in re.split(r";\s*\n", re.sub(r"--[^\n]*", "", sql)) if s.strip()]


def test_no_cross_schema_foreign_keys_or_views():
    """Principle 1: no foreign keys or views from one service's schema into another's."""
    service_schemas = {"crm", "notes", "messages"}
    checked = 0
    for f in sorted(Path(MIGRATIONS).glob("*.sql")):
        up = f.read_text().split("-- migrate:down")[0]
        for stmt in _statements(up):
            m = re.search(r"(?:CREATE\s+TABLE|ALTER\s+TABLE|CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW)"
                          r"\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\.", stmt, re.I)
            if not m:
                continue
            own = m.group(1).lower()
            for target in re.findall(r"REFERENCES\s+(\w+)\.", stmt, re.I):
                assert target.lower() == own, f"{f.name}: {own} references {target}"
                checked += 1
            if re.search(r"\bVIEW\b", stmt[:80], re.I):
                others = {s for s in service_schemas - {own} if re.search(rf"\b{s}\.", stmt)}
                assert not others, f"{f.name}: a {own} view reads {others}"
    assert checked > 5
