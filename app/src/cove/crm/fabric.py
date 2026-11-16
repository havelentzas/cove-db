"""People on the fabric (docs/fabric.md): what People shares with other services.

Provides identity.resolve (phone, email and name → contact refs), identity.describe, timeline.items (logged
conversations), search.query and activity.last_contact (from logged conversations). Every function runs as
the caller and only after the fabric checked `people:read`. People *consumes* activity.last_contact from
Messages, optionally (cove.crm.service: `_message_activity`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from cove import db, fabric, phones
from cove.cursors import decode as decode_cursor, encode as encode_cursor
from cove.crm.service import _parse_dt

KEY = "people"


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def contact_ref(contact_id: Any) -> str:
    return fabric.ref(KEY, "contact", contact_id)


def _contact_ids(refs: list[str], kind: str = "contact") -> list[uuid.UUID]:
    out = []
    for r in refs or []:
        p = fabric.parse_ref(r)
        if p and p[0] == KEY and p[1] == kind:
            try:
                out.append(uuid.UUID(p[2]))
            except ValueError:
                pass
    return out


def _aliases(card: dict) -> list[str]:
    names = [v.get("name") for v in (card.get("nicknames") or {}).values() if isinstance(v, dict)]
    comps = (card.get("name") or {}).get("components") or []
    given = [c.get("value") for c in comps if isinstance(c, dict) and c.get("kind") == "given"]
    return sorted({n.strip() for n in [*names, *given] if isinstance(n, str) and n.strip()})


# ---------------------------------------------------------------- identity.resolve
def resolve(identifiers: list[str]) -> list[dict]:
    out: list[dict] = []
    with db.conn() as c:
        for raw in (identifiers or [])[:500]:
            ident = (raw or "").strip()
            if not ident:
                continue
            if ident.startswith("name:") or not (phones.email(ident) or phones.looks_like_phone(ident)):
                name = ident.removeprefix("name:").strip()
                if len(name) < 2:
                    continue
                rows = c.execute(
                    """SELECT id, display_name, card,
                              CASE WHEN lower(display_name) = lower(%s) THEN 0.6
                                   ELSE greatest(similarity(display_name, %s) - 0.3, 0.3) END AS conf
                         FROM crm.contacts
                        WHERE archived_at IS NULL AND (lower(display_name) = lower(%s) OR similarity(display_name, %s) > 0.6
                              OR EXISTS (SELECT 1 FROM jsonb_each(coalesce(card->'nicknames', '{}')) n(k, v)
                                          WHERE lower(v->>'name') = lower(%s)))
                        ORDER BY conf DESC LIMIT 5""", (name, name, name, name, name)).fetchall()
                out += [{"identifier": raw, "ref": contact_ref(r["id"]), "label": r["display_name"],
                         "confidence": round(float(r["conf"]), 2), "method": "name"} for r in rows]
            elif phones.email(ident):
                rows = c.execute("SELECT id, display_name FROM crm.contacts WHERE %s = ANY(emails) "
                                 "AND archived_at IS NULL", (phones.email(ident),)).fetchall()
                out += [{"identifier": raw, "ref": contact_ref(r["id"]), "label": r["display_name"],
                         "confidence": 0.95 if len(rows) == 1 else 0.7, "method": "email"} for r in rows]
            else:
                target = phones.phone(ident)
                tail = target[-8:] if target else ""
                if len(tail) < 6:
                    continue
                rows = c.execute(
                    "SELECT id, display_name, phones FROM crm.contacts WHERE archived_at IS NULL AND EXISTS "
                    "(SELECT 1 FROM unnest(phones) p WHERE right(regexp_replace(p, '\\D', '', 'g'), 8) = %s)",
                    (tail,)).fetchall()
                hits = [r for r in rows if any(phones.phone(p) == target for p in r["phones"])]
                out += [{"identifier": raw, "ref": contact_ref(r["id"]), "label": r["display_name"],
                         "confidence": 1.0 if len(hits) == 1 else 0.7, "method": "phone"} for r in hits]
    return out


# ---------------------------------------------------------------- identity.describe
def describe(refs: list[str]) -> list[dict]:
    ids = _contact_ids(refs)
    iids = [int(p[2]) for p in map(fabric.parse_ref, refs or []) if p and p[0] == KEY and p[1] == "interaction"
            and p[2].isdigit()]
    out = []
    with db.conn() as c:
        for r in c.execute(
                """SELECT o.id, o.display_name, o.tags, c.card, o.archived_at
                     FROM crm.contact_overview o JOIN crm.contacts c USING (id) WHERE o.id = ANY(%s)""", (ids,)):
            org = next((v.get("name") for v in (r["card"].get("organizations") or {}).values() if v.get("name")), None)
            summary = " · ".join(x for x in [org, ", ".join(r["tags"]) or None,
                                             "archived" if r["archived_at"] else None] if x) or None
            out.append({"ref": contact_ref(r["id"]), "label": r["display_name"], "kind": "contact",
                        "url": f"/contacts/{r['id']}", "summary": summary, "aliases": _aliases(r["card"])})
        for r in c.execute("SELECT id, summary, kind, occurred_at FROM crm.interactions WHERE id = ANY(%s)", (iids,)):
            out.append({"ref": fabric.ref(KEY, "interaction", r["id"]), "label": r["summary"], "kind": "interaction",
                        "url": None, "summary": f"{r['kind']} on {r['occurred_at']:%Y-%m-%d}"})
    return out


# ---------------------------------------------------------------- timeline.items
def timeline(subject_ref: str | None = None, since: str | None = None, until: str | None = None,
             limit: int = 50, cursor: str | None = None) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    where, params = [], []
    if subject_ref:
        ids = _contact_ids([subject_ref])
        if not ids:
            return {"items": [], "next_cursor": None}       # not a person People knows
        where.append("i.id IN (SELECT interaction_id FROM crm.interaction_contacts WHERE contact_id = %s)")
        params.append(ids[0])
    if since:
        where.append("i.occurred_at >= %s")
        params.append(_parse_dt(since))
    if until:
        where.append("i.occurred_at < %s")
        params.append(_parse_dt(until))
    after = decode_cursor(cursor, 2)
    if after:
        where.append("(i.occurred_at, i.id) < (%s::timestamptz, %s)")
        params += after
    with db.conn() as c:
        rows = c.execute(
            f"""SELECT i.id, i.occurred_at, i.kind, i.summary, i.notes,
                       array_agg(ic.contact_id) FILTER (WHERE ic.contact_id IS NOT NULL) AS people
                  FROM crm.interactions i LEFT JOIN crm.interaction_contacts ic ON ic.interaction_id = i.id
                 {'WHERE ' + ' AND '.join(where) if where else ''}
                 GROUP BY i.id ORDER BY i.occurred_at DESC, i.id DESC LIMIT %s""", [*params, limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [{"ref": fabric.ref(KEY, "interaction", r["id"]), "service": KEY, "kind": f"interaction.{r['kind']}",
              "occurred_at": _iso(r["occurred_at"]), "title": r["summary"], "excerpt": (r["notes"] or None) and r["notes"][:280],
              "participants": [contact_ref(p) for p in (r["people"] or [])],
              "url": f"/contacts/{r['people'][0]}" if r["people"] else None} for r in rows]
    return {"items": items,
            "next_cursor": encode_cursor(_iso(rows[-1]["occurred_at"]), rows[-1]["id"]) if more and rows else None}


# ---------------------------------------------------------------- search.query
def search(q: str, limit: int = 20) -> list[dict]:
    from cove.crm import service as svc
    limit = max(1, min(int(limit or 20), 100))
    hits = []
    for i, r in enumerate(svc.search_contacts(q, limit=limit)):
        bits = [r.get("organization"), ", ".join(r.get("tags") or []) or None]
        hits.append({"ref": contact_ref(r["id"]), "service": KEY, "title": r["display_name"],
                     "excerpt": " · ".join(b for b in bits if b) or None, "score": round(1 - i / (limit + 1), 3),
                     "occurred_at": r.get("last_contacted")})
    with db.conn() as c:
        rows = c.execute("SELECT id, occurred_at, summary, notes FROM crm.interactions "
                         "WHERE summary ILIKE %s OR notes ILIKE %s ORDER BY occurred_at DESC LIMIT %s",
                         (f"%{q}%", f"%{q}%", limit)).fetchall()
    hits += [{"ref": fabric.ref(KEY, "interaction", r["id"]), "service": KEY, "title": r["summary"],
              "excerpt": (r["notes"] or "")[:200] or None, "score": 0.4, "occurred_at": _iso(r["occurred_at"])}
             for r in rows]
    return hits[:limit]


# ---------------------------------------------------------------- activity.last_contact
def last_contact(refs: list[str]) -> dict:
    ids = _contact_ids(refs)
    with db.conn() as c:
        rows = c.execute(
            """SELECT DISTINCT ON (ic.contact_id) ic.contact_id, i.occurred_at, i.kind
                 FROM crm.interaction_contacts ic JOIN crm.interactions i ON i.id = ic.interaction_id
                WHERE ic.contact_id = ANY(%s) ORDER BY ic.contact_id, i.occurred_at DESC""", (ids,)).fetchall()
    # A logged conversation is two-way, so it counts as both hearing from them and getting in touch.
    return {contact_ref(r["contact_id"]): {"last_inbound": _iso(r["occurred_at"]), "last_outbound": _iso(r["occurred_at"]),
                                           "channel": r["kind"]} for r in rows}


# Providers are checked by the fabric before it calls them, and again here for direct callers.
from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {_policy.PURE: ['contact_ref'], 'people:read': ['resolve', 'describe', 'timeline', 'search', 'last_contact']})


def _cap(name, fn, description):
    return fabric.Capability(name, fn, "people:read", description)


MANIFEST = fabric.register(fabric.Manifest(
    key=KEY, version=1, read_scope="people:read",
    provides={
        "identity.resolve": _cap("identity.resolve", resolve, "Phone numbers, emails and names to contacts"),
        "identity.describe": _cap("identity.describe", describe, "Contacts and logged conversations, by ref"),
        "timeline.items": _cap("timeline.items", timeline, "Logged conversations (calls, meetings, messages)"),
        "search.query": _cap("search.query", search, "Contacts and logged conversations"),
        "activity.last_contact": _cap("activity.last_contact", last_contact, "Last logged conversation per contact"),
    },
    consumes=("activity.last_contact",),
    publishes=("people.contact.created", "people.contact.updated", "people.contact.archived"),
))
