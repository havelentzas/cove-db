"""Authorization: roles, scopes, tokens and the MCP server (cove.auth.policy, cove.auth.tokens, cove.mcp_server)."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_WS, Browser
from cove import context, db
from cove.auth import policy, tokens
from cove.crm import service as svc
from cove.notes import service as notes

PUBLIC = "https://cove.test"


# ------------------------------------------------------------------ coverage: nothing left unguarded
def test_every_service_function_is_guarded():
    from cove.fabric import aggregate, core, events, services
    for module in (svc, notes, core, services, events, aggregate):
        assert policy.unguarded(module) == [], module.__name__


async def _tool_names():
    from cove.mcp_server import mcp
    return {t.name for t in await mcp.all_tools()}


def test_every_mcp_tool_has_a_scope():
    import anyio
    from cove.mcp_server import TOOL_SCOPES
    assert anyio.run(_tool_names) == set(TOOL_SCOPES)
    assert set(TOOL_SCOPES.values()) <= policy.ALL | {None}


def test_service_refuses_without_scope(database):
    reader = context.Principal(actor="x", workspace=TEST_WS, scopes=frozenset({"people:read"}))
    with context.use(reader):
        svc.search_contacts()
        with pytest.raises(policy.Forbidden):
            svc.create_contact({"@type": "Card", "version": "1.0", "name": {"full": "X"}}, "x")
        with pytest.raises(policy.Forbidden):
            svc.sql_query("select 1")
        with pytest.raises(policy.Forbidden):
            notes.search()


# ------------------------------------------------------------------ roles in the web UI
def test_viewer_can_read_but_not_write(database):
    v = Browser("viewer", role="viewer")
    assert v.get("/people").status_code == 200
    r = v.post("/contacts/new", {"f__full_name": "Nope"})
    assert r.status_code == 403 and "permission" in r.text
    assert v.post("/settings/devices", {"name": "laptop"}).status_code == 403   # no notes:sync


def test_editor_can_write(database):
    e = Browser("editor", role="editor")
    assert e.post("/contacts/new", {"f__full_name": "Ada"}).status_code == 303


# ------------------------------------------------------------------ tokens
def _as(role, user_id=None):
    return context.Principal(actor="web:t", workspace=TEST_WS, user_id=user_id, role=role,
                             scopes=policy.effective(role, None))


def test_agent_token_defaults_to_read_only_and_cannot_exceed_its_creator(database):
    b = Browser("owner")
    with context.use(_as("owner", b.user["id"])):
        t = tokens.create("claude-code", "web:owner", kind="personal")
    assert set(t["scopes"]) == policy.DEFAULT_AGENT_SCOPES
    v = Browser("viewer", role="viewer")
    with context.use(_as("viewer", v.user["id"])), pytest.raises(policy.Forbidden):
        tokens.create("sneaky", "web:viewer", scopes=["people:write"], kind="personal")


def test_token_scopes_are_capped_by_the_users_current_role(database):
    b = Browser("ann")
    with context.use(_as("owner", b.user["id"])):
        t = tokens.create("bot", "web:ann", scopes=["people:read", "people:write"], kind="personal")
    assert tokens.verify(t["token"], "personal")["effective"] == {"people:read", "people:write"}
    with db.ctl_conn() as c:   # demoted to viewer: the token loses write at once
        c.execute("UPDATE control.memberships SET role = 'viewer' WHERE user_id = %s", (b.user["id"],))
    assert tokens.verify(t["token"], "personal")["effective"] == {"people:read"}
    with db.ctl_conn() as c:   # removed from the workspace: the token stops working
        c.execute("DELETE FROM control.memberships WHERE user_id = %s", (b.user["id"],))
    assert tokens.verify(t["token"], "personal") is None


def test_token_kinds_are_not_interchangeable(database):
    with context.use(_as("owner")):
        dev = tokens.create("laptop", "web:t", kind="device")
        pat = tokens.create("agent", "web:t", kind="personal")
    assert tokens.verify(dev["token"], "personal") is None
    assert tokens.verify(pat["token"], "device") is None
    c = TestClient(__import__("cove.web.app", fromlist=["app"]).app)
    assert c.get("/api/notes/whoami", headers={"Authorization": f"Bearer {pat['token']}"}).status_code == 401


def test_expired_and_revoked_tokens_fail(database):
    with context.use(_as("owner")):
        t = tokens.create("tmp", "web:t", kind="personal", expires_days=1)
        assert tokens.verify(t["token"], "personal")
        with db.ctl_conn() as c:
            c.execute("UPDATE control.api_tokens SET expires_at = now() - interval '1 s' WHERE id = %s", (t["id"],))
        assert tokens.verify(t["token"], "personal") is None
        t2 = tokens.create("tmp2", "web:t", kind="personal")
        tokens.revoke(t2["id"], "web:t")
        assert tokens.verify(t2["token"], "personal") is None


def test_agents_page_creates_scoped_tokens(browser):
    r = browser.post("/settings/agents", {"name": "brief", "scope": ["people:read", "notes:read"]})
    assert r.status_code == 200 and "cove_pat_" in r.text and "claude mcp add" in r.text


# ------------------------------------------------------------------ MCP over HTTP
@pytest.fixture()
def mcp_client(database):
    from cove.mcp_server import build_app
    with TestClient(build_app(8765), base_url="http://127.0.0.1:8765") as c:
        yield c


def _rpc(c, method, params=None, token=None):
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json",
               "mcp-protocol-version": "2025-06-18"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    r = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return r


def _pat(scopes):
    with context.use(_as("owner")):
        return tokens.create("test-agent", "web:t", scopes=scopes, kind="personal")["token"]


def test_mcp_requires_a_token(mcp_client):
    r = _rpc(mcp_client, "tools/list")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert _rpc(mcp_client, "tools/list", token="cove_pat_nope").status_code == 401


def test_mcp_lists_and_runs_only_permitted_tools(mcp_client):
    ro = _pat(["people:read"])
    names = {t["name"] for t in _rpc(mcp_client, "tools/list", token=ro).json()["result"]["tools"]}
    assert "search_contacts" in names and "create_contact" not in names and "sql_query" not in names
    r = _rpc(mcp_client, "tools/call", {"name": "create_contact", "arguments": {"full_name": "X"}}, token=ro).json()
    assert r["result"]["isError"] and "isn't allowed" in r["result"]["content"][0]["text"]
    r = _rpc(mcp_client, "tools/call", {"name": "search_contacts", "arguments": {}}, token=ro).json()
    assert not r["result"].get("isError")


def test_mcp_writes_are_labelled_with_the_token_name(mcp_client):
    rw = _pat(["people:read", "people:write"])
    r = _rpc(mcp_client, "tools/call", {"name": "create_contact", "arguments": {"full_name": "Grace"}}, token=rw).json()
    assert not r["result"].get("isError"), r
    with db.conn() as c:
        assert c.execute("SELECT actor FROM meta.audit_log ORDER BY id DESC LIMIT 1").fetchone()["actor"] == "mcp:test-agent"


def test_mcp_caps_bulk_calls(mcp_client):
    rw = _pat(["people:read", "people:write"])
    r = _rpc(mcp_client, "tools/call", {"name": "tag_contacts", "arguments": {
        "contact_ids": [f"00000000-0000-0000-0000-{i:012d}" for i in range(101)], "tags": ["x"]}}, token=rw).json()
    assert r["result"]["isError"] and "At most 100" in r["result"]["content"][0]["text"]
