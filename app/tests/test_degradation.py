"""The degradation matrix (docs/fabric.md, Part A acceptance): every service × every way it can be missing.

For each service (People, Notes, Messages) and each failure (turned off, no permission, raises, too slow), the
cross-service answers (context_for, search_everything, over the API and directly) still come back as a 200,
name the missing service with the right reason, and the other services' parts are still there. All data is
synthetic.
"""

import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_WS
from cove import context, fabric
from cove.auth import policy, tokens
from cove.crm import service as svc
from cove.fabric import aggregate, services
from cove.messages import ingest
from cove.notes import service as notes

SERVICES = ("people", "notes", "messages")
FAILURES = ("disabled", "no_permission", "error", "timeout")
READ = {"people": "people:read", "notes": "notes:read", "messages": "messages:read"}


@pytest.fixture()
def world(database, blobs):
    ada = svc.create_contact(svc.build_card(full_name="Ada Lovelace", phones=["06-12345678"]), "web:t")
    svc.log_interaction([ada["id"]], "Talked about the lighthouse", "web:t", "call")
    notes.create_note("Journal/Friday.md", "Lighthouse walk with Ada Lovelace.\n", "web:t")
    ingest.ingest_batch({
        "format": "cove.messages.ingest/v1",
        "source": {"key": "matrix", "platform": "imessage", "label": "Matrix", "collector": "pytest/1"},
        "conversations": [{"external_id": "c1", "platform": "imessage", "kind": "direct",
                           "participants": [{"platform": "imessage", "value": "+31612345678"}]}],
        "messages": [{"external_id": "x1", "conversation": "c1", "is_from_me": False,
                      "sender": {"platform": "imessage", "value": "+31612345678"},
                      "sent_at": "2026-11-01T09:00:00Z", "body": "See you at the lighthouse"}],
    }, "web:t")
    from cove.messages import service as messages
    messages._resolve(None)
    return {"ada": ada, "ref": fabric.ref("people", "contact", ada["id"])}


@contextmanager
def broken(service: str, failure: str):
    """Make every capability of `service` fail in the given way. Yields the scopes the caller should have."""
    scopes = set(READ.values())
    if failure == "disabled":
        services.set_enabled(service, False, "t")
        yield scopes
        return
    if failure == "no_permission":
        yield scopes - {READ[service]}
        return
    m = fabric.manifests()[service]
    saved = dict(m.provides)

    def fail(**kw):
        raise RuntimeError("synthetic failure")

    def slow(**kw):
        time.sleep(1.0)
        return None
    for name, cap in saved.items():
        m.provides[name] = fabric.Capability(name, fail if failure == "error" else slow, cap.scope,
                                             timeout=0.2 if failure == "timeout" else None)
    try:
        yield scopes
    finally:
        m.provides.clear()
        m.provides.update(saved)


def _token(scopes) -> str:
    with context.use(context.Principal(actor="web:t", workspace=TEST_WS, scopes=policy.ALL)):
        return tokens.create("matrix", "web:t", scopes=sorted(scopes), kind="personal")["token"]


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("service", SERVICES)
def test_every_service_can_fail_every_way_and_answers_still_come_back(world, service, failure):
    with broken(service, failure) as scopes:
        client = TestClient(__import__("cove.web.app", fromlist=["app"]).app, base_url="https://cove.test")
        h = {"Authorization": "Bearer " + _token(scopes)}
        missing = {"service": service, "reason": failure}
        others = [s for s in SERVICES if s != service]

        r = client.get("/api/v1/fabric/search", params={"q": "lighthouse"}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert missing in body["coverage"]["missing"]
        assert {h_["service"] for h_ in body["hits"]} == set(others), body["coverage"]

        subject = "+31612345678" if service == "people" else world["ref"]
        r = client.get("/api/v1/fabric/context", params={"q": subject}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert missing in body["coverage"]["missing"]
        assert all(i["service"] != service for i in body["timeline"])
        if service != "people":           # the person is still described, and People's part is still there
            assert body["subject"]["label"] == "Ada Lovelace"
            assert any(i["service"] == "people" for i in body["timeline"])
        if service == "people" and failure == "no_permission":
            assert "Ada" not in r.text and "Lovelace" not in r.text

        r = client.get("/api/v1/fabric/capabilities", headers=h)
        assert r.status_code == 200
        caps = {s["service"]: s for s in r.json()["services"]}
        assert not any(c["available"] for c in caps[service]["provides"]) or failure in ("error", "timeout")
