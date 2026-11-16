"""WhatsApp "Export chat" importer: the .txt (or .zip, with or without media) from iOS or Android.

- Line formats: iOS `[31/12/2024, 21:05:33] Name: text`, Android `31/12/2024, 21:05 - Name: text`, in any
  locale's date order (detected from the file: a first part above 12 means day-first, a second part above 12
  month-first; otherwise `.`/`-` dates and 24-hour clocks mean day-first), 12- or 24-hour clocks
  (`PM`, `p.m.`, with the narrow no-break space iOS uses), multi-line messages, system lines, `<attached: …>`,
  `… (file attached)`, "image omitted"/`<Media omitted>`, deleted and edited markers, and the invisible
  LRM/RLM marks WhatsApp sprinkles in.
- Times are local to the phone that exported; they are read in COVE_TIMEZONE (or `tz`).
- Senders are display names (or a phone number for people not in the phone's contacts), so they become
  `name:<display name>` handles (or E.164). Name matches to People are only ever suggestions.
- External ids are a stable hash of (conversation, time, sender, text, position among same-second messages),
  so importing a newer export of the same chat only adds what's new.
- Nothing here talks to WhatsApp: no unofficial clients or bridges.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from cove.crm.service import LOCAL_TZ, Invalid
from cove.messages.normalize import clean_name

MAX_TEXT = 200 * 1024 * 1024      # biggest chat .txt we'll read out of an export (zip-bomb guard)
SOURCE = {"key": "whatsapp-export", "platform": "whatsapp", "label": "WhatsApp exports", "collector": "whatsapp-export/1"}

_INVISIBLE = re.compile(r"[‎‏‪-‮⁦-⁩﻿]")
_SPACES = re.compile(r"[    ]")
_DATE = r"(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4})"
_TIME = r"(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[APap]\.?\s?[Mm]\.?)?)"
_IOS = re.compile(rf"^\[{_DATE},?\s{_TIME}\]\s(?P<rest>.*)$")
_ANDROID = re.compile(rf"^{_DATE},?\s{_TIME}\s[-–]\s(?P<rest>.*)$")
_ATTACHED_IOS = re.compile(r"^<(?:attached|angehängt|bijgevoegd|adjunto|joint|allegato):\s*(?P<file>[^>]+)>$", re.I)
_ATTACHED_ANDROID = re.compile(r"^(?P<file>\S.*\.\w{2,5})\s\((?:file attached|Datei angehängt|bestand bijgevoegd|"
                               r"archivo adjunto|fichier joint|file allegato)\)$", re.I)
_OMITTED = re.compile(r"^(?:<Media omitted>|<Medien ausgeschlossen>|<Media weggelaten>|<Multimedia omitido>|"
                      r"(?P<what>image|video|audio|sticker|GIF|document|Contact card|Kontaktkarte|afbeelding|"
                      r"Bild|foto|sticker|document)\s(?:omitted|weggelaten|weggelassen|omitido|absent))$", re.I)
_DELETED = re.compile(r"^(?:This message was deleted|You deleted this message|Diese Nachricht wurde gelöscht|"
                      r"Du hast diese Nachricht gelöscht|Dit bericht is verwijderd|Je hebt dit bericht verwijderd)\.?$", re.I)
_EDITED = re.compile(r"\s*<(?:This message was edited|Diese Nachricht wurde bearbeitet|Dit bericht is bewerkt)>$", re.I)
_CALL = re.compile(r"^(?:Missed (?:voice|video|group voice|group video) call|(?:Voice|Video) call|"
                   r"Verpasster (?:Sprach|Video)anruf|Gemiste (?:spraak|video)oproep)\b", re.I)
_TITLE = re.compile(r"^WhatsApp(?: Chat| Unterhaltung| chat|-chat)?(?: with| mit| met| con| avec|\s-)\s*(?P<t>.+?)(?:\.txt|\.zip)?$", re.I)
_KINDS = {"image": "image/jpeg", "afbeelding": "image/jpeg", "bild": "image/jpeg", "foto": "image/jpeg",
          "video": "video/mp4", "audio": "audio/ogg", "gif": "image/gif", "sticker": "image/webp",
          "document": "application/octet-stream"}


@dataclass
class Parsed:
    title: str | None
    order: str                                  # "DMY" | "MDY" | "YMD"
    messages: list[dict] = field(default_factory=list)
    senders: list[str] = field(default_factory=list)
    skipped: int = 0


def _clean(line: str) -> str:
    return _SPACES.sub(" ", _INVISIBLE.sub("", line)).rstrip("\r")


def title_from_filename(name: str | None) -> str | None:
    if not name:
        return None
    stem = PurePosixPath(name.replace("\\", "/")).name
    m = _TITLE.match(stem)
    if m:
        return clean_name(m.group("t")) or None
    return None


def read_export(filename: str, data: bytes) -> tuple[str, dict[str, int], str | None]:
    """(chat text, {media file name: size}, title guessed from file names) from a .txt or .zip export."""
    title = title_from_filename(filename)
    if filename.lower().endswith(".zip") or data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise Invalid("that .zip couldn't be opened; export the chat again") from None
        txts = [i for i in z.infolist() if i.filename.lower().endswith(".txt") and not i.is_dir()]
        if not txts:
            raise Invalid("no chat .txt inside that .zip")
        chat = next((i for i in txts if PurePosixPath(i.filename).name == "_chat.txt"), txts[0])
        if chat.file_size > MAX_TEXT:
            raise Invalid("that chat is too big to import in one go")
        with z.open(chat) as f:
            raw = f.read(MAX_TEXT + 1)
        media = {PurePosixPath(i.filename).name: i.file_size for i in z.infolist() if i is not chat and not i.is_dir()}
        title = title or title_from_filename(chat.filename)
    else:
        raw, media = data, {}
    if len(raw) > MAX_TEXT:
        raise Invalid("that chat is too big to import in one go")
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(enc), media, title
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), media, title


def _date_order(dates: list[str], clock12: bool) -> str:
    firsts, seconds = [], []
    sep = "/"
    for d in dates:
        parts = re.split(r"[./-]", d)
        if len(parts[0]) == 4:
            return "YMD"
        firsts.append(int(parts[0]))
        seconds.append(int(parts[1]))
        sep = re.findall(r"[./-]", d)[0]
    if any(f > 12 for f in firsts):
        return "DMY"
    if any(s > 12 for s in seconds):
        return "MDY"
    return "MDY" if (sep == "/" and clock12) else "DMY"


def _when(date: str, time: str, order: str, tz: ZoneInfo) -> datetime:
    parts = [int(p) for p in re.split(r"[./-]", date)]
    y, m, d = {"DMY": (parts[2], parts[1], parts[0]), "MDY": (parts[2], parts[0], parts[1]),
               "YMD": (parts[0], parts[1], parts[2])}[order]
    if y < 100:
        y += 2000
    t = time.replace(".", ":", 2) if re.match(r"^\d{1,2}\.\d{2}", time) else time
    ampm = re.sub(r"[\s.]", "", t[len(re.match(r"[\d:]+", t).group(0)):]).lower()
    hms = [int(x) for x in re.match(r"[\d:]+", t).group(0).split(":") if x]
    h, mi, s = (hms + [0, 0])[:3]
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    return datetime(y, m, d, h, mi, s, tzinfo=tz)


def parse(text: str, title: str | None = None, date_order: str | None = None, tz: str | None = None) -> Parsed:
    """Parse an export's text into messages (sent_at in UTC) and the list of senders."""
    zone = ZoneInfo(tz) if tz else LOCAL_TZ
    lines = [_clean(l) for l in text.splitlines()]
    heads = []
    for i, line in enumerate(lines):
        m = _IOS.match(line) or _ANDROID.match(line)
        if m:
            heads.append((i, m))
    if not heads:
        raise Invalid("this doesn't look like a WhatsApp chat export (no dated lines found)")
    clock12 = any(re.search(r"[APap]\.?\s?[Mm]\.?$", m.group("time")) for _, m in heads)
    order = date_order or _date_order([m.group("date") for _, m in heads], clock12)
    out = Parsed(title=title, order=order)
    senders: dict[str, None] = {}
    for n, (i, m) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        rest = "\n".join([m.group("rest"), *lines[i + 1:end]]).strip()
        try:
            when = _when(m.group("date"), m.group("time"), order, zone)
        except ValueError:
            out.skipped += 1
            continue
        sender, body = None, rest
        sm = re.match(r"^(?P<name>[^:\n]{1,120}?):\s(?P<body>.*)$", rest, re.S)
        if sm:
            sender, body = clean_name(sm.group("name")), sm.group("body")
        elif ": " not in rest.split("\n", 1)[0] and rest.endswith(":"):
            sender, body = clean_name(rest[:-1]), ""
        msg = {"sent_at": when, "sender": sender, "body": body.strip(), "kind": "text", "attachments": [],
               "retracted": False, "edited": False}
        if sender is None:
            msg["kind"] = "system"
        else:
            senders.setdefault(sender)
            b = msg["body"]
            if _EDITED.search(b):
                msg["edited"], b = True, _EDITED.sub("", b)
            a = _ATTACHED_IOS.match(b.split("\n", 1)[0]) or _ATTACHED_ANDROID.match(b.split("\n", 1)[0])
            om = _OMITTED.match(b)
            if a:
                fname = a.group("file").strip()
                caption = b.split("\n", 1)[1].strip() if "\n" in b else ""
                msg.update(kind="attachment" if not caption else "text", body=caption,
                           attachments=[{"filename": fname, "mime": mimetypes.guess_type(fname)[0]}])
                if "STICKER" in fname.upper() or fname.lower().endswith(".webp"):
                    msg["kind"] = "sticker"
            elif om:
                what = (om.group("what") or "media").lower()
                msg.update(kind="sticker" if what == "sticker" else "attachment", body="",
                           attachments=[{"filename": None, "mime": _KINDS.get(what)}])
            elif _DELETED.match(b):
                msg.update(retracted=True, body="")
            elif _CALL.match(b):
                msg.update(kind="call", body=b)
            else:
                msg["body"] = b
        out.messages.append(msg)
    out.senders = list(senders)
    return out


def _hash(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]


def to_batches(parsed: Parsed, my_name: str | None = None, media: dict[str, int] | None = None,
               batch_size: int = 1000) -> list[dict]:
    """Ingest batches (cove.messages.ingest/v1) for one parsed chat."""
    media = media or {}
    title = parsed.title
    me = clean_name(my_name) if my_name else None
    others = [s for s in parsed.senders if s != me]
    if me is None and title and len(parsed.senders) == 2 and title in parsed.senders:
        me = next(s for s in parsed.senders if s != title)       # a 1:1 chat named after the other person
        others = [title]
    conv_key = _hash("whatsapp", title or (parsed.messages[0]["sent_at"].isoformat() + str(parsed.senders[:3])))
    group = len(others) > 1
    conv = {"external_id": f"wa:{conv_key}", "platform": "whatsapp", "kind": "group" if group else "direct",
            "title": title if group or not others else None,
            "participants": [_handle(s) for s in others]}
    seen: dict[tuple, int] = {}
    msgs = []
    for m in parsed.messages:
        stamp = m["sent_at"].isoformat()
        k = (stamp,)
        seen[k] = seen.get(k, 0) + 1
        from_me = m["sender"] is not None and m["sender"] == me
        ext = "wa:" + _hash(conv["external_id"], stamp, m["sender"] or "", m["body"], seen[k])
        atts = [{**a, "size": media.get(a["filename"]) if a.get("filename") else None} for a in m["attachments"]]
        msgs.append({"external_id": ext, "conversation": conv["external_id"], "is_from_me": from_me,
                     "sender": None if (from_me or m["sender"] is None) else _handle(m["sender"]),
                     "sent_at": stamp, "body": m["body"] or None, "kind": m["kind"], "retracted": m["retracted"],
                     **({"attachments": atts} if atts else {}),
                     "platform_meta": {"edited": True} if m["edited"] else {}})
    batches = []
    for i in range(0, max(len(msgs), 1), batch_size):
        batches.append({"format": "cove.messages.ingest/v1", "source": SOURCE,
                        "handles": [{**_handle(me), "is_me": True}] if me else [],
                        "conversations": [conv], "messages": msgs[i:i + batch_size]})
    return batches


def _handle(name: str) -> dict:
    from cove import phones
    if phones.looks_like_phone(name) and name.strip().startswith("+"):
        return {"platform": "whatsapp", "value": name}
    return {"platform": "whatsapp", "value": f"name:{name}", "display_name": name}


def import_export(filename: str, data: bytes, actor: str, my_name: str | None = None, title: str | None = None,
                  date_order: str | None = None) -> dict:
    """Import one export (.txt or .zip). Re-importing a newer export of the same chat only adds what's new."""
    from cove.fabric import services
    from cove.messages import ingest
    my_name = my_name or services.settings("messages").get("whatsapp_name")
    text, media, guessed = read_export(filename, data)
    parsed = parse(text, title=(title or guessed), date_order=date_order)
    totals = {"accepted": 0, "updated": 0, "duplicates": 0, "rejected": 0}
    for b in to_batches(parsed, my_name, media):
        r = ingest.ingest_batch(b, actor)
        for k in ("accepted", "updated", "duplicates"):
            totals[k] += r[k]
        totals["rejected"] += len(r["rejected"])
    return {"title": parsed.title, "date_order": parsed.order, "messages": len(parsed.messages),
            "senders": parsed.senders, "skipped": parsed.skipped, **totals}


from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {
    _policy.PURE: ["title_from_filename", "read_export", "parse", "to_batches"],
    "messages:ingest": ["import_export"],
})
