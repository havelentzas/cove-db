"""People and Notes on the fabric: contracts, context_for, search_everything, optional consumption."""

import json
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

from conftest import TEST_WS, Browser
from cove import context, db, fabric
from cove.auth import policy
from cove.crm import service as svc
from cove.fabric import aggregate, services
from cove.notes import service as notes

PHONE = "+31 6 1234 5678"     # synthetic


def _validate(capability, value):
    doc = fabric.contracts()[capability]
    jsonschema.Draft202012Validator({**doc["output"], "$defs": doc["$defs"]}).validate(value)


@pytest.fixture()
def world(database, blobs):
    ada = svc.create_contact(svc.build_card(full_name="Ada Lovelace", emails=["Ada@Example.org"], phones=["06-12345678"]),
                             "web:t")
    svc.update_contact(ada["id"], {"nicknames": {"n1": {"name": "Countess"}}}, ada["version"], "web:t")
    bob = svc.create_contact(svc.build_card(full_name="Bob Babbage"), "web:t")
    svc.log_interaction([ada["id"]], "Talked about the engine", "web:t", "call", notes="She wants the plans")
    notes.create_note("Journal/Monday.md", "Lunch with Ada Lovelace about looms.\n", "web:t")
    notes.create_note("Ideas/Engine.md", "Nothing about people here.\n", "web:t")
    return {"ada": ada, "bob": bob, "ref": fabric.ref("people", "contact", ada["id"])}


def _as(scopes, agent=True):
    return context.Principal(actor="mcp:t", workspace=TEST_WS, scopes=frozenset(scopes), agent=agent)


# ------------------------------------------------------------------ contracts
def test_people_and_notes_outputs_match_their_contracts(world):
    ref = world["ref"]
    for cap, kw in [("identity.resolve", {"identifiers": ["ada@example.org", "+31612345678", "Ada Lovelace"]}),
                    ("identity.describe", {"refs": [ref]}),
                    ("timeline.items", {"subject_ref": ref, "limit": 5}),
                    ("timeline.items", {"limit": 5}),
                    ("search.query", {"q": "Ada", "limit": 5}),
                    ("activity.last_contact", {"refs": [ref]})]:
        g = fabric.gather(cap, **kw)
        assert not g.coverage["missing"], (cap, g.coverage)
        for value in g.values.values():
            _validate(cap, value)


def test_identity_resolve_matches_phone_email_and_name(world):
    r = fabric.ask("identity.resolve", provider="people",
                   identifiers=["+31612345678", "ADA@example.org", "name:Ada Lovelace", "Countess", "+31699999999"])
    by = {(m["identifier"], m["method"]): m for m in r.value}
    assert by[("+31612345678", "phone")]["confidence"] == 1.0
    assert by[("ADA@example.org", "email")]["ref"] == world["ref"]
    assert by[("name:Ada Lovelace", "name")]["confidence"] < 0.9     # names are only ever suggestions
    assert ("Countess", "name") in by
    assert not any(m["identifier"] == "+31699999999" for m in r.value)


def test_describe_gives_aliases_for_mentions(world):
    d = fabric.ask("identity.describe", provider="people", refs=[world["ref"], "cove:people/contact/nope"]).value
    assert len(d) == 1 and "Countess" in d[0]["aliases"] and d[0]["url"].startswith("/contacts/")


# ------------------------------------------------------------------ context_for / search_everything
def test_context_for_merges_people_and_notes(world):
    out = aggregate.context_for("Ada Lovelace")
    assert out["subject"]["label"] == "Ada Lovelace"
    kinds = {i["kind"] for i in out["timeline"]}
    assert "interaction.call" in kinds and "note.mention" in kinds
    assert [i["title"] for i in out["timeline"] if i["kind"] == "note.mention"] == ["Monday"]
    assert out["last_contact"]["people"]["channel"] == "call"
    assert set(out["coverage"]["included"]) >= {"people", "notes"}


def test_context_for_by_phone_and_ambiguous_names(world):
    assert aggregate.context_for(PHONE)["subject"]["ref"] == world["ref"]
    svc.create_contact(svc.build_card(full_name="Ada Lovelace"), "web:t")
    out = aggregate.context_for("Ada Lovelace")
    assert out["subject"] is None and len(out["candidates"]) == 2


def test_context_for_with_a_service_off_still_answers_and_says_so(world):
    services.set_enabled("notes", False, "t")
    out = aggregate.context_for(world["ref"])
    assert out["subject"]["label"] == "Ada Lovelace"
    assert {"service": "notes", "reason": "disabled"} in out["coverage"]["missing"]
    assert all(i["service"] != "notes" for i in out["timeline"])


def test_context_for_without_people_permission_leaks_no_names(world):
    with context.use(_as({"notes:read"})):
        out = aggregate.context_for(world["ref"])
    assert out["subject"]["label"] is None
    assert {"service": "people", "reason": "no_permission"} in out["coverage"]["missing"]
    assert out["timeline"] == []        # Notes can't look for mentions without the name
    assert "Ada" not in str(out)


def test_search_everything_covers_every_readable_service(world):
    out = aggregate.search_everything("Ada")
    assert {h["service"] for h in out["hits"]} == {"people", "notes"}
    with context.use(_as({"people:read"})):
        out = aggregate.search_everything("Ada")
    assert {h["service"] for h in out["hits"]} == {"people"}
    assert {"service": "notes", "reason": "no_permission"} in out["coverage"]["missing"]


# ------------------------------------------------------------------ events
def test_people_and_notes_publish_change_events(world):
    from cove.fabric import events
    types = [e["type"] for e in events.events(0)["events"]]
    assert types.count("people.contact.created") == 2 and "people.contact.updated" in types
    assert types.count("notes.file.changed") == 2
    svc.archive_contact(world["bob"]["id"], "web:t")
    assert events.events(0, ["people.contact.archived"])["events"][0]["ref"].endswith(world["bob"]["id"])


# ------------------------------------------------------------------ optional consumption by People
@pytest.fixture()
def fake_messages(world):
    """A stand-in Messages that says you messaged Ada an hour ago."""
    sent = datetime.now(timezone.utc) - timedelta(hours=1)

    def last_contact(refs):
        return {r: {"last_inbound": None, "last_outbound": sent.isoformat(), "channel": "imessage"}
                for r in refs if r == world["ref"]}
    real = fabric.manifests().get("messages")
    fabric.register(fabric.Manifest("messages", 1, "people:read", {
        "activity.last_contact": fabric.Capability("activity.last_contact", last_contact, "people:read")}))
    with db.conn() as c:
        c.execute("INSERT INTO meta.services (key) VALUES ('messages') ON CONFLICT DO NOTHING")
    yield sent
    fabric.unregister("messages")
    if real:
        fabric.register(real)


def test_follow_ups_consider_messages_only_when_messages_is_there(world, fake_messages):
    ada = world["ada"]["id"]
    svc.set_follow_up(ada, 7, "web:t")
    with db.conn() as c:     # last logged conversation was long ago
        c.execute("UPDATE crm.interactions SET occurred_at = now() - interval '30 days'")
    assert [r["id"] for r in svc.follow_ups_due()] == []                 # a message an hour ago counts
    assert svc.get_contact(ada)["messages"]["channel"] == "imessage"
    services.set_enabled("messages", False, "t")
    assert [r["id"] for r in svc.follow_ups_due()] == [ada]             # exactly as People alone
    c = svc.get_contact(ada)
    assert c["messages"] is None and c["last_contacted"] < (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()


def test_a_failing_messages_never_breaks_people(world, fake_messages):
    def boom(refs):
        raise RuntimeError("down")
    fabric.manifests()["messages"].provides["activity.last_contact"] = fabric.Capability("activity.last_contact", boom,
                                                                                           "people:read")
    svc.set_follow_up(world["ada"]["id"], 7, "web:t")
    assert svc.get_contact(world["ada"]["id"])["messages"] is None
    b = Browser()
    assert b.get(f"/contacts/{world['ada']['id']}").status_code == 200
    assert b.get("/people").status_code == 200


# ------------------------------------------------------------------ HTTP + MCP surfaces
def test_api_v1_people_pages_with_cursors_and_change_feed(world):
    b = Browser()
    r = b.get("/api/v1/people/contacts", params={"limit": 1}).json()
    assert r["items"][0]["display_name"] == "Ada Lovelace" and r["items"][0]["ref"].startswith("cove:people/contact/")
    r2 = b.get("/api/v1/people/contacts", params={"limit": 1, "cursor": r["next_cursor"]}).json()
    assert r2["items"][0]["display_name"] == "Bob Babbage" and r2["next_cursor"] is None
    feed = b.get("/api/v1/people/contacts", params={"since": "2000-01-01T00:00:00Z"}).json()
    assert len(feed["items"]) == 2
    one = b.get(f"/api/v1/people/contacts/{world['ada']['id']}").json()
    assert one["ref"] == world["ref"]
    assert b.get("/api/v1/people/contacts/00000000-0000-0000-0000-000000000000").json()["error"]["code"] == "not_found"
    assert b.get("/api/v1/people/interactions").json()["items"][0]["title"] == "Talked about the engine"


def test_api_v1_notes_search_and_change_feed(world):
    b = Browser()
    assert len(b.get("/api/v1/notes/files", params={"q": "looms"}).json()["items"]) == 1
    feed = b.get("/api/v1/notes/files", params={"since": 0, "limit": 1}).json()
    assert len(feed["items"]) == 1 and feed["next_cursor"]
    rest = b.get("/api/v1/notes/files", params={"cursor": feed["next_cursor"]}).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None


def test_hub_and_catalog_show_integration_status(world):
    b = Browser()
    cat = {s["id"]: s for s in b.get("/api/services").json()["services"]}
    assert cat["people"]["integration"]["state"] == "enabled"
    assert any(c["capability"] == "identity.resolve" for c in cat["people"]["integration"]["provides"])
    services.set_enabled("notes", False, "t")
    cat = {s["id"]: s for s in b.get("/api/services").json()["services"]}
    assert cat["notes"]["status"] == "off"
    assert b.get("/").status_code == 200


def test_describe_system_lists_services_and_capabilities(world):
    d = svc.describe_system()
    st = {s["service"]: s for s in d["services"]}
    assert st["people"]["state"] == "enabled" and st["notes"]["provides"]


def test_mcp_context_for_wraps_text_as_untrusted(world):
    from test_policy import _pat, _rpc
    from fastapi.testclient import TestClient
    from cove.mcp_server import build_app
    tok = _pat(["people:read", "notes:read"])
    with TestClient(build_app(8765), base_url="http://127.0.0.1:8765") as c:
        r = _rpc(c, "tools/call", {"name": "context_for", "arguments": {"ref_or_name": "Ada Lovelace"}}, token=tok).json()
        assert not r["result"].get("isError"), r
        out = r["result"].get("structuredContent") or json.loads(r["result"]["content"][0]["text"])
        assert out["timeline"] and all(isinstance(i["title"], dict) and "untrusted_text" in i["title"]
                                       for i in out["timeline"])
        r = _rpc(c, "tools/call", {"name": "search_everything", "arguments": {"query": "Ada"}}, token=tok).json()
        assert "Ada" in r["result"]["content"][0]["text"]
