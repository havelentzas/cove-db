"""Messages service layer: the ONLY code that reads or curates the message archive (docs/messages.md).

The web UI, /api/v1 and MCP all call these functions, so the rules hold everywhere:
- `messages:read` to read, `messages:write` to curate (link senders, hide from agents, archive).
- Agents (MCP, personal-token API callers) never see conversations hidden from agents, and never see one-time
  codes while the OTP filter is on (the default). The signed-in user sees everything, marked.
- Sender names come from People through the fabric at read time, under the caller's own permissions; without
  them senders show as their raw handles and `coverage.missing` names People. No contact name is stored here.
- Bodies are never logged.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from cove import db, fabric, phones
from cove.crm.service import LOCAL_TZ, Invalid, NotFound, _audit, _parse_dt, _plain
from cove.cursors import decode as decode_cursor, encode as encode_cursor
from cove.messages.normalize import PLATFORMS, handle_value, is_name_handle

KEY = "messages"
MAX_WINDOW = 100


# ================================================================ helpers
def _settings() -> dict:
    from cove.fabric import services
    return {"otp_filter": True, **services.settings(KEY)}


def _agent() -> bool:
    return fabric.caller_is_agent()


def _visibility(c_alias: str = "c", m_alias: str | None = "m") -> str:
    """SQL that leaves out what agents may not see (nothing for the signed-in user)."""
    if not _agent():
        return "true"
    parts = [f"{c_alias}.agent_visible"]
    if m_alias and _settings().get("otp_filter", True):
        parts.append(f"NOT {m_alias}.is_sensitive")
    return " AND ".join(parts)


def _id(value: Any, kind: str) -> uuid.UUID:
    """A record id from a UUID or a cove ref of this kind."""
    text = str(value or "").strip()
    p = fabric.parse_ref(text)
    if p:
        if p[0] != KEY or p[1] != kind:
            raise NotFound(f"{text} is not a messages {kind}")
        text = p[2]
    try:
        return uuid.UUID(text)
    except ValueError:
        raise NotFound(f"no {kind} {value!r}") from None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _handle_label(h: dict, names: dict[str, str]) -> str:
    if h.get("contact_ref") and names.get(h["contact_ref"]):
        return names[h["contact_ref"]]
    if h.get("display_name"):
        return h["display_name"]
    v = h.get("value") or "unknown"
    return v[5:] if is_name_handle(v) else v


def _names(refs: Iterable[str | None]) -> tuple[dict[str, str], list]:
    """Contact names for refs, from their owner through the fabric, as the caller. ({ref: label}, results)."""
    by_owner: dict[str, set[str]] = {}
    for r in refs:
        p = fabric.parse_ref(r) if r else None
        if p:
            by_owner.setdefault(p[0], set()).add(r)
    names, results = {}, []
    for owner, rs in by_owner.items():
        res = fabric.ask("identity.describe", provider=owner, refs=sorted(rs))
        results.append(res)
        if res.ok:
            names.update({d["ref"]: d["label"] for d in res.value or []})
    return names, results


def _coverage(results: list) -> dict:
    return fabric.coverage([fabric.Result(KEY, None), *results])


def _person_handles(c, person: str) -> tuple[list[uuid.UUID], list]:
    """Handles that belong to `person`: a contact ref or id, a handle ref, a phone/email, or a name."""
    person = (person or "").strip()
    p = fabric.parse_ref(person)
    if p and p[0] == KEY and p[1] == "handle":
        return [_id(person, "handle")], []
    ref = person if p else None
    if not ref:
        try:
            ref = fabric.ref("people", "contact", uuid.UUID(person))
        except ValueError:
            ref = None
    if ref:
        rows = c.execute("SELECT id FROM messages.handles WHERE contact_ref = %s", (ref,)).fetchall()
        return [r["id"] for r in rows], []
    ident = phones.identifier(person)
    if ident != person or "@" in person or phones.looks_like_phone(person):
        rows = c.execute("SELECT id FROM messages.handles WHERE value = %s", (ident,)).fetchall()
        return [r["id"] for r in rows], []
    # a name: People's matches, plus handles whose own name matches (WhatsApp exports, iMessage display names)
    res = fabric.ask("identity.resolve", provider="people", identifiers=[person])
    refs = [m["ref"] for m in (res.value or []) if m["confidence"] >= 0.55] if res.ok else []
    rows = c.execute("SELECT id FROM messages.handles WHERE contact_ref = ANY(%s) OR lower(display_name) = lower(%s) "
                     "OR value = %s", (refs, person, "name:" + person)).fetchall()
    return [r["id"] for r in rows], [res]


def _handles(c, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict]:
    ids = sorted(set(i for i in ids if i))
    if not ids:
        return {}
    return {r["id"]: r for r in c.execute(
        "SELECT id, platform, value, display_name, is_me, contact_ref, resolution FROM messages.handles "
        "WHERE id = ANY(%s)", (ids,)).fetchall()}


def _participants(c, conv_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    out: dict[uuid.UUID, list[dict]] = {}
    if not conv_ids:
        return out
    for r in c.execute(
            """SELECT ch.conversation_id, h.id, h.platform, h.value, h.display_name, h.is_me, h.contact_ref, h.resolution
                 FROM messages.conversation_handles ch JOIN messages.handles h ON h.id = ch.handle_id
                WHERE ch.conversation_id = ANY(%s) ORDER BY h.is_me, h.value""", (conv_ids,)).fetchall():
        out.setdefault(r["conversation_id"], []).append(r)
    return out


def _person(h: dict, names: dict) -> dict:
    return {"handle_id": str(h["id"]), "handle_ref": fabric.ref(KEY, "handle", h["id"]), "label": _handle_label(h, names),
            "value": h["value"], "platform": h["platform"], "contact_ref": h["contact_ref"],
            "resolution": h["resolution"], "is_me": h["is_me"]}


def _conv_out(r: dict, parts: list[dict], names: dict) -> dict:
    others = [p for p in parts if not p["is_me"]]
    title = r.get("title") or ", ".join(_handle_label(p, names) for p in others[:4]) or "(no one)"
    return {"id": str(r["id"]), "ref": fabric.ref(KEY, "conversation", r["id"]), "title": title,
            "has_title": bool(r.get("title")), "kind": r["kind"], "platform": r["platform"],
            "last_message_at": _iso(r["last_message_at"]), "message_count": r["message_count"],
            "agent_visible": r["agent_visible"], "archived": r["archived"],
            "participants": [_person(p, names) for p in others],
            "unresolved": sum(1 for p in others if not p["contact_ref"]),
            "url": f"/messages/c/{r['id']}"}


# ================================================================ conversations
def list_conversations(person: str | None = None, q: str | None = None, platform: str | None = None,
                       active_since: Any = None, include_archived: bool = False, unresolved_only: bool = False,
                       cursor: str | None = None, limit: int = 50) -> dict:
    """Conversations, most recently active first: {items, next_cursor, coverage}."""
    limit = max(1, min(int(limit), 200))
    where, params, results = [_visibility("c", None)], [], []
    with db.conn() as c:
        if person:
            hids, results = _person_handles(c, person)
            where.append("c.id IN (SELECT conversation_id FROM messages.conversation_handles WHERE handle_id = ANY(%s))")
            params.append(hids)
        if q and q.strip():
            where.append("(c.title ILIKE %s OR EXISTS (SELECT 1 FROM messages.conversation_handles ch "
                         "JOIN messages.handles h ON h.id = ch.handle_id WHERE ch.conversation_id = c.id "
                         "AND (h.value ILIKE %s OR h.display_name ILIKE %s)))")
            params += [f"%{q.strip()}%"] * 3
        if platform:
            if platform not in PLATFORMS:
                raise Invalid(f"platform must be one of {', '.join(PLATFORMS)}")
            where.append("c.platform = %s")
            params.append(platform)
        if active_since:
            where.append("c.last_message_at >= %s")
            params.append(_parse_dt(active_since))
        if not include_archived:
            where.append("NOT c.archived")
        if unresolved_only:
            where.append("EXISTS (SELECT 1 FROM messages.conversation_handles ch JOIN messages.handles h "
                         "ON h.id = ch.handle_id WHERE ch.conversation_id = c.id AND NOT h.is_me AND h.contact_ref IS NULL)")
        after = decode_cursor(cursor, 2)
        if after:
            where.append("(coalesce(c.last_message_at, 'epoch'::timestamptz), c.id) < (%s::timestamptz, %s::uuid)")
            params += after
        rows = c.execute(f"""SELECT c.* FROM messages.conversations c WHERE {' AND '.join(where)}
                             ORDER BY coalesce(c.last_message_at, 'epoch'::timestamptz) DESC, c.id DESC LIMIT %s""",
                         [*params, limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        parts = _participants(c, [r["id"] for r in rows])
    names, res = _names(p["contact_ref"] for ps in parts.values() for p in ps)
    items = [_conv_out(r, parts.get(r["id"], []), names) for r in rows]
    last = rows[-1] if rows else None
    return {"items": items, "coverage": _coverage([*results, *res]),
            "next_cursor": encode_cursor(_iso(last["last_message_at"]) or "1970-01-01T00:00:00+00:00", str(last["id"]))
            if more and last else None}


def _load_conversation(c, conversation: Any) -> dict:
    cid = _id(conversation, "conversation")
    row = c.execute(f"SELECT c.* FROM messages.conversations c WHERE c.id = %s AND {_visibility('c', None)}",
                    (cid,)).fetchone()
    if not row:
        raise NotFound(f"no conversation {cid}")
    return row


def _message_rows(c, where: str, params: list, order: str, limit: int) -> list[dict]:
    return c.execute(f"""SELECT m.id, m.conversation_id, m.external_id, m.sender_handle_id, m.is_from_me, m.reply_to_id,
                                m.sent_at, m.edited_at, m.retracted, m.body, m.kind, m.reactions, m.is_sensitive, m.seq,
                                coalesce((SELECT json_agg(json_build_object('filename', a.filename, 'mime', a.mime,
                                          'size', a.size) ORDER BY a.ordinal) FROM messages.attachments a
                                          WHERE a.message_id = m.id), '[]') AS attachments
                           FROM messages.messages m JOIN messages.conversations c ON c.id = m.conversation_id
                          WHERE {where} AND {_visibility()} ORDER BY {order} LIMIT %s""", [*params, limit]).fetchall()


def _messages_out(c, rows: list[dict]) -> tuple[list[dict], list]:
    hids = {r["sender_handle_id"] for r in rows}
    for r in rows:
        for v in (r["reactions"] or {}).values():
            if v.get("handle_id"):
                try:
                    hids.add(uuid.UUID(v["handle_id"]))
                except ValueError:
                    pass
    handles = _handles(c, hids)
    names, res = _names(h["contact_ref"] for h in handles.values())
    out = []
    for r in rows:
        h = handles.get(r["sender_handle_id"])
        reactions = []
        for v in (r["reactions"] or {}).values():
            rh = handles.get(uuid.UUID(v["handle_id"])) if v.get("handle_id") else None
            reactions.append({"type": v.get("type"), "emoji": v.get("emoji"), "from_me": v.get("from_me", False),
                              "by": "you" if v.get("from_me") else (_handle_label(rh, names) if rh else "someone")})
        out.append({
            "id": str(r["id"]), "ref": fabric.ref(KEY, "message", r["id"]),
            "conversation_id": str(r["conversation_id"]), "sent_at": _iso(r["sent_at"]),
            "is_from_me": r["is_from_me"],
            "sender": None if r["is_from_me"] else ({"label": _handle_label(h, names), "handle_id": str(h["id"]),
                                                      "contact_ref": h["contact_ref"]} if h else {"label": "unknown"}),
            "body": r["body"], "kind": r["kind"], "edited_at": _iso(r["edited_at"]), "retracted": r["retracted"],
            "reactions": reactions, "attachments": r["attachments"], "is_sensitive": r["is_sensitive"],
            "reply_to_id": str(r["reply_to_id"]) if r["reply_to_id"] else None, "seq": r["seq"],
        })
    return out, res


def get_conversation(conversation: Any = None, person: str | None = None, around: Any = None,
                     before: int = 50, after: int = 0) -> dict:
    """A window of a thread: the latest `before` messages, or `before`/`after` around a message or a date."""
    before = max(0, min(int(before), MAX_WINDOW))
    after = max(0, min(int(after), MAX_WINDOW))
    results = []
    with db.conn() as c:
        if conversation is None:
            if not person:
                raise Invalid("pass a conversation, or a person")
            hids, results = _person_handles(c, person)
            row = c.execute(f"""SELECT c.id FROM messages.conversations c
                                 WHERE c.id IN (SELECT conversation_id FROM messages.conversation_handles WHERE handle_id = ANY(%s))
                                   AND {_visibility('c', None)}
                                 ORDER BY (c.kind = 'direct') DESC, c.last_message_at DESC NULLS LAST LIMIT 1""",
                            (hids,)).fetchone()
            if not row:
                raise NotFound(f"no conversation with {person!r}")
            conversation = row["id"]
        conv = _load_conversation(c, conversation)
        pivot = None
        if around:
            try:
                mid = _id(around, "message")
                hit = c.execute("SELECT sent_at FROM messages.messages WHERE id = %s AND conversation_id = %s",
                                (mid, conv["id"])).fetchone()
                pivot = hit["sent_at"] if hit else None
            except NotFound:
                pivot = _parse_dt(around)
            if pivot is None:
                raise NotFound(f"no message {around!r} in this conversation")
            after = after or before // 2
        if pivot is None:
            older = _message_rows(c, "m.conversation_id = %s", [conv["id"]], "m.sent_at DESC, m.id DESC", before + 1)
            newer = []
        else:
            older = _message_rows(c, "m.conversation_id = %s AND m.sent_at < %s", [conv["id"], pivot],
                                  "m.sent_at DESC, m.id DESC", before + 1)
            newer = _message_rows(c, "m.conversation_id = %s AND m.sent_at >= %s", [conv["id"], pivot],
                                  "m.sent_at, m.id", after + 1)
        has_before, has_after = len(older) > before, len(newer) > after
        rows = list(reversed(older[:before])) + newer[:after]
        msgs, res = _messages_out(c, rows)
        parts = _participants(c, [conv["id"]])
    names, res2 = _names(p["contact_ref"] for p in parts.get(conv["id"], []))
    return {"conversation": _conv_out(conv, parts.get(conv["id"], []), names), "messages": msgs,
            "has_before": has_before, "has_after": has_after, "coverage": _coverage([*results, *res, *res2])}


def search_messages(query: str | None = None, person: str | None = None, conversation: Any = None,
                    platform: str | None = None, since: Any = None, until: Any = None, from_me: bool | None = None,
                    limit: int = 50) -> dict:
    """Messages matching words (all of them, any language) and filters, newest first: {items, coverage}."""
    limit = max(1, min(int(limit), 100))
    where, params, results = ["NOT m.retracted"], [], []
    with db.conn() as c:
        if query and query.strip():
            where.append("m.search @@ websearch_to_tsquery('simple', %s)")
            params.append(query.strip())
        if person:
            hids, results = _person_handles(c, person)
            where.append("(m.sender_handle_id = ANY(%s) OR (m.is_from_me AND m.conversation_id IN "
                         "(SELECT conversation_id FROM messages.conversation_handles WHERE handle_id = ANY(%s))))")
            params += [hids, hids]
        if conversation:
            where.append("m.conversation_id = %s")
            params.append(_id(conversation, "conversation"))
        if platform:
            where.append("c.platform = %s")
            params.append(platform)
        if since:
            where.append("m.sent_at >= %s")
            params.append(_parse_dt(since))
        if until:
            where.append("m.sent_at < %s")
            params.append(_parse_dt(until))
        if from_me is not None:
            where.append("m.is_from_me = %s")
            params.append(bool(from_me))
        if len(where) == 1:
            raise Invalid("give some words to search for, or a person, conversation or date range")
        rows = _message_rows(c, " AND ".join(where), params, "m.sent_at DESC, m.id DESC", limit)
        msgs, res = _messages_out(c, rows)
        convs = {r["id"]: r for r in c.execute("SELECT * FROM messages.conversations WHERE id = ANY(%s)",
                                               ([r["conversation_id"] for r in rows],)).fetchall()}
        parts = _participants(c, list(convs))
    names, res2 = _names(p["contact_ref"] for ps in parts.values() for p in ps)
    for m in msgs:
        cv = convs[uuid.UUID(m["conversation_id"])]
        m["conversation"] = {k: v for k, v in _conv_out(cv, parts.get(cv["id"], []), names).items()
                             if k in ("id", "ref", "title", "platform", "url")}
    return {"items": msgs, "coverage": _coverage([*results, *res, *res2])}


def message_activity(person: str | None = None, conversation: Any = None, period_days: int = 90) -> dict:
    """How a relationship is going by message: volume, last in/out, typical reply times and the trend."""
    period_days = max(1, min(int(period_days), 3650))
    results = []
    with db.conn() as c:
        if conversation:
            convs = [_load_conversation(c, conversation)["id"]]
            hids = None
        elif person:
            hids, results = _person_handles(c, person)
            convs = [r["conversation_id"] for r in c.execute(
                "SELECT DISTINCT conversation_id FROM messages.conversation_handles WHERE handle_id = ANY(%s)",
                (hids,)).fetchall()]
        else:
            raise Invalid("pass a person or a conversation")
        vis = _visibility()
        base = f"""FROM messages.messages m JOIN messages.conversations c ON c.id = m.conversation_id
                  WHERE m.conversation_id = ANY(%(convs)s) AND m.kind <> 'system' AND {vis}
                    AND (%(hids)s::uuid[] IS NULL OR m.is_from_me OR m.sender_handle_id = ANY(%(hids)s::uuid[]))"""
        p = {"convs": convs, "hids": hids, "days": period_days}
        totals = c.execute(f"""SELECT count(*) FILTER (WHERE m.sent_at >= now() - make_interval(days => %(days)s)) AS messages,
                                      count(*) FILTER (WHERE m.is_from_me AND m.sent_at >= now() - make_interval(days => %(days)s)) AS sent,
                                      count(*) FILTER (WHERE NOT m.is_from_me AND m.sent_at >= now() - make_interval(days => %(days)s)) AS received,
                                      count(*) FILTER (WHERE m.sent_at >= now() - make_interval(days => %(days)s * 2)
                                                         AND m.sent_at < now() - make_interval(days => %(days)s)) AS previous,
                                      max(m.sent_at) FILTER (WHERE NOT m.is_from_me) AS last_inbound,
                                      max(m.sent_at) FILTER (WHERE m.is_from_me) AS last_outbound,
                                      count(*) AS all_time, min(m.sent_at) AS first_at
                                 {base}""", p).fetchone()
        replies = c.execute(f"""WITH seq AS (
                                    SELECT m.is_from_me, m.sent_at, lag(m.is_from_me) OVER w AS prev_me, lag(m.sent_at) OVER w AS prev_at
                                      {base} AND m.sent_at >= now() - make_interval(days => %(days)s)
                                    WINDOW w AS (PARTITION BY m.conversation_id ORDER BY m.sent_at))
                                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM sent_at - prev_at))
                                           FILTER (WHERE is_from_me AND NOT prev_me) AS mine,
                                       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM sent_at - prev_at))
                                           FILTER (WHERE NOT is_from_me AND prev_me) AS theirs
                                  FROM seq""", p).fetchone()
        weeks = c.execute(f"""SELECT date_trunc('week', m.sent_at AT TIME ZONE %(tz)s)::date AS week, count(*) AS n
                                {base} AND m.sent_at >= now() - interval '12 weeks' GROUP BY 1 ORDER BY 1""",
                          {**p, "tz": str(LOCAL_TZ)}).fetchall()
    now_n, prev_n = totals["messages"], totals["previous"]
    trend = ("new" if prev_n == 0 and now_n else "quiet" if now_n == 0 else
             "up" if now_n > prev_n * 1.25 else "down" if now_n < prev_n * 0.75 else "steady")
    mins = lambda s: round(s / 60) if s is not None else None  # noqa: E731
    return _plain({"conversations": len(convs), "period_days": period_days, "messages": now_n,
                   "sent": totals["sent"], "received": totals["received"], "previous_period": prev_n, "trend": trend,
                   "last_inbound": _iso(totals["last_inbound"]), "last_outbound": _iso(totals["last_outbound"]),
                   "typical_reply_minutes": {"you": mins(replies["mine"]), "them": mins(replies["theirs"])},
                   "all_time": totals["all_time"], "first_message_at": _iso(totals["first_at"]),
                   "weekly": [{"week": w["week"], "messages": w["n"]} for w in weeks],
                   "coverage": _coverage(results)})


def awaiting_reply(days: int = 14, limit: int = 20, person: str | None = None, refs: list[str] | None = None) -> dict:
    """Threads where the last message came from someone else (within `days`) and you haven't replied,
    longest wait first: {items, coverage}."""
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 200))
    results, filt, params = [], "", {"days": days, "limit": limit}
    with db.conn() as c:
        hids = None
        if person:
            hids, results = _person_handles(c, person)
        elif refs:
            hids = [r["id"] for r in c.execute("SELECT id FROM messages.handles WHERE contact_ref = ANY(%s)",
                                                (list(refs),)).fetchall()]
        if hids is not None:
            filt = "AND c.id IN (SELECT conversation_id FROM messages.conversation_handles WHERE handle_id = ANY(%(hids)s))"
            params["hids"] = hids
        rows = c.execute(f"""SELECT c.*, l.id AS last_id, l.sent_at AS waiting_since, l.body AS last_body,
                                    l.sender_handle_id AS last_sender, l.is_sensitive AS last_sensitive
                               FROM messages.conversations c
                               JOIN LATERAL (SELECT m.* FROM messages.messages m WHERE m.conversation_id = c.id
                                               AND m.kind NOT IN ('system') AND NOT m.retracted
                                             ORDER BY m.sent_at DESC LIMIT 1) l ON true
                              WHERE NOT c.archived AND NOT l.is_from_me AND {_visibility('c', None)}
                                AND l.sent_at >= now() - make_interval(days => %(days)s) {filt}
                              ORDER BY l.sent_at LIMIT %(limit)s""", params).fetchall()
        parts = _participants(c, [r["id"] for r in rows])
        senders = _handles(c, [r["last_sender"] for r in rows])
    names, res = _names([*(p["contact_ref"] for ps in parts.values() for p in ps),
                         *(h["contact_ref"] for h in senders.values())])
    hide_sensitive = _agent() and _settings().get("otp_filter", True)
    items = []
    for r in rows:
        conv = _conv_out(r, parts.get(r["id"], []), names)
        s = senders.get(r["last_sender"])
        conv.update(waiting_since=_iso(r["waiting_since"]), last_message_id=str(r["last_id"]),
                    last_from=_handle_label(s, names) if s else "unknown",
                    last_body=None if (hide_sensitive and r["last_sensitive"]) else (r["last_body"] or "")[:300])
        items.append(conv)
    return {"items": items, "coverage": _coverage([*results, *res])}


def last_contact(refs: list[str]) -> dict:
    """{person ref: {last_inbound, last_outbound, channel}} for refs that have linked handles and messages."""
    refs = [r for r in refs or [] if fabric.parse_ref(r)]
    if not refs:
        return {}
    with db.conn() as c:
        rows = c.execute(f"""SELECT h.contact_ref,
                                    max(m.sent_at) FILTER (WHERE NOT m.is_from_me AND m.sender_handle_id = h.id) AS last_in,
                                    max(m.sent_at) FILTER (WHERE m.is_from_me) AS last_out,
                                    (array_agg(c.platform ORDER BY m.sent_at DESC))[1] AS channel
                               FROM messages.handles h
                               JOIN messages.conversation_handles ch ON ch.handle_id = h.id
                               JOIN messages.conversations c ON c.id = ch.conversation_id
                               JOIN messages.messages m ON m.conversation_id = c.id
                                    AND (m.is_from_me OR m.sender_handle_id = h.id) AND m.kind <> 'system'
                              WHERE h.contact_ref = ANY(%s) AND (c.kind = 'direct' OR NOT m.is_from_me) AND {_visibility()}
                              GROUP BY h.contact_ref""", (refs,)).fetchall()
    return {r["contact_ref"]: {"last_inbound": _iso(r["last_in"]), "last_outbound": _iso(r["last_out"]),
                               "channel": r["channel"]} for r in rows if r["last_in"] or r["last_out"]}


# ================================================================ handles, sources, stats
def _load_handle(c, handle: Any) -> dict:
    text = str(handle or "").strip()
    try:
        hid = _id(text, "handle")
        row = c.execute("SELECT * FROM messages.handles WHERE id = %s", (hid,)).fetchone()
    except NotFound:
        row = c.execute("SELECT * FROM messages.handles WHERE value = %s ORDER BY platform LIMIT 1",
                        (handle_value("other", text),)).fetchone()
    if not row:
        raise NotFound(f"no handle {handle!r}")
    return row


def list_handles(state: str | None = None, q: str | None = None, limit: int = 200) -> dict:
    """Senders and their link to People. state: suggested | none | linked | me. {items, coverage}."""
    where, params = ["true"], []
    if state == "suggested":
        where.append("h.resolution = 'suggested'")
    elif state == "none":
        where.append("h.resolution = 'none' AND NOT h.is_me")
    elif state == "linked":
        where.append("h.contact_ref IS NOT NULL")
    elif state == "me":
        where.append("h.is_me")
    if q and q.strip():
        where.append("(h.value ILIKE %s OR h.display_name ILIKE %s)")
        params += [f"%{q.strip()}%"] * 2
    vis = "" if not _agent() else ("AND EXISTS (SELECT 1 FROM messages.conversation_handles ch JOIN messages.conversations c "
                                   "ON c.id = ch.conversation_id WHERE ch.handle_id = h.id AND c.agent_visible)")
    with db.conn() as c:
        rows = c.execute(f"""SELECT h.*, (SELECT count(*) FROM messages.messages m WHERE m.sender_handle_id = h.id) AS messages,
                                    (SELECT max(m.sent_at) FROM messages.messages m WHERE m.sender_handle_id = h.id) AS last_at
                               FROM messages.handles h WHERE {' AND '.join(where)} {vis}
                              ORDER BY h.is_me DESC, (SELECT count(*) FROM messages.messages m WHERE m.sender_handle_id = h.id) DESC,
                                       h.value LIMIT %s""", [*params, max(1, min(int(limit), 1000))]).fetchall()
    refs = [r["contact_ref"] for r in rows] + [s["ref"] for r in rows for s in (r["suggestions"] or [])]
    names, res = _names(refs)
    items = []
    for r in rows:
        items.append({**_person(r, names), "display_name": r["display_name"], "messages": r["messages"],
                      "last_at": _iso(r["last_at"]), "resolved_at": _iso(r["resolved_at"]),
                      "suggestions": [{**s, "label": names.get(s["ref"])} for s in (r["suggestions"] or [])
                                      if names.get(s["ref"])]})
    return {"items": items, "coverage": _coverage(res)}


def list_sources() -> list[dict]:
    with db.conn() as c:
        return _plain(c.execute(
            """SELECT s.id, s.key, s.platform, s.label, s.collector, s.last_ingest_at, s.created_at,
                      (SELECT count(*) FROM messages.conversations c WHERE c.source_id = s.id) AS conversations,
                      (SELECT count(*) FROM messages.messages m WHERE m.source_id = s.id) AS messages,
                      (SELECT max(m.sent_at) FROM messages.messages m WHERE m.source_id = s.id) AS newest
                 FROM messages.sources s ORDER BY s.last_ingest_at DESC NULLS LAST""").fetchall())


def stats() -> dict:
    with db.conn() as c:
        return _plain(c.execute(
            """SELECT (SELECT count(*) FROM messages.conversations WHERE NOT archived) AS conversations,
                      (SELECT count(*) FROM messages.messages) AS messages,
                      (SELECT count(*) FROM messages.handles WHERE resolution = 'suggested') AS to_confirm,
                      (SELECT count(*) FROM messages.handles WHERE resolution = 'none' AND NOT is_me) AS unresolved,
                      (SELECT count(*) FROM messages.sources) AS sources""").fetchone())


def changes(since: int = 0, limit: int = 500) -> dict:
    """The message change feed (seq), oldest first: {items, last_seq, more}. Agent rules apply."""
    limit = max(1, min(int(limit), 1000))
    with db.conn() as c:
        rows = _message_rows(c, "m.seq > %s", [int(since)], "m.seq", limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        msgs, res = _messages_out(c, rows)
    return {"items": msgs, "last_seq": rows[-1]["seq"] if rows else int(since), "more": more,
            "coverage": _coverage(res)}


# ================================================================ curation (messages:write)
def link_handle(handle: Any, contact: Any, actor: str) -> dict:
    """Link a sender to a person in People (a contact id or ref). Manual links are never undone automatically."""
    p = fabric.parse_ref(str(contact))
    ref = str(contact) if p else fabric.ref("people", "contact", _uuid_or_invalid(contact))
    check = fabric.ask("identity.describe", provider=fabric.parse_ref(ref)[0], refs=[ref])
    if not check.ok:
        raise Invalid(f"can't check that contact right now ({check.service}: {check.reason.replace('_', ' ')})")
    if not any(d["ref"] == ref for d in check.value or []):
        raise NotFound(f"no contact {contact}")
    with db.conn() as c:
        h = _load_handle(c, handle)
        c.execute("UPDATE messages.handles SET contact_ref = %s, resolution = 'manual', suggestions = '[]', "
                  "resolved_at = now(), dismissed = array_remove(dismissed, %s) WHERE id = %s", (ref, ref, h["id"]))
        _audit(c, actor, "messages.handle", h["id"], "link", {"contact_ref": ref})
    return {"handle_id": str(h["id"]), "contact_ref": ref, "resolution": "manual"}


def _uuid_or_invalid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise Invalid(f"{value!r} is not a contact id or ref") from None


def unlink_handle(handle: Any, actor: str) -> dict:
    """Remove a sender's link; the old match is remembered as dismissed so it isn't suggested again."""
    with db.conn() as c:
        h = _load_handle(c, handle)
        c.execute("UPDATE messages.handles SET contact_ref = NULL, resolution = 'none', suggestions = '[]', "
                  "resolved_at = now(), dismissed = CASE WHEN %s::text IS NULL THEN dismissed "
                  "ELSE array_append(array_remove(dismissed, %s::text), %s::text) END WHERE id = %s",
                  (h["contact_ref"], h["contact_ref"], h["contact_ref"], h["id"]))
        _audit(c, actor, "messages.handle", h["id"], "unlink", {"contact_ref": h["contact_ref"]})
    return {"handle_id": str(h["id"]), "resolution": "none"}


def dismiss_suggestion(handle: Any, ref: str, actor: str) -> dict:
    with db.conn() as c:
        h = _load_handle(c, handle)
        left = [s for s in (h["suggestions"] or []) if s["ref"] != ref]
        c.execute("UPDATE messages.handles SET suggestions = %s, resolution = CASE WHEN contact_ref IS NOT NULL "
                  "THEN resolution WHEN %s THEN 'suggested' ELSE 'none' END, "
                  "dismissed = array_append(array_remove(dismissed, %s), %s) WHERE id = %s",
                  (Jsonb(left), bool(left), ref, ref, h["id"]))
        _audit(c, actor, "messages.handle", h["id"], "dismiss", {"ref": ref})
    return {"handle_id": str(h["id"]), "suggestions": left}


def set_my_handle(handle: Any, is_me: bool, actor: str) -> dict:
    with db.conn() as c:
        h = _load_handle(c, handle)
        c.execute("UPDATE messages.handles SET is_me = %s WHERE id = %s", (bool(is_me), h["id"]))
        _audit(c, actor, "messages.handle", h["id"], "me" if is_me else "not_me")
    return {"handle_id": str(h["id"]), "is_me": bool(is_me)}


def set_conversation_agent_visible(conversation: Any, visible: bool, actor: str) -> dict:
    """Show or hide a conversation from every agent path (MCP, context_for, search, personal-token API)."""
    with db.conn() as c:
        cid = _id(conversation, "conversation")
        r = c.execute("UPDATE messages.conversations SET agent_visible = %s WHERE id = %s RETURNING id",
                      (bool(visible), cid)).fetchone()
        if not r:
            raise NotFound(f"no conversation {cid}")
        _audit(c, actor, "messages.conversation", cid, "show_to_agents" if visible else "hide_from_agents")
    return {"conversation_id": str(cid), "agent_visible": bool(visible)}


def archive_conversation(conversation: Any, actor: str, archived: bool = True) -> dict:
    with db.conn() as c:
        cid = _id(conversation, "conversation")
        r = c.execute("UPDATE messages.conversations SET archived = %s WHERE id = %s RETURNING id",
                      (bool(archived), cid)).fetchone()
        if not r:
            raise NotFound(f"no conversation {cid}")
        _audit(c, actor, "messages.conversation", cid, "archive" if archived else "unarchive")
    return {"conversation_id": str(cid), "archived": bool(archived)}


def resolve_handles(actor: str, rebuild: bool = False) -> dict:
    """Match unlinked senders to People again. rebuild=True first forgets automatic links (they're a cache:
    rebuilt from People); manual links and dismissals are kept."""
    if rebuild:
        with db.conn() as c:
            c.execute("UPDATE messages.handles SET contact_ref = NULL, resolution = 'none', suggestions = '[]' "
                      "WHERE resolution IN ('auto', 'suggested')")
            _audit(c, actor, "messages.handle", "*", "rebuild_links")
    return _resolve(None)


def _resolve(handle_ids: list | None, reverify_refs: list[str] | None = None) -> dict:
    """Match handles to People through the fabric, as the caller. Phone/email matches with exactly one
    contact link automatically; names (and ambiguous matches) become suggestions for you to confirm."""
    with db.conn() as c:
        where = ["NOT is_me", "resolution IN ('none', 'suggested')"]
        params: list = []
        if handle_ids is not None:
            where.append("id = ANY(%s)")
            params.append(list(handle_ids))
        rows = c.execute(f"SELECT id, platform, value, resolution, contact_ref, suggestions, dismissed "
                         f"FROM messages.handles WHERE {' AND '.join(where)} LIMIT 20000", params).fetchall()
        if reverify_refs:
            rows += c.execute("SELECT id, platform, value, resolution, contact_ref, suggestions, dismissed "
                              "FROM messages.handles WHERE resolution = 'auto' AND contact_ref = ANY(%s)",
                              (reverify_refs,)).fetchall()
    if not rows:
        return {"linked": 0, "suggested": 0, "checked": 0}
    matches: dict[str, list[dict]] = {}
    idents = sorted({r["value"] for r in rows})
    for i in range(0, len(idents), 500):
        res = fabric.ask("identity.resolve", provider="people", identifiers=idents[i:i + 500])
        if not res.ok:
            return {"linked": 0, "suggested": 0, "checked": 0, "unavailable": res.reason}
        for m in res.value or []:
            matches.setdefault(m["identifier"], []).append(m)
    updates = []
    linked = suggested = 0
    for r in rows:
        ms = [m for m in matches.get(r["value"], []) if m["ref"] not in (r["dismissed"] or [])]
        strong = {m["ref"] for m in ms if m["method"] in ("phone", "email") and m["confidence"] >= 0.9}
        if len(strong) == 1:
            new = ("auto", strong.pop(), [])
            linked += new[1] != r["contact_ref"]
        else:
            seen, sugg = set(), []
            for m in sorted(ms, key=lambda m: -m["confidence"]):
                if m["ref"] not in seen:
                    seen.add(m["ref"])
                    sugg.append({"ref": m["ref"], "confidence": m["confidence"], "method": m["method"]})
            new = ("suggested" if sugg else "none", None, sugg[:5])
            suggested += bool(sugg) and r["resolution"] != "suggested"
        if (new[0], new[1], new[2]) != (r["resolution"], r["contact_ref"], r["suggestions"] or []):
            updates.append((r["id"], *new))
    if updates:
        with db.conn() as c:
            c.execute("""UPDATE messages.handles h SET resolution = u.resolution, contact_ref = u.contact_ref,
                                suggestions = u.suggestions::jsonb, resolved_at = now()
                           FROM unnest(%s::uuid[], %s::text[], %s::text[], %s::text[]) AS u(id, resolution, contact_ref, suggestions)
                          WHERE h.id = u.id AND h.resolution <> 'manual'""",
                      ([u[0] for u in updates], [u[1] for u in updates], [u[2] for u in updates],
                       [json.dumps(u[3]) for u in updates]))
    return {"linked": linked, "suggested": suggested, "checked": len(rows)}


def on_events(events: list[dict]) -> None:
    """Worker consumer: match new handles, and re-match when People's contacts change."""
    new = [fabric.parse_ref(e["ref"])[2] for e in events if e["type"] == "messages.handle.created" and e["ref"]]
    people = [e["ref"] for e in events if e["type"].startswith("people.contact.") and e["ref"]]
    if people:
        _resolve(None, reverify_refs=people)
    elif new:
        _resolve(new)


def update_settings(actor: str, otp_filter: bool | None = None, default_region: str | None = None) -> dict:
    """Owner settings for Messages: hide one-time codes from agents, and the region for numbers without +."""
    from cove.fabric import services
    patch: dict[str, Any] = {}
    if otp_filter is not None:
        patch["otp_filter"] = bool(otp_filter)
    if default_region is not None:
        region = default_region.strip().upper()
        if region and region not in phones.REGIONS:
            raise Invalid(f"unknown region {default_region!r}; use one of {', '.join(sorted(phones.REGIONS))}")
        patch["default_region"] = region or None
    return services.update_settings(KEY, patch, actor)


def settings() -> dict:
    return {**_settings(), "default_region": _settings().get("default_region") or phones.default_region()}


from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {
    "messages:read": ["list_conversations", "get_conversation", "search_messages", "message_activity",
                      "awaiting_reply", "last_contact", "list_handles", "list_sources", "stats", "changes", "settings"],
    "messages:write": ["link_handle", "unlink_handle", "dismiss_suggestion", "set_my_handle",
                       "set_conversation_agent_visible", "archive_conversation", "resolve_handles",
                       "update_settings"],
    None: ["on_events"],   # the worker, as the workspace owner
})
