"""Messages in the browser and over MCP, and the Messages panel on a People contact page."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_WS, Browser
from cove import context, db, fabric
from cove.auth import policy, tokens
from cove.crm import service as svc
from cove.fabric import services
from cove.messages import ingest, service as messages
from test_messages import ADA_PHONE, BODY, batch

PUBLIC = "https://cove.test"


@pytest.fixture()
def world(database):
    ada = svc.create_contact(svc.build_card(full_name="Ada Lovelace", phones=["06-12345678"]), "web:t")
    ingest.ingest_batch(batch(), "t")
    messages._resolve(None)
    conv = messages.list_conversations(person=ADA_PHONE)["items"][0]
    return {"ada": ada, "conv": conv}


def test_pages_render(world):
    b = Browser()
    for url in ("/messages", "/messages?q=lighthouse", "/messages?person=Ada+Lovelace&platform=imessage",
                f"/messages/c/{world['conv']['id']}", f"/messages/c/{world['conv']['id']}?date=2026-11-01",
                "/messages/waiting?days=365", "/messages/senders", "/messages/senders?state=linked",
                "/messages/settings", "/"):
        r = b.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
    thread = b.get(f"/messages/c/{world['conv']['id']}").text
    assert BODY in thread and "Ada Lovelace" in thread and 'class="day-sep"' in thread
    assert "♥" in thread                                  # the tapback
    assert "Messages" in b.get("/").text


def test_thread_toggles_and_settings_forms(world):
    b = Browser()
    cid = world["conv"]["id"]
    assert b.post(f"/messages/c/{cid}/agents", {"visible": "false"}).status_code == 303
    assert not messages.list_conversations(person=ADA_PHONE)["items"][0]["agent_visible"]
    assert "Hidden from agents" in b.get(f"/messages/c/{cid}").text
    assert world["conv"]["title"] in b.get("/messages/settings").text
    r = b.post("/messages/settings/tokens", {"name": "my mac"})
    assert r.status_code == 200 and "cove_ing_" in r.text
    assert b.post("/messages/settings", {"otp_filter": "", "default_region": "GB", "whatsapp_name": "Sam Example"}).status_code == 303
    assert messages.settings()["default_region"] == "GB" and not messages.settings()["otp_filter"]
    assert b.post("/messages/settings", {"default_region": "XX"}).status_code == 422


def test_whatsapp_upload_form(world):
    b = Browser()
    from pathlib import Path
    data = (Path(__file__).parent / "fixtures" / "whatsapp" / "android_nl-NL.txt").read_bytes()
    r = b.client.post("/messages/import/whatsapp", data={"csrf": b.csrf, "my_name": "Sam Example"},
                      files={"file": ("WhatsApp Chat with Noor Voorbeeld.txt", data)}, headers={"origin": PUBLIC})
    assert r.status_code == 200 and "4 new" in r.text
    r = b.client.post("/messages/import/whatsapp", data={"csrf": b.csrf}, files={"file": ("x.txt", b"nope")},
                      headers={"origin": PUBLIC})
    assert r.status_code == 422 and "Couldn" in r.text


def test_senders_queue_links_by_typed_name(world):
    ingest.ingest_batch(batch(handles=[], reactions=[], conversations=[{"external_id": "wa", "platform": "whatsapp",
        "participants": [{"platform": "whatsapp", "value": "name:Countess"}]}], messages=[]), "t")
    b = Browser()
    with db.conn() as c:
        hid = c.execute("SELECT id FROM messages.handles WHERE value = 'name:Countess'").fetchone()["id"]
    assert "name:Countess" in b.get("/messages/senders?state=none").text or "Countess" in b.get("/messages/senders?state=none").text
    r = b.post(f"/messages/senders/{hid}", {"action": "link", "contact": "Ada Lovelace", "state": "none"})
    assert r.status_code == 303
    with db.conn() as c:
        assert c.execute("SELECT resolution FROM messages.handles WHERE id = %s", (hid,)).fetchone()["resolution"] == "manual"
    assert b.post(f"/messages/senders/{hid}", {"action": "link", "contact": "Nobody Atall"}).status_code == 422


# ------------------------------------------------------------------ the People panel (a non-critical integration)
def test_contact_page_shows_the_messages_panel_only_when_messages_answers(world):
    b = Browser()
    url = f"/contacts/{world['ada']['id']}"
    page = b.get(url).text
    assert 'class="card integration"' in page and "You wrote" in page
    services.set_enabled("messages", False, "t")
    page = b.get(url)
    assert page.status_code == 200 and 'class="card integration"' not in page.text
    services.set_enabled("messages", True, "t")
    v = Browser("viewer", role="viewer")                 # viewers don't get messages:read: no panel, no leak
    page = v.get(url)
    assert page.status_code == 200 and 'class="card integration"' not in page.text


def test_contact_page_survives_a_broken_messages(world):
    m = fabric.manifests()["messages"]
    saved = dict(m.provides)

    def boom(**kw):
        raise RuntimeError("down")
    m.provides["activity.last_contact"] = fabric.Capability("activity.last_contact", boom, "messages:read")
    try:
        r = Browser().get(f"/contacts/{world['ada']['id']}")
        assert r.status_code == 200 and 'class="card integration"' not in r.text
    finally:
        m.provides.clear()
        m.provides.update(saved)


# ------------------------------------------------------------------ MCP
def _pat(scopes):
    with context.use(context.Principal(actor="web:t", workspace=TEST_WS, scopes=policy.ALL)):
        return tokens.create("agent", "web:t", scopes=scopes, kind="personal")["token"]


def _call(c, tok, name, args):
    from test_policy import _rpc
    r = _rpc(c, "tools/call", {"name": name, "arguments": args}, token=tok).json()["result"]
    return r, (r.get("structuredContent") or (json.loads(r["content"][0]["text"]) if not r.get("isError") else None))


def test_mcp_messages_tools_wrap_bodies_and_follow_the_rules(world):
    from test_policy import _rpc
    from cove.mcp_server import build_app
    tok = _pat(["messages:read", "people:read"])
    with TestClient(build_app(8765), base_url="http://127.0.0.1:8765") as c:
        names = {t["name"] for t in _rpc(c, "tools/list", token=tok).json()["result"]["tools"]}
        assert {"search_messages", "get_conversation", "awaiting_reply", "message_activity", "list_conversations",
                "context_for"} <= names and "link_handle" not in names
        r, out = _call(c, tok, "search_messages", {"query": "lighthouse"})
        assert out["items"][0]["body"] == {"untrusted_text": BODY}
        r, out = _call(c, tok, "get_conversation", {"person": "Ada Lovelace"})
        assert out["messages"] and all(m["body"] is None or "untrusted_text" in m["body"] for m in out["messages"])
        r, out = _call(c, tok, "search_messages", {"query": "verification"})
        assert out["items"] == []                                     # one-time codes never reach agents
        r, out = _call(c, tok, "message_activity", {"person": ADA_PHONE, "period_days": 3650})
        assert out["all_time"] >= 2
        r, out = _call(c, tok, "context_for", {"ref_or_name": "Ada Lovelace"})
        assert any(i["service"] == "messages" for i in out["timeline"])
        r, out = _call(c, tok, "awaiting_reply", {"days": 3650})
        assert r.get("isError") is not True
        services.set_enabled("messages", False, "t")
        names = {t["name"] for t in _rpc(c, "tools/list", token=tok).json()["result"]["tools"]}
        assert "search_messages" not in names and "context_for" in names
        r, out = _call(c, tok, "context_for", {"ref_or_name": "Ada Lovelace"})
        assert {"service": "messages", "reason": "disabled"} in out["coverage"]["missing"]


def test_mcp_without_messages_permission(world):
    from test_policy import _rpc
    from cove.mcp_server import build_app
    tok = _pat(["people:read"])
    with TestClient(build_app(8765), base_url="http://127.0.0.1:8765") as c:
        names = {t["name"] for t in _rpc(c, "tools/list", token=tok).json()["result"]["tools"]}
        assert "search_messages" not in names
        r, out = _call(c, tok, "context_for", {"ref_or_name": "Ada Lovelace"})
        assert {"service": "messages", "reason": "no_permission"} in out["coverage"]["missing"]
        assert "lighthouse" not in json.dumps(out).lower()
        r, out = _call(c, tok, "search_everything", {"query": "lighthouse"})
        assert out["hits"] == []
