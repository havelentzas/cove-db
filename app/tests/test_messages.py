"""Messages: ingest contract, idempotency, agent rules, permissions, handle resolution, read-only exclusion.

All data here is synthetic (example.org addresses, +31 6 1234… numbers from no real person)."""

import copy
import json
import logging

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_WS, Browser
from cove import context, db, fabric
from cove.auth import policy, tokens
from cove.crm import service as svc
from cove.fabric import aggregate, services, worker
from cove.messages import ingest, service as messages
from cove.messages.normalize import handle_value, is_sensitive

PUBLIC = "https://cove.test"
ADA_PHONE = "+31612345678"
BODY = "Shall we meet at the lighthouse on Friday?"
OTP = "Your verification code is 482913. Don't share it."


def batch(**over):
    b = {
        "format": "cove.messages.ingest/v1",
        "source": {"key": "test-mac", "platform": "imessage", "label": "Test Mac", "collector": "pytest/1"},
        "high_water": {"rowid": 7},
        "handles": [{"platform": "imessage", "value": "me@example.org", "is_me": True}],
        "conversations": [
            {"external_id": "chat-ada", "platform": "imessage", "kind": "direct",
             "participants": [{"platform": "imessage", "value": "06 1234 5678"}]},
            {"external_id": "chat-bank", "platform": "sms", "kind": "direct",
             "participants": [{"platform": "sms", "value": "BANK"}]},
        ],
        "messages": [
            {"external_id": "m1", "conversation": "chat-ada", "sender": {"platform": "imessage", "value": "+31 6 12345678"},
             "is_from_me": False, "sent_at": "2026-11-01T09:00:00Z", "body": BODY},
            {"external_id": "m2", "conversation": "chat-ada", "sender": None, "is_from_me": True,
             "sent_at": "2026-11-01T09:05:00+01:00", "body": "Yes! Friday works.", "reply_to": "m1",
             "attachments": [{"filename": "map.png", "mime": "image/png", "size": 1234}]},
            {"external_id": "m3", "conversation": "chat-bank", "sender": {"platform": "sms", "value": "BANK"},
             "is_from_me": False, "sent_at": "2026-11-02T10:00:00Z", "body": OTP},
        ],
        "reactions": [{"target": "m2", "sender": {"platform": "imessage", "value": ADA_PHONE}, "is_from_me": False,
                       "type": "love", "at": "2026-11-01T09:06:00Z"}],
    }
    b.update(over)
    return b


@pytest.fixture()
def ada(database):
    return svc.create_contact(svc.build_card(full_name="Ada Lovelace", phones=["06-12345678"]), "web:t")


def _p(scopes, agent=False, role="owner"):
    return context.Principal(actor="mcp:t" if agent else "web:t", workspace=TEST_WS, role=role,
                             scopes=frozenset(scopes), agent=agent)


AGENT_ALL = _p({"messages:read", "people:read", "notes:read"}, agent=True)


def _token(kind="personal", scopes=None):
    with context.use(_p(policy.ALL)):
        return tokens.create("t-" + kind, "web:t", scopes=scopes, kind=kind)["token"]


def _client():
    return TestClient(__import__("cove.web.app", fromlist=["app"]).app, base_url=PUBLIC)


# ------------------------------------------------------------------ pure helpers
def test_handle_normalisation():
    assert handle_value("sms", "06 1234 5678") == ADA_PHONE
    assert handle_value("imessage", "tel:+31 (0)6-1234 5678".replace("(0)", "")) == ADA_PHONE
    assert handle_value("imessage", "Ada@Example.ORG") == "ada@example.org"
    assert handle_value("whatsapp", "name:‎Ada Lovelace ") == "name:Ada Lovelace"
    assert handle_value("sms", "12345") == "12345"          # short codes stay as they are


@pytest.mark.parametrize("text,sensitive", [
    (OTP, True), ("G-582910 is your Google verification code.", True), ("Je verificatiecode is 1234", True),
    ("Use 123-456 to sign in", True), ("See you at 10:30 on Friday", False), ("Call me on 0612345678", False),
    (BODY, False), ("", False)])
def test_otp_detection(text, sensitive):
    assert is_sensitive(text) is sensitive


# ------------------------------------------------------------------ ingest
def test_ingest_is_idempotent_and_merges_changes(ada):
    r = ingest.ingest_batch(batch(), "api:collector")
    assert (r["accepted"], r["updated"], r["duplicates"], r["rejected"]) == (3, 1, 0, [])   # +1: the reaction
    again = ingest.ingest_batch(batch(), "api:collector")
    assert (again["accepted"], again["updated"]) == (0, 0) and again["duplicates"] >= 3
    edited = batch(messages=[], reactions=[], edits=[{"external_id": "m2", "body": "Yes! Friday at noon works.",
                                                      "edited_at": "2026-11-01T09:10:00Z"}],
                   retractions=[{"external_id": "m3"}, {"external_id": "nope"}])
    r = ingest.ingest_batch(edited, "api:collector")
    assert r["updated"] == 2 and r["rejected"] == [{"external_id": "nope", "reason": "target_not_found"}]
    stale = batch()   # an old copy of m2 arrives again: the edit wins; the retraction is final
    ingest.ingest_batch(stale, "api:collector")
    with db.conn() as c:
        rows = {r["external_id"]: r for r in c.execute("SELECT * FROM messages.messages").fetchall()}
        conv = c.execute("SELECT message_count, last_message_at FROM messages.conversations "
                         "WHERE external_id = 'chat-ada'").fetchone()
        att = c.execute("SELECT filename FROM messages.attachments").fetchall()
        audits = c.execute("SELECT count(*) AS n FROM meta.audit_log WHERE action = 'ingest'").fetchone()["n"]
    assert rows["m2"]["body"] == "Yes! Friday at noon works." and rows["m2"]["edited_at"] is not None
    assert rows["m3"]["retracted"] and rows["m3"]["body"] is None
    assert rows["m2"]["reply_to_id"] == rows["m1"]["id"]
    assert list(rows["m2"]["reactions"].values())[0]["type"] == "love"
    assert rows["m1"]["sent_at"].isoformat().startswith("2026-11-01T09:00")
    assert conv["message_count"] == 2 and [a["filename"] for a in att] == ["map.png"]
    assert audits == 4                       # one audit row per batch, not per message
    assert ingest.ingest_state("test-mac")["high_water"] == {"rowid": 7}
    assert ingest.ingest_state("other")["high_water"] is None


def test_ingest_rejects_bad_messages_but_keeps_the_rest(database):
    b = batch(reactions=[])
    b["messages"] += [{"external_id": "m9", "conversation": "chat-unknown", "is_from_me": True,
                       "sent_at": "2026-11-01T09:00:00Z", "body": "x"},
                      {"external_id": "m10", "conversation": "chat-ada", "is_from_me": True,
                       "sent_at": "2026-11-01 09:00", "body": "naive time"}]
    r = ingest.ingest_batch(b, "t")
    assert r["accepted"] == 3
    assert {(x["external_id"], x["reason"]) for x in r["rejected"]} == {("m9", "unknown_conversation"),
                                                                       ("m10", "bad_timestamp")}


def test_invalid_batches_are_refused_without_echoing_text(database):
    b = batch()
    b["messages"][0]["body"] = 12345
    b["messages"][0]["oops"] = "private words"
    with pytest.raises(svc.Invalid) as e:
        ingest.ingest_batch(b, "t")
    assert "private" not in str(e.value) and "messages/0" in str(e.value)
    too_many = batch(messages=[{**batch()["messages"][1], "external_id": f"x{i}"} for i in range(1001)])
    with pytest.raises(svc.Invalid):
        ingest.ingest_batch(too_many, "t")


def test_logs_never_contain_message_text(ada, caplog):
    caplog.set_level(logging.DEBUG)
    ingest.ingest_batch(batch(), "t")
    with context.use(AGENT_ALL):
        aggregate.context_for(ADA_PHONE)
        aggregate.search_everything("lighthouse")
    assert "lighthouse" not in caplog.text and "482913" not in caplog.text


# ------------------------------------------------------------------ the HTTP ingest route and ingest tokens
def test_ingest_tokens_can_write_but_never_read(ada):
    tok = _token("ingest")
    assert tok.startswith("cove_ing_")
    c, h = _client(), {"Authorization": f"Bearer {tok}"}
    r = c.post("/api/v1/messages/ingest/batch", content=json.dumps(batch()), headers=h)
    assert r.status_code == 200 and r.json()["accepted"] == 3
    assert c.get("/api/v1/messages/ingest/state", params={"source": "test-mac"}, headers=h).json()["high_water"]
    for path in ("/api/v1/messages/conversations", "/api/v1/messages/search?q=lighthouse",
                 "/api/v1/fabric/search?q=lighthouse", "/api/v1/people/contacts"):
        r = c.get(path, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", path
    assert c.get("/api/notes/whoami", headers=h).status_code == 401
    from cove.mcp_server import build_app
    from test_policy import _rpc
    with TestClient(build_app(8765), base_url="http://127.0.0.1:8765") as m:
        assert _rpc(m, "tools/list", token=tok).status_code == 401


def test_bad_batches_get_the_error_envelope_and_size_limits(database):
    tok = _token("ingest")
    c, h = _client(), {"Authorization": f"Bearer {tok}"}
    r = c.post("/api/v1/messages/ingest/batch", content=b"not json", headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid"
    r = c.post("/api/v1/messages/ingest/batch", content=json.dumps({"format": "nope"}), headers=h)
    assert r.status_code == 422
    r = c.post("/api/v1/messages/ingest/batch", content=b"{}", headers={**h, "content-length": str(9 * 1024 * 1024)})
    assert r.status_code == 413


def test_ingest_tokens_need_the_ingest_permission(database):
    v = Browser("viewer", role="viewer")
    with context.use(_p(policy.effective("viewer", None), role="viewer")), pytest.raises(policy.Forbidden):
        tokens.create("mac", "web:viewer", kind="ingest")
    with context.use(_p(policy.ALL)), pytest.raises(svc.Invalid):
        tokens.create("sneaky", "web:t", scopes=["messages:ingest"], kind="personal")
    assert "messages:read" not in policy.ROLE_SCOPES["viewer"]
    assert not {"messages:read", "messages:write", "messages:ingest"} & policy.DEFAULT_AGENT_SCOPES
    assert v.get("/people").status_code == 200


# ------------------------------------------------------------------ handle resolution through the fabric
def test_handles_resolve_through_people_or_later_through_the_worker(ada):
    with context.use(_p({"messages:ingest"})):           # a collector: can't read People
        ingest.ingest_batch(batch(), "api:mac")
    with db.conn() as c:
        h = c.execute("SELECT resolution, contact_ref FROM messages.handles WHERE value = %s", (ADA_PHONE,)).fetchone()
    assert h["resolution"] == "none"
    worker.run_workspace(TEST_WS)                         # the worker acts as the owner, picks up handle.created
    with db.conn() as c:
        h = c.execute("SELECT resolution, contact_ref FROM messages.handles WHERE value = %s", (ADA_PHONE,)).fetchone()
    assert h["resolution"] == "auto" and h["contact_ref"] == fabric.ref("people", "contact", ada["id"])


def test_name_handles_are_only_suggested_and_contact_changes_rematch(ada):
    b = batch(handles=[], reactions=[], conversations=[{"external_id": "wa-1", "platform": "whatsapp",
              "participants": [{"platform": "whatsapp", "value": "name:Ada Lovelace"}]}],
              messages=[{"external_id": "w1", "conversation": "wa-1", "is_from_me": False, "body": "hi",
                         "sender": {"platform": "whatsapp", "value": "name:Ada Lovelace"},
                         "sent_at": "2026-11-03T08:00:00Z"}])
    ingest.ingest_batch(b, "t")
    with db.conn() as c:
        h = c.execute("SELECT * FROM messages.handles WHERE value = 'name:Ada Lovelace'").fetchone()
    assert h["resolution"] == "suggested" and h["contact_ref"] is None
    messages.link_handle(h["id"], ada["id"], "web:t")
    # a contact without a phone gains one: the worker links the waiting handle
    bob = svc.create_contact(svc.build_card(full_name="Bob Babbage"), "web:t")
    ingest.ingest_batch(batch(handles=[], reactions=[], messages=[], conversations=[
        {"external_id": "chat-bob", "platform": "sms", "participants": [{"platform": "sms", "value": "+31687654321"}]}]), "t")
    svc.update_contact(bob["id"], {"phones": {"p1": {"number": "+31 6 87654321"}}}, bob["version"], "web:t")
    worker.run_workspace(TEST_WS)
    with db.conn() as c:
        got = c.execute("SELECT contact_ref FROM messages.handles WHERE value = '+31687654321'").fetchone()
    assert got["contact_ref"] == fabric.ref("people", "contact", bob["id"])
    assert messages.list_handles("linked")["items"]


# ------------------------------------------------------------------ agent rules
def test_agents_never_see_hidden_conversations_or_one_time_codes(ada):
    ingest.ingest_batch(batch(), "t")
    # the user sees everything, one-time codes marked
    found = messages.search_messages("verification")["items"]
    assert found and found[0]["is_sensitive"]
    with context.use(AGENT_ALL):
        assert messages.search_messages("verification")["items"] == []
        assert all("482913" not in json.dumps(i) for i in messages.awaiting_reply(days=365)["items"])
    services.update_settings("messages", {"otp_filter": False}, "t")
    with context.use(AGENT_ALL):
        assert messages.search_messages("verification")["items"]
    conv = messages.list_conversations(person=ADA_PHONE)["items"][0]
    messages.set_conversation_agent_visible(conv["id"], False, "web:t")
    with context.use(AGENT_ALL):
        assert all(c["id"] != conv["id"] for c in messages.list_conversations()["items"])
        assert messages.search_messages("lighthouse")["items"] == []
        with pytest.raises(svc.NotFound):
            messages.get_conversation(conv["id"])
        ctx = aggregate.context_for(ADA_PHONE)
        assert all(i["service"] != "messages" for i in ctx["timeline"])
        assert "lighthouse" not in json.dumps(aggregate.search_everything("lighthouse"))
    pat = _token(scopes=["messages:read", "people:read"])     # personal-token API callers are agents too
    r = _client().get("/api/v1/messages/search", params={"q": "lighthouse"}, headers={"Authorization": f"Bearer {pat}"})
    assert r.status_code == 200 and r.json()["items"] == []
    assert Browser().get("/api/v1/messages/search", params={"q": "lighthouse"}).json()["items"]   # you still can


def test_without_people_permission_senders_stay_raw_handles(ada):
    ingest.ingest_batch(batch(), "t")
    messages._resolve(None)
    pat = _token(scopes=["messages:read"])
    c, h = _client(), {"Authorization": f"Bearer {pat}"}
    for path in ("/api/v1/messages/conversations", f"/api/v1/messages/search?q=lighthouse",
                 "/api/v1/messages/awaiting-reply?days=365", "/api/v1/messages/handles",
                 f"/api/v1/fabric/context?q={ADA_PHONE}", "/api/v1/fabric/search?q=lighthouse"):
        r = c.get(path, headers=h)
        assert r.status_code == 200, (path, r.text)
        assert "Ada" not in r.text and "Lovelace" not in r.text, path
    body = c.get("/api/v1/messages/conversations", headers=h).json()
    assert body["items"][0]["participants"][0]["label"] in (ADA_PHONE, "bank")
    assert {"service": "people", "reason": "no_permission"} in body["coverage"]["missing"]
    with context.use(AGENT_ALL):          # with people:read the name comes through the fabric
        convs = messages.list_conversations(person="Ada Lovelace")["items"]
    assert convs[0]["participants"][0]["label"] == "Ada Lovelace"


def test_mcp_read_only_sql_cannot_reach_messages(database):
    with psycopg.connect(database) as c:
        assert not c.execute("SELECT has_schema_privilege('cove_readonly', 'messages', 'USAGE')").fetchone()[0]
        tables = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'messages'").fetchall()]
        assert len(tables) == 6
        for t in tables:
            assert not c.execute("SELECT has_table_privilege('cove_readonly', %s, 'SELECT')",
                                 (f"messages.{t}",)).fetchone()[0], t
        assert not c.execute("SELECT has_table_privilege('cove_readonly', 'meta.events', 'SELECT')").fetchone()[0]
        assert c.execute("SELECT has_table_privilege('cove_app', 'messages.messages', 'INSERT')").fetchone()[0]


# ------------------------------------------------------------------ degradation (Part A acceptance)
def test_people_off_messages_still_ingests_searches_and_shows_threads(ada):
    services.set_enabled("people", False, "t")
    r = ingest.ingest_batch(batch(), "t")
    assert r["accepted"] == 3
    res = messages.search_messages("lighthouse")
    assert res["items"] and res["items"][0]["sender"]["label"] == ADA_PHONE
    conv = messages.list_conversations()
    assert {"service": "people", "reason": "disabled"} in conv["coverage"]["missing"] or \
        all(p["contact_ref"] is None for c in conv["items"] for p in c["participants"])
    thread = messages.get_conversation(conv["items"][-1]["id"])
    assert thread["messages"]
    out = aggregate.context_for(ADA_PHONE)            # no People: the phone number finds the handle itself
    assert out["subject"]["ref"].startswith("cove:messages/handle/")
    assert any(i["service"] == "messages" for i in out["timeline"])
    assert {"service": "people", "reason": "disabled"} in out["coverage"]["missing"]


def test_messages_off_people_and_context_for_carry_on(ada):
    ingest.ingest_batch(batch(), "t")
    messages._resolve(None)
    svc.set_follow_up(ada["id"], 7, "web:t")
    assert svc.get_contact(ada["id"])["messages"] is not None
    services.set_enabled("messages", False, "t")
    b = Browser()
    assert b.get(f"/contacts/{ada['id']}").status_code == 200
    assert b.get("/people?due=1").status_code == 200
    assert svc.get_contact(ada["id"])["messages"] is None
    out = aggregate.context_for("Ada Lovelace")
    assert out["subject"]["label"] == "Ada Lovelace"
    assert {"service": "messages", "reason": "disabled"} in out["coverage"]["missing"]
    assert b.get("/api/v1/messages/conversations").json()["error"]["code"] == "service_off"


def test_a_failing_or_slow_messages_is_a_200_with_coverage(ada, monkeypatch):
    ingest.ingest_batch(batch(), "t")
    m = fabric.manifests()["messages"]
    caps = dict(m.provides)

    def boom(**kw):
        raise RuntimeError(BODY)

    def slow(**kw):
        __import__("time").sleep(1)
        return {"items": [], "next_cursor": None}
    m.provides["timeline.items"] = fabric.Capability("timeline.items", slow, "messages:read", timeout=0.2)
    m.provides["search.query"] = fabric.Capability("search.query", boom, "messages:read")
    try:
        b = Browser()
        r = b.get("/api/v1/fabric/context", params={"q": "Ada Lovelace"})
        assert r.status_code == 200
        assert {"service": "messages", "reason": "timeout"} in r.json()["coverage"]["missing"]
        r = b.get("/api/v1/fabric/search", params={"q": "lighthouse"})
        assert r.status_code == 200
        assert {"service": "messages", "reason": "error"} in r.json()["coverage"]["missing"]
    finally:
        m.provides.clear()
        m.provides.update(caps)


def test_messages_fabric_outputs_match_contracts(ada):
    import jsonschema
    ingest.ingest_batch(batch(), "t")
    messages._resolve(None)
    ref = fabric.ref("people", "contact", ada["id"])
    for cap, kw in [("identity.resolve", {"identifiers": [ADA_PHONE]}),
                    ("identity.describe", {"refs": [messages.list_conversations()["items"][0]["ref"]]}),
                    ("timeline.items", {"subject_ref": ref}), ("search.query", {"q": "lighthouse"}),
                    ("activity.last_contact", {"refs": [ref]}),
                    ("activity.awaiting_reply", {"refs": None, "days": 365, "limit": 5})]:
        r = fabric.ask(cap, provider="messages", **kw)
        assert r.ok, (cap, r)
        doc = fabric.contracts()[cap]
        jsonschema.Draft202012Validator({**doc["output"], "$defs": doc["$defs"]}).validate(r.value)
        assert r.value, cap
