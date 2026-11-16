"""WhatsApp export importer: iOS and Android, four locales, .txt and .zip, idempotent re-imports."""

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import TEST_WS, Browser
from cove import context, db
from cove.auth import policy, tokens
from cove.crm import service as svc
from cove.messages import service as messages
from cove.messages.importers import whatsapp as wa

FIX = Path(__file__).parent / "fixtures" / "whatsapp"


def _text(name):
    return (FIX / name).read_text(encoding="utf-8")


def _utc(m):
    return m["sent_at"].astimezone(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ parsing
def test_ios_uk_export():
    p = wa.parse(_text("ios_en-GB.txt"), title="Ada Lovelace", tz="UTC")
    assert p.order == "DMY" and p.senders == ["Ada Lovelace", "Sam Example"]
    ms = p.messages
    assert _utc(ms[1]) == datetime(2024, 3, 14, 9, 15, 2)
    assert ms[2]["body"] == "Yes, Saturday works.\nBring a flask of tea,\nit'll be windy."
    assert ms[3]["kind"] == "attachment" and ms[3]["attachments"][0]["filename"].endswith("PHOTO-2024-03-14-09-17-05.jpg")
    assert ms[4]["kind"] == "attachment" and ms[4]["attachments"][0]["mime"] == "image/jpeg"
    assert ms[5]["retracted"] and ms[5]["body"] == ""
    assert ms[6]["edited"] and ms[6]["body"] == "See you at the harbour at 10"
    assert ms[7]["kind"] == "call" and ms[7]["sender"] == "Ada Lovelace"       # the LRM before the name is gone


def test_android_us_export_with_12_hour_clock_and_narrow_spaces():
    p = wa.parse(_text("android_en-US.txt"), tz="UTC")
    assert p.order == "MDY"
    ms = p.messages
    assert ms[0]["kind"] == "system" and ms[1]["kind"] == "system"     # encryption notice, group created
    assert _utc(ms[2]) == datetime(2024, 3, 14, 9, 16)
    assert ms[3]["sender"] == "+1 555 010 0199"
    assert ms[4]["body"] == "Me too!\nI'll bring the map."
    assert ms[5]["attachments"][0]["filename"] == "IMG-20240314-WA0001.jpg" and ms[5]["body"] == "Here's the route"
    assert _utc(ms[5]) == datetime(2024, 3, 14, 13, 5)
    assert ms[6]["kind"] == "attachment" and ms[7]["kind"] == "attachment"
    assert _utc(ms[8]) == datetime(2024, 3, 14, 0, 30)                 # 12:30 AM is just after midnight


def test_german_and_dutch_exports():
    de = wa.parse(_text("ios_de-DE.txt"), tz="UTC")
    assert de.order == "DMY" and _utc(de.messages[2]) == datetime(2024, 4, 13, 7, 59, 59)
    assert de.messages[2]["attachments"][0]["filename"].endswith(".opus") and de.messages[3]["retracted"]
    nl = wa.parse(_text("android_nl-NL.txt"), tz="UTC")
    assert nl.order == "DMY" and _utc(nl.messages[0]) == datetime(2024, 6, 5, 20, 10)
    assert nl.messages[3]["kind"] == "attachment"


def test_times_are_read_in_the_local_timezone():
    p = wa.parse(_text("android_nl-NL.txt"), tz="Europe/Amsterdam")
    assert _utc(p.messages[0]) == datetime(2024, 6, 5, 18, 10)          # 20:10 CEST


def test_not_an_export_is_refused():
    with pytest.raises(svc.Invalid):
        wa.parse("just some text\nwith no dates\n")


def test_title_from_file_names():
    assert wa.title_from_filename("WhatsApp Chat with Ada Lovelace.txt") == "Ada Lovelace"
    assert wa.title_from_filename("WhatsApp Chat - Harbour walkers.zip") == "Harbour walkers"
    assert wa.title_from_filename("_chat.txt") is None


def test_external_ids_are_stable_and_ordinal_aware():
    p = wa.parse(_text("android_en-US.txt"), tz="UTC")
    a = wa.to_batches(p, my_name="Sam Example")[0]["messages"]
    b = wa.to_batches(wa.parse(_text("android_en-US.txt"), tz="UTC"), my_name="Sam Example")[0]["messages"]
    assert [m["external_id"] for m in a] == [m["external_id"] for m in b]
    assert a[6]["external_id"] != a[7]["external_id"]     # two identical "<Media omitted>" in the same minute
    assert a[4]["is_from_me"] and a[4]["sender"] is None


# ------------------------------------------------------------------ importing
@pytest.fixture()
def user(database):
    return context.Principal(actor="web:t", workspace=TEST_WS, scopes=policy.ALL)


def _zip(txt: str, media: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("_chat.txt", txt)
        for name, data in media.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_import_zip_is_idempotent_and_newer_exports_only_add(user):
    svc.create_contact(svc.build_card(full_name="Ada Lovelace"), "web:t")
    txt = _text("ios_en-GB.txt")
    data = _zip(txt, {"00000003-PHOTO-2024-03-14-09-17-05.jpg": b"x" * 321})
    r = wa.import_export("WhatsApp Chat - Ada Lovelace.zip", data, "web:t")
    assert r["title"] == "Ada Lovelace" and r["accepted"] == 9 and r["rejected"] == 0
    again = wa.import_export("WhatsApp Chat - Ada Lovelace.zip", data, "web:t")
    assert again["accepted"] == 0 and again["duplicates"] == 9
    newer = txt + "[16/03/2024, 08:00:00] Ada Lovelace: One more thing\n"
    r = wa.import_export("WhatsApp Chat - Ada Lovelace.zip", _zip(newer, {}), "web:t")
    assert r["accepted"] == 1
    with db.conn() as c:
        conv = c.execute("SELECT kind, message_count FROM messages.conversations").fetchall()
        mine = c.execute("SELECT count(*) AS n FROM messages.messages WHERE is_from_me").fetchone()["n"]
        size = c.execute("SELECT size FROM messages.attachments WHERE filename LIKE '%PHOTO%'").fetchone()["size"]
        otp = c.execute("SELECT is_sensitive FROM messages.messages WHERE body LIKE '%verification%'").fetchone()
        ada = c.execute("SELECT resolution, contact_ref, is_me FROM messages.handles WHERE value = 'name:Ada Lovelace'").fetchone()
        sam = c.execute("SELECT is_me FROM messages.handles WHERE value = 'name:Sam Example'").fetchone()
    assert [dict(x) for x in conv] == [{"kind": "direct", "message_count": 10}]
    assert mine == 3 and size == 321 and otp["is_sensitive"] and sam["is_me"]
    assert ada["resolution"] == "suggested" and ada["contact_ref"] is None   # names are never auto-linked
    queue = messages.list_handles("suggested")["items"]
    assert queue[0]["suggestions"][0]["label"] == "Ada Lovelace"


def test_group_chats_and_phone_number_senders(user):
    svc.create_contact(svc.build_card(full_name="Someone Unsaved", phones=["+1 555 010 0199"]), "web:t")
    r = wa.import_export("WhatsApp Chat with Harbour walkers.txt", _text("android_en-US.txt").encode(), "web:t",
                         my_name="Sam Example")
    assert r["accepted"] == 9
    with db.conn() as c:
        conv = c.execute("SELECT kind, title FROM messages.conversations").fetchone()
        phone = c.execute("SELECT value, resolution FROM messages.handles WHERE value LIKE '+%'").fetchone()
    assert dict(conv) == {"kind": "group", "title": "Harbour walkers"}
    assert dict(phone) == {"value": "+15550100199", "resolution": "auto"}     # a number can link by itself


def test_upload_api_with_a_session_or_an_ingest_token(user):
    b = Browser()
    r = b.client.post("/api/v1/messages/ingest/whatsapp", headers={"origin": "https://cove.test", "x-csrf-token": b.csrf},
                      files={"file": ("WhatsApp Chat with Noor Voorbeeld.txt", _text("android_nl-NL.txt").encode())},
                      data={"my_name": "Sam Example"})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 4 and r.json()["date_order"] == "DMY"
    with context.use(user):
        tok = tokens.create("phone", "web:t", kind="ingest")["token"]
    r = b.client.post("/api/v1/messages/ingest/whatsapp", headers={"Authorization": f"Bearer {tok}"},
                      files={"file": ("chat.txt", _text("ios_de-DE.txt").encode())}, data={"date_order": "DMY"})
    assert r.status_code == 200 and r.json()["accepted"] == 4
    r = b.client.post("/api/v1/messages/ingest/whatsapp", headers={"Authorization": f"Bearer {tok}"},
                      files={"file": ("chat.txt", b"nothing here")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid"
