"""Messages on the fabric (docs/fabric.md, docs/messages.md).

Provides identity.resolve (its own handles), identity.describe, timeline.items (conversation activity, one item
per conversation per day, never per message), search.query, activity.last_contact and activity.awaiting_reply.
Consumes identity.resolve and identity.describe from People, optionally. Its worker consumer re-matches senders
when handles appear or People's contacts change.

The agent rules of cove.messages.service apply here too: hidden conversations and one-time codes never leave
through the fabric to an agent.
"""

from __future__ import annotations

import uuid
from typing import Any

from cove import db, fabric
from cove.crm.service import LOCAL_TZ, _parse_dt
from cove.cursors import decode as decode_cursor, encode as encode_cursor
from cove.messages import service as svc
from cove.messages.normalize import handle_value, is_name_handle

KEY = "messages"
PLATFORM_NAMES = {"imessage": "iMessage", "sms": "SMS", "rcs": "RCS", "whatsapp": "WhatsApp", "signal": "Signal",
                  "telegram": "Telegram"}


def _ids(refs: list[str], kind: str) -> list[uuid.UUID]:
    out = []
    for r in refs or []:
        p = fabric.parse_ref(r)
        if p and p[0] == KEY and p[1] == kind:
            try:
                out.append(uuid.UUID(p[2]))
            except ValueError:
                pass
    return out


def resolve(identifiers: list[str]) -> list[dict]:
    values = {}
    for i in (identifiers or [])[:500]:
        v = handle_value("other", i)
        values.setdefault(v, []).append(i)
    vis = "" if not fabric.caller_is_agent() else (
        "AND EXISTS (SELECT 1 FROM messages.conversation_handles ch JOIN messages.conversations c ON c.id = ch.conversation_id "
        "WHERE ch.handle_id = h.id AND c.agent_visible)")
    with db.conn() as c:
        rows = c.execute(f"SELECT h.id, h.value, h.display_name FROM messages.handles h "
                         f"WHERE h.value = ANY(%s) AND NOT h.is_me {vis}", (list(values),)).fetchall()
    out = []
    for r in rows:
        label = r["display_name"] or (r["value"][5:] if is_name_handle(r["value"]) else r["value"])
        out += [{"identifier": i, "ref": fabric.ref(KEY, "handle", r["id"]), "label": label,
                 "confidence": 0.9, "method": "handle"} for i in values[r["value"]]]
    return out


def describe(refs: list[str]) -> list[dict]:
    out = []
    convs = _ids(refs, "conversation")
    if convs:
        res = svc.list_conversations(limit=200, include_archived=True)   # cheap enough, and applies agent rules
        wanted = {str(c) for c in convs}
        for cv in res["items"]:
            if cv["id"] in wanted:
                out.append({"ref": cv["ref"], "label": cv["title"], "kind": f"conversation.{cv['kind']}",
                            "url": cv["url"], "summary": f"{cv['message_count']} messages on {cv['platform']}"})
    handles = _ids(refs, "handle")
    if handles:
        with db.conn() as c:
            for h in c.execute("SELECT id, platform, value, display_name FROM messages.handles WHERE id = ANY(%s)",
                               (handles,)).fetchall():
                out.append({"ref": fabric.ref(KEY, "handle", h["id"]),
                            "label": h["display_name"] or (h["value"][5:] if is_name_handle(h["value"]) else h["value"]),
                            "kind": "handle", "url": None, "summary": f"{h['platform']} sender"})
    msgs = _ids(refs, "message")
    if msgs:
        with db.conn() as c:
            for m in c.execute(f"""SELECT m.id, m.conversation_id, m.sent_at FROM messages.messages m
                                    JOIN messages.conversations c ON c.id = m.conversation_id
                                   WHERE m.id = ANY(%s) AND {svc._visibility()}""", (msgs,)).fetchall():
                out.append({"ref": fabric.ref(KEY, "message", m["id"]), "label": f"Message of {m['sent_at']:%Y-%m-%d}",
                            "kind": "message", "url": f"/messages/c/{m['conversation_id']}#m-{m['id']}", "summary": None})
    return out


def timeline(subject_ref: str | None = None, since: str | None = None, until: str | None = None,
             limit: int = 50, cursor: str | None = None) -> dict:
    """One item per conversation per (local) day with activity: counts, and the day's last visible message."""
    limit = max(1, min(int(limit or 50), 200))
    where, params = [svc._visibility(), "m.kind <> 'system'", "NOT c.archived"], {"tz": str(LOCAL_TZ)}
    if subject_ref:
        p = fabric.parse_ref(subject_ref)
        if not p:
            return {"items": [], "next_cursor": None}
        with db.conn() as c:
            if p[0] == KEY and p[1] == "handle":
                hids = _ids([subject_ref], "handle")
            else:
                hids = [r["id"] for r in c.execute("SELECT id FROM messages.handles WHERE contact_ref = %s",
                                                    (subject_ref,)).fetchall()]
        if not hids:
            return {"items": [], "next_cursor": None}
        where.append("c.id IN (SELECT conversation_id FROM messages.conversation_handles WHERE handle_id = ANY(%(hids)s))")
        params["hids"] = hids
    if since:
        where.append("m.sent_at >= %(since)s")
        params["since"] = _parse_dt(since)
    if until:
        where.append("m.sent_at < %(until)s")
        params["until"] = _parse_dt(until)
    after = decode_cursor(cursor, 2)
    having = ""
    if after:
        having = "HAVING (max(m.sent_at), c.id::text) < (%(a_at)s::timestamptz, %(a_id)s)"
        params.update(a_at=after[0], a_id=after[1])
    params["limit"] = limit + 1
    with db.conn() as c:
        rows = c.execute(f"""SELECT c.id, (m.sent_at AT TIME ZONE %(tz)s)::date AS day, count(*) AS n,
                                    count(*) FILTER (WHERE m.is_from_me) AS mine, max(m.sent_at) AS last_at,
                                    (array_agg(m.body ORDER BY m.sent_at DESC) FILTER (WHERE m.body IS NOT NULL))[1] AS last_body
                               FROM messages.messages m JOIN messages.conversations c ON c.id = m.conversation_id
                              WHERE {' AND '.join(where)}
                              GROUP BY c.id, day {having}
                              ORDER BY last_at DESC, c.id::text DESC LIMIT %(limit)s""", params).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        convs = {r["id"]: r for r in c.execute("SELECT * FROM messages.conversations WHERE id = ANY(%s)",
                                               ([r["id"] for r in rows],)).fetchall()}
        parts = svc._participants(c, list(convs))
    names, _ = svc._names(p["contact_ref"] for ps in parts.values() for p in ps)
    items = []
    for r in rows:
        cv = svc._conv_out(convs[r["id"]], parts.get(r["id"], []), names)
        theirs = r["n"] - r["mine"]
        items.append({"ref": cv["ref"], "service": KEY, "kind": "messages.day", "occurred_at": svc._iso(r["last_at"]),
                      "title": f"{cv['title']}: {r['n']} message{'s' if r['n'] != 1 else ''} on {PLATFORM_NAMES.get(cv['platform'], cv['platform'])} "
                               f"({r['mine']} from you, {theirs} from them)",
                      "excerpt": (r["last_body"] or "")[:200] or None,
                      "participants": sorted({p["contact_ref"] or p["handle_ref"] for p in cv["participants"]}),
                      "url": f"{cv['url']}?date={r['day']}"})
    last = rows[-1] if rows else None
    return {"items": items,
            "next_cursor": encode_cursor(svc._iso(last["last_at"]), str(last["id"])) if more and last else None}


def search(q: str, limit: int = 20) -> list[dict]:
    res = svc.search_messages(q, limit=limit)
    n = len(res["items"])
    return [{"ref": m["ref"], "service": KEY, "title": f"{m['conversation']['title']}"
             f" ({'you' if m['is_from_me'] else m['sender']['label']})",
             "excerpt": (m["body"] or "")[:200] or None, "score": round(0.9 - 0.5 * i / max(n, 1), 3),
             "occurred_at": m["sent_at"]} for i, m in enumerate(res["items"])]


def awaiting(refs: list[str] | None = None, days: int = 14, limit: int = 20) -> list[dict]:
    res = svc.awaiting_reply(days=days, limit=limit, refs=refs)
    return [{"ref": cv["ref"], "title": cv["title"], "last_inbound_at": cv["waiting_since"], "url": cv["url"],
             "subject_refs": sorted({p["contact_ref"] for p in cv["participants"] if p["contact_ref"]})}
            for cv in res["items"]]


def last_contact(refs: list[str]) -> dict[str, Any]:
    return svc.last_contact(refs)


from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {"messages:read": ["resolve", "describe", "timeline", "search", "awaiting", "last_contact"]})


def _cap(name, fn, description):
    return fabric.Capability(name, fn, "messages:read", description)


MANIFEST = fabric.register(fabric.Manifest(
    key=KEY, version=1, read_scope="messages:read",
    provides={
        "identity.resolve": _cap("identity.resolve", resolve, "Phone numbers and emails to message senders"),
        "identity.describe": _cap("identity.describe", describe, "Conversations, senders and messages, by ref"),
        "timeline.items": _cap("timeline.items", timeline, "Conversation activity, one item per conversation per day"),
        "search.query": _cap("search.query", search, "Message text"),
        "activity.last_contact": _cap("activity.last_contact", last_contact, "Last message to and from each person"),
        "activity.awaiting_reply": _cap("activity.awaiting_reply", awaiting, "Threads waiting on your reply"),
    },
    consumes=("identity.resolve", "identity.describe"),
    publishes=("messages.handle.created",),
    consumers=(fabric.Consumer("messages.resolve-handles",
                               ("messages.handle.created", "people.contact.created", "people.contact.updated",
                                "people.contact.archived"), svc.on_events),),
))
