"""The iMessage collector (collectors/imessage) against a synthetic chat.db, end to end through the ingest API."""

import importlib.util
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_WS
from cove import context, db
from cove.auth import policy, tokens

ROOT = Path(os.environ.get("COVE_COLLECTORS_DIR", Path(__file__).resolve().parents[2] / "collectors")) / "imessage"
if not ROOT.exists():
    pytest.skip("collectors/ not mounted", allow_module_level=True)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


col = _load("cove_imessage")
fixture = _load("fixture")
SCHEMA = json.loads((Path(os.environ.get("COVE_SCHEMA_DIR", Path(__file__).resolve().parents[2] / "schemas"))
                     / "contracts" / "messages-ingest.v1.json").read_text())


# ------------------------------------------------------------------ the typedstream decoder
@pytest.mark.parametrize("text", ["hi", "Reply kept only in attributedBody ✨", "x" * 127, "y" * 128,
                                  "A long note about tides. " * 12, "ü" * 40000, "z" * 70000, ""])
def test_attributed_body_round_trips(text):
    assert col.decode_attributed_body(fixture.typedstream(text)) == text


def test_attributed_body_length_markers():
    blob = fixture.typedstream("a" * 300)
    i = blob.index(b"+") + 1
    assert blob[i] == 0x81 and int.from_bytes(blob[i + 1:i + 3], "little") == 300
    blob = fixture.typedstream("a" * 70000)
    i = blob.index(b"+") + 1
    assert blob[i] == 0x82


@pytest.mark.parametrize("blob", [None, b"", b"no string here", b"NSString\x01\x94\x84\x01+", b"NSString\x01+\x10short",
                                  b"NSString\x01+\x81\xff"])
def test_attributed_body_garbage_is_none(blob):
    assert col.decode_attributed_body(blob) is None


def test_mutable_string_variant():
    blob = fixture.typedstream("mutable").replace(b"\x08NSString", b"\x0fNSMutableString")
    assert col.decode_attributed_body(blob) == "mutable"


# ------------------------------------------------------------------ small pieces
def test_apple_time_seconds_and_nanoseconds():
    when = datetime(2024, 5, 1, 9, 30, tzinfo=timezone.utc)
    assert col.apple_time(fixture.apple(when)) == "2024-05-01T09:30:00Z"
    assert col.apple_time(fixture.apple(when, legacy=True)) == "2024-05-01T09:30:00Z"
    assert col.apple_time(0) is None and col.apple_time(None) is None


def test_config_without_tomllib_and_no_token_in_files(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('# comment\nurl = "https://cove.example.test/"  # mine\nrescan_days = 3\nlabel = \'Mac # 2\'\n')
    assert col._parse_toml(p.read_text()) == {"url": "https://cove.example.test/", "rescan_days": 3, "label": "Mac # 2"}
    cfg = col.load_config(str(p))
    assert cfg["url"] == "https://cove.example.test" and cfg["rescan_days"] == 3 and cfg["batch_size"] == 500
    p.write_text('url = "https://x.test"\ntoken = "cove_ing_nope"\n')
    with pytest.raises(col.CollectorError):
        col.load_config(str(p))
    monkeypatch.setenv("COVE_INGEST_TOKEN", "cove_ing_from_env")
    assert col.get_token(cfg) == "cove_ing_from_env"


def test_client_refuses_plain_http():
    with pytest.raises(col.CollectorError):
        col.Client("http://cove.example.test", "t")


def test_snapshot_copies_wal_and_never_touches_the_original(tmp_path):
    live = tmp_path / "live" / "chat.db"
    live.parent.mkdir()
    fixture.build(str(live))
    c = sqlite3.connect(live)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("INSERT INTO handle (id, service) VALUES ('wal-only@example.org', 'iMessage')")
    c.commit()                                   # still open: the row lives only in chat.db-wal
    before = live.read_bytes()
    work = tmp_path / "work"
    work.mkdir()
    copy = col.snapshot(str(live), str(work))
    conn = col.open_copy(copy)
    assert conn.execute("SELECT count(*) FROM handle WHERE id = 'wal-only@example.org'").fetchone()[0] == 1
    assert live.read_bytes() == before
    c.close()


# ------------------------------------------------------------------ batches
@pytest.fixture()
def chat_db(tmp_path):
    p = tmp_path / "chat.db"
    fixture.build(str(p))
    return p


def _cfg(path, **kw):
    return {**col.DEFAULTS, "url": "https://cove.example.test", "chat_db": str(path), **kw}


def test_batches_match_the_ingest_contract(chat_db):
    conn = col.open_copy(str(chat_db))
    batches = list(col.build_batches(conn, _cfg(chat_db, batch_size=3), None, backfill=True))
    assert len(batches) >= 3
    for b in batches:
        jsonschema.Draft202012Validator(SCHEMA).validate(b)
        assert len(b["messages"]) <= 3
    msgs = {m["external_id"]: m for b in batches for m in b["messages"]}
    reactions = [r for b in batches for r in b["reactions"]]
    assert msgs["G-OLD-2"]["body"] == "Reply kept only in attributedBody ✨" and msgs["G-OLD-2"]["is_from_me"]
    assert msgs["G-OLD-3"]["body"].startswith("A long note about tides.")
    assert msgs["G-OLD-4"]["sender"] == {"platform": "sms", "value": "+447700900123"}
    assert msgs["G-OLD-5"]["kind"] == "attachment" and msgs["G-OLD-5"]["body"] is None
    assert msgs["G-OLD-5"]["attachments"] == [{"filename": "IMG_0001.HEIC", "mime": "image/heic", "size": 2048}]
    assert msgs["G-OLD-6"]["kind"] == "system"
    assert msgs["G-NEW-1"]["edited_at"] and msgs["G-NEW-1"]["reply_to"] == "G-OLD-1"
    assert msgs["G-NEW-2"]["retracted"] and msgs["G-NEW-2"]["body"] is None
    assert {(r["target"], r["type"], r["is_from_me"]) for r in reactions} == {
        ("G-OLD-1", "love", True), ("G-OLD-3", "laugh", False), ("G-NEW-1", "emoji", False)}
    assert "G-TB-1" not in msgs
    convs = {c["external_id"]: c for b in batches for c in b["conversations"]}
    assert convs["iMessage;+;chat000000000000000001"]["title"] == "Lighthouse crew"
    assert convs["iMessage;+;chat000000000000000001"]["kind"] == "group"
    assert batches[-1]["high_water"]["rowid"] == max(b["high_water"]["rowid"] for b in batches)


def test_legacy_layout_still_reads(tmp_path):
    p = tmp_path / "old.db"
    fixture.build(str(p), legacy=True)
    batches = list(col.build_batches(col.open_copy(str(p)), _cfg(p), None, backfill=True))
    msgs = {m["external_id"]: m for b in batches for m in b["messages"]}
    assert msgs["G-OLD-1"]["sent_at"].endswith("Z") and msgs["G-NEW-1"]["edited_at"] is None


def test_first_run_without_backfill_only_sends_the_recent_past(chat_db):
    batches = list(col.build_batches(col.open_copy(str(chat_db)), _cfg(chat_db), None))
    ids = {m["external_id"] for b in batches for m in b["messages"]}
    assert ids == {"G-BANK-1", "G-NEW-1", "G-NEW-2"}


# ------------------------------------------------------------------ end to end through the API
class ApiClient:
    """The collector's Client, talking to the app in-process."""

    def __init__(self, token):
        from cove.web.app import app
        self.http = TestClient(app, base_url="https://cove.test")
        self.h = {"Authorization": f"Bearer {token}"}
        self.sent = []

    def state(self, key):
        r = self.http.get("/api/v1/messages/ingest/state", params={"source": key}, headers=self.h)
        assert r.status_code == 200, r.text
        return r.json()

    def send(self, batch):
        self.sent.append(batch)
        r = self.http.post("/api/v1/messages/ingest/batch", content=json.dumps(batch), headers=self.h)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture()
def api(database):
    with context.use(context.Principal(actor="web:t", workspace=TEST_WS, scopes=policy.ALL)):
        return ApiClient(tokens.create("mac", "web:t", kind="ingest")["token"])


def test_end_to_end_backfill_then_resume(chat_db, api):
    out = io.StringIO()
    t = col.run(_cfg(chat_db, batch_size=4), api, backfill=True, out=out)
    assert t["accepted"] == 9 and t["rejected"] == 0
    assert "%" in out.getvalue() and "lighthouse" not in out.getvalue().lower()
    assert "verification" not in out.getvalue()
    with db.conn() as c:
        n = c.execute("SELECT count(*) AS n FROM messages.messages").fetchone()["n"]
        edited = c.execute("SELECT body, edited_at, reply_to_id FROM messages.messages WHERE external_id = 'G-NEW-1'").fetchone()
        unsent = c.execute("SELECT body, retracted FROM messages.messages WHERE external_id = 'G-NEW-2'").fetchone()
        loved = c.execute("SELECT reactions FROM messages.messages WHERE external_id = 'G-OLD-1'").fetchone()
        otp = c.execute("SELECT is_sensitive FROM messages.messages WHERE external_id = 'G-BANK-1'").fetchone()
        group = c.execute("SELECT title, kind FROM messages.conversations WHERE title IS NOT NULL").fetchone()
        hw = c.execute("SELECT high_water FROM messages.sources WHERE key = 'imessage-mac'").fetchone()["high_water"]
    assert n == 9 and edited["body"] == "See you Friday (edited)" and edited["edited_at"] and edited["reply_to_id"]
    assert unsent["retracted"] and unsent["body"] is None
    assert [v["type"] for v in loved["reactions"].values()] == ["love"]
    assert otp["is_sensitive"] and dict(group) == {"title": "Lighthouse crew", "kind": "group"}

    # a new message arrives; the next scheduled run sends it plus the re-scan window, nothing older
    fixture.add_message(str(chat_db), "G-NEXT-1", "Another one")
    api.sent.clear()
    t = col.run(_cfg(chat_db), api, out=io.StringIO())
    sent = {m["external_id"] for b in api.sent for m in b["messages"]}
    assert "G-NEXT-1" in sent and "G-OLD-1" not in sent
    assert t["accepted"] == 1 and t["updated"] == 0
    with db.conn() as c:
        hw2 = c.execute("SELECT high_water FROM messages.sources WHERE key = 'imessage-mac'").fetchone()["high_water"]
    assert hw2["rowid"] > hw["rowid"]

    # running again changes nothing
    t = col.run(_cfg(chat_db), api, out=io.StringIO())
    assert t["accepted"] == 0 and t["updated"] == 0


def test_dry_run_sends_nothing_and_prints_only_counts(chat_db, api, capsys):
    t = col.run(_cfg(chat_db), None, backfill=True, dry_run=True)
    printed = capsys.readouterr().out
    assert t["messages"] == 9 and "would send 9 messages" in printed
    assert "Friday" not in printed and "tides" not in printed
    with db.conn() as c:
        assert c.execute("SELECT count(*) AS n FROM messages.messages").fetchone()["n"] == 0
