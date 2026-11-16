#!/usr/bin/env python3
"""cove iMessage/SMS collector for macOS: sends new messages from ~/Library/Messages/chat.db to cove.

One file, Python 3 standard library only (works with the python3 that ships with macOS), so there is nothing
to install. It never changes chat.db: each run copies the database (and its -wal/-shm) into a private temp
folder and reads the copy. It sends batches to cove's ingest API (docs/messages.md) and resumes where it left
off (the server keeps the high-water mark), re-reading the last few days each run to pick up edits, unsends
and tapbacks.

    cove_imessage.py              # one run: new messages since last time (launchd runs this every 10 minutes)
    cove_imessage.py --backfill   # everything, with progress
    cove_imessage.py --dry-run    # read and count, send nothing (prints counts only, never message text)
    cove_imessage.py --check      # can I read chat.db (Full Disk Access) and reach cove with my token?

Config: ~/.config/cove-collector/config.toml (url = "https://<your cove host>"). The ingest token lives in
the macOS Keychain (service "cove-collector", account "ingest-token"), never in the config file.
Message text is never printed or logged.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

VERSION = "1.0.0"
FORMAT = "cove.messages.ingest/v1"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
DEFAULTS = {
    "url": "",
    "source_key": "imessage-mac",
    "label": "iMessage on my Mac",
    "chat_db": "~/Library/Messages/chat.db",
    "rescan_days": 7,           # re-read this many days each run, for edits, unsends and tapbacks
    "initial_days": 30,         # first normal run without --backfill: how far back to start
    "batch_size": 500,          # messages per request (the server takes up to 1,000)
    "keychain_service": "cove-collector",
    "keychain_account": "ingest-token",
    "timeout": 60,
}
CONFIG_PATH = "~/.config/cove-collector/config.toml"
SERVICES = {"imessage": "imessage", "sms": "sms", "rcs": "rcs"}
# associated_message_type: 2000-2006 add a tapback, 3000-3006 remove it
TAPBACKS = {0: "love", 1: "like", 2: "dislike", 3: "laugh", 4: "emphasize", 5: "question", 6: "emoji"}


class CollectorError(RuntimeError):
    """Something the user can fix; the message says what (and never contains message text)."""


# ================================================================ config and secrets
def _parse_toml(text: str) -> dict:
    """A tiny TOML subset (key = "string" | number | true/false, # comments) for Pythons without tomllib."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "[")):
            continue
        if "=" not in line:
            raise CollectorError(f"config: can't read the line starting {line[:20]!r}")
        key, value = (x.strip() for x in line.split("=", 1))
        if value[:1] in ('"', "'"):
            end = value.find(value[0], 1)
            out[key] = value[1:end] if end > 0 else value[1:]
            continue
        value = value.split("#", 1)[0].strip()
        if value in ("true", "false"):
            out[key] = value == "true"
        else:
            try:
                out[key] = int(value)
            except ValueError:
                out[key] = float(value)
    return out


def load_config(path: str | None = None) -> dict:
    p = os.path.expanduser(path or os.environ.get("COVE_COLLECTOR_CONFIG") or CONFIG_PATH)
    cfg = dict(DEFAULTS)
    if os.path.exists(p):
        with open(p, "rb") as f:
            text = f.read().decode()
        try:
            import tomllib  # Python 3.11+
            cfg.update(tomllib.loads(text))
        except ImportError:
            cfg.update(_parse_toml(text))
    if "token" in cfg:
        raise CollectorError("don't put the token in config.toml; store it in the Keychain (see README)")
    cfg["url"] = str(cfg.get("url") or "").rstrip("/")
    return cfg


def get_token(cfg: dict) -> str:
    """The ingest token: from the Keychain (or COVE_INGEST_TOKEN, for testing)."""
    tok = os.environ.get("COVE_INGEST_TOKEN")
    if tok:
        return tok.strip()
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", cfg["keychain_service"],
                              "-a", cfg["keychain_account"], "-w"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise CollectorError("the `security` tool isn't available (this collector runs on macOS)") from None
    if out.returncode != 0 or not out.stdout.strip():
        raise CollectorError("no ingest token in the Keychain. Add it with: security add-generic-password -U "
                             f"-s {cfg['keychain_service']} -a {cfg['keychain_account']} -w")
    return out.stdout.strip()


# ================================================================ reading chat.db
def snapshot(db_path: str, workdir: str, attempts: int = 5) -> str:
    """Copy chat.db and its -wal/-shm into `workdir` while nothing changes underneath, and return the copy.

    Messages.app keeps writing to the live database; reading a copy means we never lock it or see a torn
    state. The copy is retried when the files changed while being copied."""
    src = os.path.expanduser(db_path)
    if not os.path.exists(src):
        raise CollectorError(f"{src} not found (or this Python has no Full Disk Access)")
    dst = os.path.join(workdir, "chat.db")

    def stamp():
        return [(os.stat(p).st_size, os.stat(p).st_mtime_ns) if os.path.exists(p) else None
                for p in (src, src + "-wal")]
    for _ in range(attempts):
        before = stamp()
        try:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(src + suffix):
                    shutil.copyfile(src + suffix, dst + suffix)
                elif os.path.exists(dst + suffix):
                    os.remove(dst + suffix)
        except PermissionError:
            raise CollectorError("can't read chat.db: give Full Disk Access to the Python that runs this "
                                 "(see README › Full Disk Access)") from None
        if stamp() == before:
            return dst
        time.sleep(0.5)
    return dst      # still busy after several tries: the copied WAL keeps it consistent, and the next run re-reads


def open_copy(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def apple_time(value) -> str | None:
    """chat.db dates: seconds since 2001-01-01 on old macOS, nanoseconds on newer; 0/NULL means none."""
    if not value:
        return None
    v = int(value)
    seconds = v / 1e9 if abs(v) > 10**11 else v
    return (APPLE_EPOCH + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def decode_attributed_body(blob) -> str | None:
    """The plain text inside a message's `attributedBody` (an NSAttributedString in NSArchiver typedstream form).

    macOS 13+ often leaves `text` NULL and keeps the body only here. Around the string the stream reads
    ... "NSString" 0x01 0x94 0x84 0x01 '+' <length> <UTF-8 bytes> ...
    where <length> is one byte (< 0x80), or 0x81 + 2 bytes little-endian, or 0x82 + 4 bytes little-endian."""
    if not blob:
        return None
    data = bytes(blob)
    for marker in (b"NSString", b"NSMutableString"):
        i = data.find(marker)
        if i < 0:
            continue
        j = data.find(b"+", i + len(marker), i + len(marker) + 12)
        if j < 0:
            continue
        p = j + 1
        if p >= len(data):
            return None
        n = data[p]
        if n == 0x81:
            length, p = int.from_bytes(data[p + 1:p + 3], "little"), p + 3
        elif n == 0x82:
            length, p = int.from_bytes(data[p + 1:p + 5], "little"), p + 5
        elif n < 0x80:
            length, p = n, p + 1
        else:
            return None
        chunk = data[p:p + length]
        if len(chunk) < length:
            return None
        return chunk.decode("utf-8", errors="replace")
    return None


def _body(row) -> str | None:
    text = row["text"]
    if text is None:
        text = decode_attributed_body(row["attributedBody"])
    if text is None:
        return None
    text = text.replace("￼", "").strip()      # attachment placeholders
    return text or None


def _service(name) -> str:
    return SERVICES.get((name or "").lower(), "other")


def _target_guid(assoc):
    """`p:0/GUID` (part 0 of a message) or `bp:GUID` -> GUID."""
    if not assoc:
        return None
    if "/" in assoc:
        return assoc.split("/", 1)[1]
    return assoc.split(":", 1)[1] if ":" in assoc else assoc


class Reader:
    """Turns chat.db rows into ingest items."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.mcols = columns(conn, "message")
        self.ccols = columns(conn, "chat")
        self.handles = {r["ROWID"]: dict(r) for r in conn.execute("SELECT ROWID, id, service FROM handle")}
        self._chats: dict = {}

    def _opt(self, col: str) -> str:
        return f"m.{col}" if col in self.mcols else f"NULL AS {col}"

    def rows(self, after_rowid: int, since_date):
        """Message rows with ROWID > after_rowid, or sent since `since_date` (the re-scan window), by ROWID."""
        cols = ", ".join([
            "m.ROWID", "m.guid", "m.text", self._opt("attributedBody"), "m.handle_id", self._opt("service"),
            "m.date", self._opt("date_edited"), self._opt("date_retracted"), "m.is_from_me",
            self._opt("associated_message_guid"), self._opt("associated_message_type"),
            self._opt("associated_message_emoji"), self._opt("item_type"), self._opt("thread_originator_guid"),
            self._opt("cache_has_attachments"), "cmj.chat_id"])
        where, params = "m.ROWID > ?", [after_rowid]
        if since_date:
            apple = int((since_date - APPLE_EPOCH).total_seconds())
            where += " OR (m.date >= ? AND m.date < ?) OR m.date >= ?"
            params += [apple, 10**11, apple * 10**9]       # seconds (old macOS) or nanoseconds (new)
        return self.conn.execute(
            f"SELECT {cols} FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            f"WHERE {where} ORDER BY m.ROWID", params)

    def chat(self, chat_id: int) -> dict:
        if chat_id not in self._chats:
            style = "style" if "style" in self.ccols else "NULL AS style"
            c = self.conn.execute(f"SELECT ROWID, guid, service_name, display_name, {style} FROM chat WHERE ROWID = ?",
                                  (chat_id,)).fetchone()
            parts = [self.handles[r["handle_id"]] for r in
                     self.conn.execute("SELECT handle_id FROM chat_handle_join WHERE chat_id = ?", (chat_id,))
                     if r["handle_id"] in self.handles]
            platform = _service(c["service_name"])
            self._chats[chat_id] = {
                "external_id": c["guid"], "platform": platform,
                "kind": "group" if (c["style"] == 43 or len(parts) > 1) else "direct",
                "title": c["display_name"] or None,
                "participants": [{"platform": self._platform(h, platform), "value": h["id"]} for h in parts],
            }
        return self._chats[chat_id]

    @staticmethod
    def _platform(handle: dict, fallback: str) -> str:
        p = _service(handle.get("service"))
        return p if p != "other" else fallback

    def attachments(self, rowid: int) -> list:
        out = []
        for a in self.conn.execute(
                "SELECT a.filename, a.mime_type, a.transfer_name, a.total_bytes FROM attachment a "
                "JOIN message_attachment_join j ON j.attachment_id = a.ROWID WHERE j.message_id = ?", (rowid,)):
            name = a["transfer_name"] or os.path.basename(a["filename"] or "") or None   # never the local path
            out.append({"filename": name, "mime": a["mime_type"], "size": a["total_bytes"]})
        return out

    def convert(self, row):
        """('message' | 'reaction' | 'skip', item)."""
        chat = self.chat(row["chat_id"])
        h = self.handles.get(row["handle_id"])
        sender = None if row["is_from_me"] or not h else {"platform": self._platform(h, chat["platform"]),
                                                          "value": h["id"]}
        atype = row["associated_message_type"] or 0
        if 2000 <= atype <= 3006 and row["associated_message_guid"]:
            kind = TAPBACKS.get(atype % 1000, "other")
            return "reaction", {"target": _target_guid(row["associated_message_guid"]), "sender": sender,
                                "is_from_me": bool(row["is_from_me"]), "type": kind,
                                "emoji": row["associated_message_emoji"] if kind == "emoji" else None,
                                "removed": atype >= 3000, "at": apple_time(row["date"])}
        if atype or not apple_time(row["date"]):      # stickers placed on bubbles, and undated rows
            return "skip", {}
        body = _body(row)
        atts = self.attachments(row["ROWID"]) if row["cache_has_attachments"] else []
        retracted = bool(row["date_retracted"])
        kind = "system" if (row["item_type"] or 0) != 0 else ("attachment" if atts and not body else "text")
        msg = {"external_id": row["guid"], "conversation": chat["external_id"], "sender": sender,
               "is_from_me": bool(row["is_from_me"]), "sent_at": apple_time(row["date"]),
               "edited_at": apple_time(row["date_edited"]), "retracted": retracted,
               "body": None if retracted else body, "kind": kind, "reply_to": row["thread_originator_guid"] or None,
               "platform_meta": {"service": row["service"] or chat["platform"]}}
        if atts:
            msg["attachments"] = atts
        return "message", msg


def build_batches(conn: sqlite3.Connection, cfg: dict, high_water, backfill: bool = False, now=None):
    """Yield ingest batches, each carrying the high-water mark the server stores once it's accepted."""
    now = now or datetime.now(timezone.utc)
    reader = Reader(conn)
    hw = int((high_water or {}).get("rowid") or 0)
    if backfill:
        after, since = 0, None
    elif hw:
        after, since = hw, now - timedelta(days=int(cfg["rescan_days"]))
    else:                                              # first run without --backfill: only the recent past
        after, since = 2**62, now - timedelta(days=int(cfg["initial_days"]))
    size = max(1, min(int(cfg["batch_size"]), 1000))
    source = {"key": cfg["source_key"], "platform": "imessage", "label": cfg["label"],
              "collector": f"cove-imessage/{VERSION}"}

    def batch(msgs, reactions, chats, top):
        return {"format": FORMAT, "source": source, "conversations": list(chats.values()), "messages": msgs,
                "reactions": reactions, "high_water": {"rowid": top, "collector": VERSION}}
    msgs, reactions, chats, top = [], [], {}, hw
    for row in reader.rows(after, since):
        what, item = reader.convert(row)
        top = max(top, row["ROWID"])
        if what == "skip":
            continue
        chat = reader.chat(row["chat_id"])
        chats[chat["external_id"]] = chat
        (msgs if what == "message" else reactions).append(item)
        if len(msgs) >= size or len(reactions) >= 4000:
            yield batch(msgs, reactions, chats, top)
            msgs, reactions, chats = [], [], {}
    if msgs or reactions or top > hw:
        yield batch(msgs, reactions, chats, top)


# ================================================================ talking to cove
class Client:
    def __init__(self, url: str, token: str, timeout: int = 60):
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise CollectorError("url must be your cove's https:// address (config.toml)")
        self.url, self.token, self.timeout = url, token, timeout

    def _req(self, method: str, path: str, body=None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(4):
            req = urllib.request.Request(self.url + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "User-Agent": f"cove-imessage/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code >= 500 or e.code == 429:
                    time.sleep(2 ** attempt * 5)
                    continue
                try:
                    err = json.loads(e.read()).get("error", {})
                except ValueError:
                    err = {}
                raise CollectorError(f"cove said {e.code} {err.get('code', '')}: {err.get('message', e.reason)}") from None
            except (urllib.error.URLError, OSError) as e:
                if attempt == 3:
                    raise CollectorError(f"can't reach {self.url}: {getattr(e, 'reason', e)}") from None
                time.sleep(2 ** attempt * 5)
        raise CollectorError("cove kept failing; the next run will try again")

    def state(self, source_key: str) -> dict:
        from urllib.parse import quote
        return self._req("GET", f"/api/v1/messages/ingest/state?source={quote(source_key)}")

    def send(self, batch: dict) -> dict:
        return self._req("POST", "/api/v1/messages/ingest/batch", batch)


def run(cfg: dict, client, *, backfill: bool = False, dry_run: bool = False, out=None) -> dict:
    """One collection run. Prints counts only, never message text. Returns the totals."""
    out = out or sys.stdout
    state = client.state(cfg["source_key"]) if client else {"high_water": None}
    totals = {"batches": 0, "messages": 0, "reactions": 0, "accepted": 0, "updated": 0, "duplicates": 0,
              "rejected": 0}
    with tempfile.TemporaryDirectory(prefix="cove-collector-") as tmp:
        os.chmod(tmp, 0o700)
        conn = open_copy(snapshot(cfg["chat_db"], tmp))
        try:
            top = conn.execute("SELECT coalesce(max(ROWID), 0) FROM message").fetchone()[0] or 1
            for b in build_batches(conn, cfg, state.get("high_water"), backfill=backfill):
                totals["batches"] += 1
                totals["messages"] += len(b["messages"])
                totals["reactions"] += len(b["reactions"])
                if not dry_run:
                    r = client.send(b)
                    for k in ("accepted", "updated", "duplicates"):
                        totals[k] += r.get(k, 0)
                    totals["rejected"] += len(r.get("rejected") or [])
                if backfill:
                    print(f"  {100 * b['high_water']['rowid'] / top:5.1f}%  {totals['messages']:>8} messages  "
                          f"{totals['reactions']:>7} tapbacks", file=out, flush=True)
        finally:
            conn.close()
    if dry_run:
        print(f"cove-imessage {VERSION}: would send {totals['messages']} messages and {totals['reactions']} "
              f"tapbacks in {totals['batches']} batches", file=out)
    else:
        print(f"cove-imessage {VERSION}: sent {totals['messages']} messages and {totals['reactions']} tapbacks in "
              f"{totals['batches']} batches (new {totals['accepted']}, changed {totals['updated']}, unchanged "
              f"{totals['duplicates']}, rejected {totals['rejected']})", file=out)
    return totals


def check(cfg: dict, out=None) -> int:
    """Full Disk Access, the token and the server, each reported on one line."""
    out = out or sys.stdout
    ok = True
    try:
        with tempfile.TemporaryDirectory(prefix="cove-collector-") as tmp:
            conn = open_copy(snapshot(cfg["chat_db"], tmp))
            n = conn.execute("SELECT count(*) FROM message").fetchone()[0]
            conn.close()
        print(f"ok  chat.db readable ({n} messages): Full Disk Access works", file=out)
    except (CollectorError, sqlite3.Error) as e:
        ok = False
        print(f"no  chat.db: {e}", file=out)
    try:
        st = Client(cfg["url"], get_token(cfg), cfg["timeout"]).state(cfg["source_key"])
        print(f"ok  cove reachable at {cfg['url']} with the Keychain token; last ingest: "
              f"{st.get('last_ingest_at') or 'never'}", file=out)
    except CollectorError as e:
        ok = False
        print(f"no  cove: {e}", file=out)
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Send iMessage/SMS history from this Mac to cove.")
    ap.add_argument("--config", help=f"config file (default {CONFIG_PATH})")
    ap.add_argument("--backfill", action="store_true", help="send the whole history, with progress")
    ap.add_argument("--dry-run", action="store_true", help="read and count only; send nothing")
    ap.add_argument("--check", action="store_true", help="check Full Disk Access, the token and the server")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.check:
            return check(cfg)
        import fcntl
        lock = os.path.join(tempfile.gettempdir(), f"cove-collector-{os.getuid()}.lock")
        with open(lock, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("cove-imessage: a previous run is still going; skipping this one")
                return 0
            client = None if (args.dry_run and not cfg["url"]) else Client(cfg["url"], get_token(cfg), cfg["timeout"])
            run(cfg, client, backfill=args.backfill, dry_run=args.dry_run)
        return 0
    except CollectorError as e:
        print(f"cove-imessage: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
