"""Build a synthetic ~/Library/Messages/chat.db for tests (no real people, numbers or messages).

    python3 fixture.py /tmp/chat.db            # modern macOS layout (nanosecond dates, attributedBody)
    python3 fixture.py /tmp/chat.db --legacy   # old layout (second dates, no edit/unsend columns)

Only the tables and columns the collector reads are created, with the same names and meanings as Apple's.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def typedstream(text: str) -> bytes:
    """An NSAttributedString archive like Messages writes into `attributedBody`."""
    raw = text.encode("utf-8")
    n = len(raw)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x10000:
        length = b"\x81" + n.to_bytes(2, "little")
    else:
        length = b"\x82" + n.to_bytes(4, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject"
            b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + raw +
            b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96"
            b"\x1d__kIMMessagePartAttributeName\x86\x92\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue\x00\x94"
            b"\x84\x01*\x84\x99\x99\x00\x86\x86\x86")


def apple(dt: datetime, legacy: bool = False) -> int:
    s = (dt - APPLE_EPOCH).total_seconds()
    return int(s) if legacy else int(s * 1e9)


def build(path: str, legacy: bool = False, now: datetime | None = None) -> dict:
    """Create the database; returns the GUIDs and ROWIDs tests refer to."""
    now = now or datetime.now(timezone.utc)
    c = sqlite3.connect(path)
    edit_cols = "" if legacy else ", date_edited INTEGER DEFAULT 0, date_retracted INTEGER DEFAULT 0, " \
                                  "associated_message_emoji TEXT, thread_originator_guid TEXT"
    c.executescript(f"""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, country TEXT,
                             service TEXT NOT NULL, uncanonicalized_id TEXT, person_centric_id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, style INTEGER,
                           state INTEGER, account_id TEXT, chat_identifier TEXT, service_name TEXT,
                           room_name TEXT, display_name TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, text TEXT,
                              handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER, date_read INTEGER,
                              date_delivered INTEGER, is_from_me INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0,
                              group_action_type INTEGER DEFAULT 0, cache_has_attachments INTEGER DEFAULT 0,
                              associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,
                              attributedBody BLOB {edit_cols});
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0,
                                        PRIMARY KEY (chat_id, message_id));
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER, UNIQUE (chat_id, handle_id));
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
                                 filename TEXT, mime_type TEXT, transfer_name TEXT, total_bytes INTEGER DEFAULT 0);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER,
                                              UNIQUE (message_id, attachment_id));
    """)
    h = {}
    for key, ident, service in [("ada", "+31612345678", "iMessage"), ("bob", "bob@example.org", "iMessage"),
                                ("cat", "+447700900123", "SMS"), ("bank", "BANK", "SMS")]:
        h[key] = c.execute("INSERT INTO handle (id, service, country) VALUES (?, ?, 'nl')", (ident, service)).lastrowid
    chats = {}
    for key, guid, style, service, name, members in [
            ("ada", "iMessage;-;+31612345678", 45, "iMessage", None, ["ada"]),
            ("group", "iMessage;+;chat000000000000000001", 43, "iMessage", "Lighthouse crew", ["ada", "bob"]),
            ("cat", "SMS;-;+447700900123", 45, "SMS", None, ["cat"]),
            ("bank", "SMS;-;BANK", 45, "SMS", None, ["bank"])]:
        chats[key] = c.execute("INSERT INTO chat (guid, style, service_name, display_name, chat_identifier) "
                               "VALUES (?, ?, ?, ?, ?)", (guid, style, service, name, guid.split(";")[-1])).lastrowid
        for m in members:
            c.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (chats[key], h[m]))
    old = now - timedelta(days=60)
    recent = now - timedelta(days=2)
    rows = []

    def msg(guid, chat, when, *, text=None, body=None, sender=None, me=False, service="iMessage", **extra):
        cols = {"guid": guid, "text": text, "attributedBody": typedstream(body) if body is not None else None,
                "handle_id": h[sender] if sender else 0, "service": service, "date": apple(when, legacy),
                "is_from_me": int(me), **extra}
        if legacy:
            cols = {k: v for k, v in cols.items() if k not in ("date_edited", "date_retracted",
                                                                "associated_message_emoji", "thread_originator_guid")}
        rid = c.execute(f"INSERT INTO message ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        list(cols.values())).lastrowid
        c.execute("INSERT INTO chat_message_join (chat_id, message_id, message_date) VALUES (?, ?, ?)",
                  (chats[chat], rid, cols["date"]))
        rows.append(rid)
        return rid

    msg("G-OLD-1", "ada", old, text="Old plain text message", sender="ada")
    msg("G-OLD-2", "ada", old + timedelta(minutes=3), body="Reply kept only in attributedBody ✨", me=True)
    long_text = "A long note about tides. " * 12            # > 127 bytes: 0x81 length
    msg("G-OLD-3", "group", old + timedelta(hours=1), body=long_text, sender="bob")
    msg("G-OLD-4", "cat", old + timedelta(hours=2), text="Text over SMS", sender="cat", service="SMS")
    att = msg("G-OLD-5", "ada", old + timedelta(hours=3), text="￼", sender="ada", cache_has_attachments=1)
    aid = c.execute("INSERT INTO attachment (guid, filename, mime_type, transfer_name, total_bytes) VALUES "
                    "('A-1', '~/Library/Messages/Attachments/ab/12/IMG_0001.HEIC', 'image/heic', 'IMG_0001.HEIC', 2048)").lastrowid
    c.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (att, aid))
    msg("G-OLD-6", "group", old + timedelta(hours=4), text=None, item_type=2)          # "renamed the group"
    msg("G-TB-1", "ada", old + timedelta(hours=5), text='Loved "Old plain text message"', me=True,
        associated_message_guid="p:0/G-OLD-1", associated_message_type=2000)
    msg("G-TB-2", "group", old + timedelta(hours=6), text=None, sender="ada",
        associated_message_guid="bp:G-OLD-3", associated_message_type=2003)
    msg("G-BANK-1", "bank", recent, text="Your verification code is 739104", sender="bank", service="SMS")
    extra = {} if legacy else {"date_edited": apple(recent + timedelta(minutes=5)), "thread_originator_guid": "G-OLD-1"}
    msg("G-NEW-1", "ada", recent + timedelta(minutes=1), body="See you Friday (edited)", me=True, **extra)
    if not legacy:
        msg("G-NEW-2", "ada", recent + timedelta(minutes=2), text=None, sender="ada",
            date_retracted=apple(recent + timedelta(minutes=4)))
        msg("G-TB-3", "ada", recent + timedelta(minutes=6), text=None, sender="ada",
            associated_message_guid="p:0/G-NEW-1", associated_message_type=2006, associated_message_emoji="🌊")
    c.commit()
    c.close()
    return {"handles": h, "chats": chats, "max_rowid": max(rows)}


def add_message(path: str, guid: str, text: str, when: datetime | None = None, legacy: bool = False) -> int:
    """Append one new incoming message from 'ada' (to test resuming)."""
    c = sqlite3.connect(path)
    when = when or datetime.now(timezone.utc)
    hid = c.execute("SELECT ROWID FROM handle WHERE id = '+31612345678'").fetchone()[0]
    chat = c.execute("SELECT ROWID FROM chat WHERE guid = 'iMessage;-;+31612345678'").fetchone()[0]
    rid = c.execute("INSERT INTO message (guid, text, handle_id, service, date) VALUES (?, ?, ?, 'iMessage', ?)",
                    (guid, text, hid, apple(when, legacy))).lastrowid
    c.execute("INSERT INTO chat_message_join VALUES (?, ?, 0)", (chat, rid))
    c.commit()
    c.close()
    return rid


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    print(build(sys.argv[1], legacy="--legacy" in sys.argv))
