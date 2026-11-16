"""CRM service layer — the ONLY code that writes CRM data.

Both the web UI and the MCP server call these functions, so validation,
optimistic locking and audit logging are identical for humans and agents.
Every write takes an ``actor`` (``"web"``, ``"mcp:<client>"``, ...) that is
recorded in ``meta.audit_log``.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Iterable

import jsonschema
from psycopg.types.json import Jsonb

from cove import db
from cove.crm import vcard
from cove.templating import new_id

LOCAL_TZ = ZoneInfo(os.environ.get("COVE_TIMEZONE", "UTC"))
SCHEMA_DIR = Path(os.environ.get("COVE_SCHEMA_DIR", Path(__file__).resolve().parents[4] / "schemas"))
INTERACTION_KINDS = ("call", "meeting", "message", "email", "other")
# JSContact relatedTo / vCard RELATED relation types (RFC 6350 §6.6.6)
RELATION_TYPES = ("contact", "acquaintance", "friend", "met", "co-worker", "colleague", "co-resident",
                  "neighbor", "child", "parent", "sibling", "spouse", "kin", "muse", "crush", "date",
                  "sweetheart", "me", "agent", "emergency")


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    def __init__(self, message: str, current_version: int | None = None):
        super().__init__(message)
        self.current_version = current_version


class Invalid(ValueError):
    pass


# ================================================================== helpers
def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / "jscontact-card.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema)


def validate_card(card: dict) -> None:
    errors = sorted(_validator().iter_errors(card), key=lambda e: list(e.path))
    if errors:
        msgs = [f"{'/'.join(str(p) for p in e.path) or '(card)'}: {e.message}" for e in errors[:5]]
        raise Invalid("Invalid JSContact card — " + "; ".join(msgs))


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396 JSON Merge Patch."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            result.pop(k, None)
        else:
            result[k] = merge_patch(result.get(k), v)
    return result


def _diff(old: dict, new: dict) -> dict:
    keys = (set(old) | set(new)) - {"updated"}
    return {k: {"from": old.get(k), "to": new.get(k)} for k in sorted(keys) if old.get(k) != new.get(k)}


def _audit(c, actor: str, entity: str, entity_id: Any, action: str, diff: Any = None) -> None:
    c.execute("INSERT INTO meta.audit_log (actor, entity, entity_id, action, diff) VALUES (%s,%s,%s,%s,%s)",
              (actor, entity, str(entity_id), action, Jsonb(diff) if diff is not None else None))


def _uuid(value: Any) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value).removeprefix("urn:uuid:"))
    except ValueError:
        raise NotFound(f"no contact with id {value!r}") from None


def _plain(value: Any) -> Any:
    """Make DB rows JSON-friendly (UUIDs, datetimes)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def build_card(*, full_name: str | None = None, given: str | None = None, surname: str | None = None,
               emails: Iterable[str] = (), phones: Iterable[str] = (), organization: str | None = None,
               title: str | None = None, birthday: str | None = None, notes: str | None = None,
               how_met: str | None = None, links: Iterable[str] = ()) -> dict:
    """Build a JSContact card from simple fields (so agents needn't write JSContact)."""
    from cove.templating import date_from_form

    card: dict[str, Any] = {"@type": "Card", "version": "1.0", "kind": "individual"}
    name: dict[str, Any] = {}
    if full_name:
        name["full"] = full_name.strip()
    comps = [{"kind": k, "value": v.strip()} for k, v in (("given", given), ("surname", surname)) if v and v.strip()]
    if comps:
        name["components"] = comps
    if name:
        card["name"] = name
    for prop, key, values in (("emails", "address", emails), ("phones", "number", phones), ("links", "uri", links)):
        for v in values:
            if v and v.strip():
                m = card.setdefault(prop, {})
                m[new_id(m, prop[0])] = {key: v.strip()}
    if organization:
        card["organizations"] = {"o1": {"name": organization.strip()}}
    if title:
        card["titles"] = {"t1": {"name": title.strip(), "kind": "title"}}
    if birthday:
        try:
            card["anniversaries"] = {"a1": {"kind": "birth", "date": date_from_form(birthday)}}
        except ValueError as e:
            raise Invalid(str(e)) from None
    if notes:
        card["notes"] = {"n1": {"note": notes.strip(), "created": _now_utc()}}
    if how_met:
        card["cove.local:howWeMet"] = how_met.strip()
    return card


def _normalize(card: dict, contact_id: uuid.UUID, *, created: str | None = None) -> dict:
    card = copy.deepcopy(card)
    card["@type"] = "Card"
    card.setdefault("version", "1.0")
    card["uid"] = f"urn:uuid:{contact_id}"
    card["updated"] = _now_utc()
    card["created"] = created or card.get("created") or card["updated"]
    if not (card.get("name") or card.get("emails") or card.get("phones") or card.get("organizations")):
        raise Invalid("A contact needs at least a name, email, phone or organization.")
    validate_card(card)
    return card


def _absorb_crm_fields(c, contact_id: uuid.UUID, card: dict, actor: str) -> dict:
    """Move JSContact keywords -> crm tags and relatedTo (known contacts) ->
    crm relationships, so each fact has exactly one home."""
    card = copy.deepcopy(card)
    kws = [k for k, v in (card.pop("keywords", None) or {}).items() if v]
    if kws:
        _tag(c, [contact_id], kws, "tag", actor)
    rel = card.get("relatedTo") or {}
    for uid in list(rel):
        try:
            other = uuid.UUID(uid.removeprefix("urn:uuid:"))
        except ValueError:
            continue
        exists = c.execute("SELECT 1 FROM crm.contacts WHERE id=%s", (other,)).fetchone()
        if exists and other != contact_id:
            for rtype in [k for k, v in (rel[uid].get("relation") or {}).items() if v] or ["contact"]:
                c.execute("INSERT INTO crm.relationships (from_id, to_id, type) VALUES (%s,%s,%s) "
                          "ON CONFLICT DO NOTHING", (contact_id, other, rtype))
            del rel[uid]
    if "relatedTo" in card and not rel:
        del card["relatedTo"]
    return card


# ================================================================== contacts
def create_contact(card: dict, actor: str) -> dict:
    contact_id = uuid.uuid4()
    card = _normalize(card, contact_id)
    with db.conn() as c:
        c.execute("INSERT INTO crm.contacts (id, card) VALUES (%s, %s)", (contact_id, Jsonb(card)))
        absorbed = _absorb_crm_fields(c, contact_id, card, actor)  # needs the row to exist (FKs)
        if absorbed != card:
            card = absorbed
            c.execute("UPDATE crm.contacts SET card=%s WHERE id=%s", (Jsonb(card), contact_id))
        _audit(c, actor, "crm.contact", contact_id, "create", {"card": card})
    return get_contact(contact_id)


def _row(c, contact_id: uuid.UUID) -> dict:
    row = c.execute("SELECT * FROM crm.contact_overview o JOIN crm.contacts USING (id) WHERE id=%s",
                    (contact_id,)).fetchone()
    if not row:
        raise NotFound(f"no contact with id {contact_id}")
    return row


def get_contact(contact_id: Any, *, interactions: int = 10) -> dict:
    cid = _uuid(contact_id)
    with db.conn() as c:
        row = _row(c, cid)
        tags = c.execute("SELECT t.id, t.name, t.kind, t.color FROM crm.contact_tags ct JOIN crm.tags t "
                         "ON t.id=ct.tag_id WHERE ct.contact_id=%s ORDER BY t.kind, t.name", (cid,)).fetchall()
        rels = c.execute(
            """SELECT r.id, 'outgoing' AS direction, r.type, r.note, r.to_id AS other_id, o.display_name AS other_name
                 FROM crm.relationships r JOIN crm.contacts o ON o.id=r.to_id WHERE r.from_id=%s
               UNION ALL
               SELECT r.id, 'incoming', r.type, r.note, r.from_id, o.display_name
                 FROM crm.relationships r JOIN crm.contacts o ON o.id=r.from_id WHERE r.to_id=%s
               ORDER BY other_name""", (cid, cid)).fetchall()
        inter = _interactions(c, cid, interactions)
    return _plain({
        "id": row["id"], "version": row["version"], "display_name": row["display_name"],
        "card": row["card"], "tags": tags, "relationships": rels, "recent_interactions": inter,
        "last_contacted": row["last_contacted"],
        "follow_up": ({"every_days": row["every_days"], "next_due": row["next_due"],
                       "snoozed_until": row["snoozed_until"]} if row["every_days"] else None),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "archived_at": row["archived_at"],
    })


def _save_card(cid: uuid.UUID, new_card_fn, expected_version: int, actor: str, action: str = "update") -> dict:
    with db.conn() as c:
        row = c.execute("SELECT card, version, created_at FROM crm.contacts WHERE id=%s FOR UPDATE", (cid,)).fetchone()
        if not row:
            raise NotFound(f"no contact with id {cid}")
        if expected_version is not None and row["version"] != int(expected_version):
            raise Conflict(f"contact was changed elsewhere (you have version {expected_version}, "
                           f"current is {row['version']}); reload and retry", row["version"])
        old = row["card"]
        new = _absorb_crm_fields(c, cid, new_card_fn(old), actor)
        new = _normalize(new, cid, created=old.get("created"))
        diff = _diff(old, new)
        if diff:
            c.execute("UPDATE crm.contacts SET card=%s, version=version+1 WHERE id=%s", (Jsonb(new), cid))
            _audit(c, actor, "crm.contact", cid, action, diff)
    return get_contact(cid)


def update_contact(contact_id: Any, patch: dict, expected_version: int, actor: str) -> dict:
    """Apply an RFC 7396 merge patch to the contact's JSContact card."""
    if not isinstance(patch, dict):
        raise Invalid("patch must be a JSON object")
    return _save_card(_uuid(contact_id), lambda old: merge_patch(old, patch), expected_version, actor)


def replace_card(contact_id: Any, card: dict, expected_version: int, actor: str) -> dict:
    return _save_card(_uuid(contact_id), lambda old: card, expected_version, actor)


def archive_contact(contact_id: Any, actor: str, archived: bool = True) -> dict:
    cid = _uuid(contact_id)
    with db.conn() as c:
        r = c.execute("UPDATE crm.contacts SET archived_at = CASE WHEN %s THEN coalesce(archived_at, now()) END, "
                      "version = version + 1 WHERE id=%s RETURNING id", (archived, cid)).fetchone()
        if not r:
            raise NotFound(f"no contact with id {cid}")
        _audit(c, actor, "crm.contact", cid, "archive" if archived else "unarchive")
    return get_contact(cid)


def search_contacts(query: str | None = None, tag: str | None = None, due_only: bool = False,
                    include_archived: bool = False, limit: int = 50) -> list[dict]:
    where, params = [], []
    if not include_archived:
        where.append("o.archived_at IS NULL")
    if query and query.strip():
        q = query.strip()
        like = f"%{q}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        clauses = ["c.display_name ILIKE %s", "array_to_string(c.emails, ' ') ILIKE %s",
                   "c.search @@ plainto_tsquery('simple', %s)", "similarity(c.display_name, %s) > 0.3"]
        params += [like, like, q, q]
        if len(digits) >= 3:
            clauses.append("array_to_string(c.phones, ' ') LIKE %s")
            params.append(f"%{digits}%")
        where.append("(" + " OR ".join(clauses) + ")")
    if tag:
        where.append("EXISTS (SELECT 1 FROM crm.contact_tags ct JOIN crm.tags t ON t.id=ct.tag_id "
                     "WHERE ct.contact_id=o.id AND lower(t.name)=lower(%s))")
        params.append(tag)
    if due_only:
        where.append("o.next_due <= now() AND (o.snoozed_until IS NULL OR o.snoozed_until <= now())")
    order = "similarity(c.display_name, %s) DESC, lower(o.display_name)" if query and query.strip() else "lower(o.display_name)"
    if query and query.strip():
        params.append(query.strip())
    sql = f"""SELECT o.id, o.display_name, o.emails, o.phones, o.tags, o.last_contacted, o.next_due,
                     o.every_days, o.version, o.archived_at,
                     (SELECT v->>'name' FROM jsonb_each(coalesce(c.card->'organizations','{{}}'::jsonb)) AS x(k, v) LIMIT 1) AS organization
                FROM crm.contact_overview o JOIN crm.contacts c USING (id)
               {'WHERE ' + ' AND '.join(where) if where else ''}
               ORDER BY {order} LIMIT %s"""
    params.append(max(1, min(int(limit), 500)))
    with db.conn() as c:
        return _plain(c.execute(sql, params).fetchall())


# ================================================================== interactions
def _interactions(c, contact_id: uuid.UUID | None, limit: int, since: datetime | None = None) -> list[dict]:
    where, params = [], []
    if contact_id:
        where.append("i.id IN (SELECT interaction_id FROM crm.interaction_contacts WHERE contact_id=%s)")
        params.append(contact_id)
    if since:
        where.append("i.occurred_at >= %s")
        params.append(since)
    params.append(limit)
    return c.execute(
        f"""SELECT i.id, i.occurred_at, i.kind, i.summary, i.notes, i.calendar_event_id,
                   coalesce(json_agg(json_build_object('id', p.id, 'name', p.display_name) ORDER BY p.display_name)
                            FILTER (WHERE p.id IS NOT NULL), '[]') AS people
              FROM crm.interactions i
              LEFT JOIN crm.interaction_contacts ic ON ic.interaction_id = i.id
              LEFT JOIN crm.contacts p ON p.id = ic.contact_id
             {'WHERE ' + ' AND '.join(where) if where else ''}
             GROUP BY i.id ORDER BY i.occurred_at DESC LIMIT %s""", params).fetchall()


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise Invalid(f"{value!r} is not an ISO date/time") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)  # naive = user's local time


def log_interaction(contact_ids: list[Any], summary: str, actor: str, kind: str = "other",
                    occurred_at: Any = None, notes: str | None = None,
                    calendar_event_id: str | None = None) -> dict:
    if not summary or not summary.strip():
        raise Invalid("summary is required")
    if kind not in INTERACTION_KINDS:
        raise Invalid(f"kind must be one of {', '.join(INTERACTION_KINDS)}")
    ids = [_uuid(x) for x in contact_ids]
    if not ids:
        raise Invalid("at least one contact is required")
    with db.conn() as c:
        found = {r["id"] for r in c.execute("SELECT id FROM crm.contacts WHERE id = ANY(%s)", (ids,)).fetchall()}
        missing = [str(i) for i in ids if i not in found]
        if missing:
            raise NotFound(f"no contact with id {', '.join(missing)}")
        row = c.execute("INSERT INTO crm.interactions (occurred_at, kind, summary, notes, calendar_event_id) "
                        "VALUES (coalesce(%s, now()), %s, %s, %s, %s) RETURNING id",
                        (_parse_dt(occurred_at), kind, summary.strip(), (notes or "").strip() or None,
                         calendar_event_id or None)).fetchone()
        c.cursor().executemany("INSERT INTO crm.interaction_contacts VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      [(row["id"], i) for i in set(ids)])
        _audit(c, actor, "crm.interaction", row["id"], "create",
               {"contacts": [str(i) for i in ids], "kind": kind, "summary": summary})
        inter = c.execute("SELECT * FROM crm.interactions WHERE id=%s", (row["id"],)).fetchone()
    return _plain({**inter, "contact_ids": [str(i) for i in ids]})


def list_interactions(contact_id: Any = None, limit: int = 20, since: Any = None) -> list[dict]:
    with db.conn() as c:
        return _plain(_interactions(c, _uuid(contact_id) if contact_id else None,
                                    max(1, min(int(limit), 500)), _parse_dt(since)))


def delete_interaction(interaction_id: int, actor: str) -> None:
    with db.conn() as c:
        r = c.execute("DELETE FROM crm.interactions WHERE id=%s RETURNING summary", (interaction_id,)).fetchone()
        if not r:
            raise NotFound(f"no interaction {interaction_id}")
        _audit(c, actor, "crm.interaction", interaction_id, "delete", {"summary": r["summary"]})


# ================================================================== follow-ups
def set_follow_up(contact_id: Any, every_days: int | None, actor: str, snoozed_until: Any = None) -> dict:
    cid = _uuid(contact_id)
    with db.conn() as c:
        _row(c, cid)
        if not every_days:
            c.execute("DELETE FROM crm.follow_up_rules WHERE contact_id=%s", (cid,))
            _audit(c, actor, "crm.follow_up", cid, "delete")
        else:
            if int(every_days) <= 0:
                raise Invalid("every_days must be positive")
            c.execute("""INSERT INTO crm.follow_up_rules (contact_id, every_days, snoozed_until, active)
                         VALUES (%s, %s, %s, true)
                         ON CONFLICT (contact_id) DO UPDATE SET every_days=EXCLUDED.every_days,
                               snoozed_until=EXCLUDED.snoozed_until, active=true""",
                      (cid, int(every_days), _parse_dt(snoozed_until)))
            _audit(c, actor, "crm.follow_up", cid, "set",
                   {"every_days": int(every_days), "snoozed_until": str(snoozed_until) if snoozed_until else None})
    return get_contact(cid, interactions=0)


def follow_ups_due(limit: int = 50) -> list[dict]:
    with db.conn() as c:
        return _plain(c.execute(
            "SELECT id, display_name, tags, last_contacted, every_days, next_due FROM crm.follow_ups_due LIMIT %s",
            (max(1, min(int(limit), 500)),)).fetchall())


# ================================================================== tags
def list_tags() -> list[dict]:
    with db.conn() as c:
        return _plain(c.execute(
            """SELECT t.id, t.name, t.kind, t.color, count(c.id) AS contacts
                 FROM crm.tags t LEFT JOIN crm.contact_tags ct ON ct.tag_id=t.id
                 LEFT JOIN crm.contacts c ON c.id=ct.contact_id AND c.archived_at IS NULL
                GROUP BY t.id ORDER BY t.kind, lower(t.name)""").fetchall())


def _ensure_tags(c, names: Iterable[str], kind: str) -> list[int]:
    ids = []
    for name in {n.strip() for n in names if n and n.strip()}:
        row = c.execute("SELECT id FROM crm.tags WHERE lower(name)=lower(%s)", (name,)).fetchone()
        if not row:
            if kind not in ("tag", "group"):
                raise Invalid("kind must be 'tag' or 'group'")
            row = c.execute("INSERT INTO crm.tags (name, kind) VALUES (%s, %s) RETURNING id", (name, kind)).fetchone()
        ids.append(row["id"])
    return ids


def _tag(c, contact_ids: list[uuid.UUID], tags: Iterable[str], kind: str, actor: str) -> None:
    tag_ids = _ensure_tags(c, tags, kind)
    rows = [(cid, tid) for cid in contact_ids for tid in tag_ids]
    if rows:
        c.cursor().executemany("INSERT INTO crm.contact_tags VALUES (%s, %s) ON CONFLICT DO NOTHING", rows)
        for cid in contact_ids:
            _audit(c, actor, "crm.contact", cid, "tag", {"tags": sorted(set(tags))})


def tag_contacts(contact_ids: list[Any], tags: list[str], actor: str, kind: str = "tag") -> list[dict]:
    """Add tags (created on first use) to contacts. kind applies to new tags: 'tag' or 'group'."""
    ids = [_uuid(x) for x in contact_ids]
    with db.conn() as c:
        for cid in ids:
            _row(c, cid)
        _tag(c, ids, tags, kind, actor)
    return list_tags()


def untag_contacts(contact_ids: list[Any], tags: list[str], actor: str) -> None:
    ids = [_uuid(x) for x in contact_ids]
    with db.conn() as c:
        c.execute("""DELETE FROM crm.contact_tags WHERE contact_id = ANY(%s)
                     AND tag_id IN (SELECT id FROM crm.tags WHERE lower(name) = ANY(%s))""",
                  (ids, [t.lower() for t in tags]))
        for cid in ids:
            _audit(c, actor, "crm.contact", cid, "untag", {"tags": tags})


def set_contact_tags(contact_id: Any, tags: list[str], actor: str) -> None:
    cid = _uuid(contact_id)
    with db.conn() as c:
        _row(c, cid)
        tag_ids = _ensure_tags(c, tags, "tag")
        c.execute("DELETE FROM crm.contact_tags WHERE contact_id=%s AND NOT (tag_id = ANY(%s))", (cid, tag_ids))
        c.cursor().executemany("INSERT INTO crm.contact_tags VALUES (%s, %s) ON CONFLICT DO NOTHING", [(cid, t) for t in tag_ids])
        _audit(c, actor, "crm.contact", cid, "set_tags", {"tags": sorted(tags)})


def update_tag(tag_id: int, actor: str, name: str | None = None, kind: str | None = None, color: str | None = None) -> None:
    if kind and kind not in ("tag", "group"):
        raise Invalid("kind must be 'tag' or 'group'")
    with db.conn() as c:
        r = c.execute("UPDATE crm.tags SET name=coalesce(%s,name), kind=coalesce(%s,kind), color=coalesce(%s,color) "
                      "WHERE id=%s RETURNING id", (name or None, kind or None, color or None, tag_id)).fetchone()
        if not r:
            raise NotFound(f"no tag {tag_id}")
        _audit(c, actor, "crm.tag", tag_id, "update", {"name": name, "kind": kind, "color": color})


def delete_tag(tag_id: int, actor: str) -> None:
    with db.conn() as c:
        r = c.execute("DELETE FROM crm.tags WHERE id=%s RETURNING name", (tag_id,)).fetchone()
        if not r:
            raise NotFound(f"no tag {tag_id}")
        _audit(c, actor, "crm.tag", tag_id, "delete", {"name": r["name"]})


# ================================================================== relationships
def relate_contacts(from_id: Any, to_id: Any, relation: str, actor: str, note: str | None = None) -> dict:
    a, b = _uuid(from_id), _uuid(to_id)
    if a == b:
        raise Invalid("a contact can't be related to itself")
    relation = relation.strip().lower()
    if not relation:
        raise Invalid("relation is required")
    with db.conn() as c:
        _row(c, a)
        _row(c, b)
        c.execute("""INSERT INTO crm.relationships (from_id, to_id, type, note) VALUES (%s,%s,%s,%s)
                     ON CONFLICT (from_id, to_id, type) DO UPDATE SET note=EXCLUDED.note""",
                  (a, b, relation, note or None))
        _audit(c, actor, "crm.relationship", f"{a}->{b}", "set", {"type": relation, "note": note})
    return get_contact(a, interactions=0)


def delete_relationship(relationship_id: int, actor: str) -> None:
    with db.conn() as c:
        r = c.execute("DELETE FROM crm.relationships WHERE id=%s RETURNING from_id, to_id, type",
                      (relationship_id,)).fetchone()
        if not r:
            raise NotFound(f"no relationship {relationship_id}")
        _audit(c, actor, "crm.relationship", f"{r['from_id']}->{r['to_id']}", "delete", {"type": r["type"]})


# ================================================================== vCard I/O
def export_vcard(contact_ids: list[Any] | None = None, tag: str | None = None, include_archived: bool = False) -> str:
    where, params = [], []
    if contact_ids:
        where.append("c.id = ANY(%s)")
        params.append([_uuid(x) for x in contact_ids])
    if not include_archived:
        where.append("c.archived_at IS NULL")
    if tag:
        where.append("EXISTS (SELECT 1 FROM crm.contact_tags ct JOIN crm.tags t ON t.id=ct.tag_id "
                     "WHERE ct.contact_id=c.id AND lower(t.name)=lower(%s))")
        params.append(tag)
    with db.conn() as c:
        rows = c.execute(
            f"""SELECT c.card,
                       coalesce((SELECT array_agg(t.name) FROM crm.contact_tags ct JOIN crm.tags t ON t.id=ct.tag_id
                                  WHERE ct.contact_id=c.id), '{{}}') AS tags,
                       coalesce((SELECT array_agg(json_build_array('urn:uuid:' || r.to_id, r.type))
                                   FROM crm.relationships r WHERE r.from_id=c.id), '{{}}') AS rels
                  FROM crm.contacts c {'WHERE ' + ' AND '.join(where) if where else ''}
                 ORDER BY lower(c.display_name)""", params).fetchall()
    return "".join(vcard.card_to_vcard(r["card"], r["tags"], [tuple(x) for x in r["rels"]]) for r in rows)


def preview_import(text: str) -> dict:
    """Parse vCards and flag likely duplicates (same email, phone, imported UID or exact name)
    and cards that can't be saved. Returns {"items": [...], "errors": [unparseable cards]}."""
    cards, errors = vcard.parse_vcards_detailed(text)
    if not cards and not errors:
        raise Invalid("no vCards found in the file (expected BEGIN:VCARD … END:VCARD blocks)")
    out = []
    with db.conn() as c:
        for i, card in enumerate(cards):
            emails = [e["address"].lower() for e in (card.get("emails") or {}).values() if e.get("address")]
            phones = ["".join(ch for ch in p["number"] if ch.isdigit() or ch == "+")
                      for p in (card.get("phones") or {}).values() if p.get("number")]
            phones = [p for p in phones if len(p) >= 6]
            name = vcard.display_name(card)
            dups = c.execute(
                """SELECT id, display_name FROM crm.contacts
                    WHERE emails && %s OR phones && %s OR lower(display_name) = lower(%s)
                       OR (card->>'cove.local:importedUid') = %s LIMIT 3""",
                (emails, phones, name, card.get("cove.local:importedUid"))).fetchall()
            try:
                _normalize(card, uuid.uuid4())
                problem = None
            except Invalid as e:
                problem = str(e)
            out.append({"index": i, "display_name": name, "card": card, "emails": emails,
                        "duplicates": _plain(dups), "problem": problem,
                        "has_photo": any(m.get("kind") == "photo" for m in (card.get("media") or {}).values())})
    return {"items": out, "errors": errors}


def import_cards(cards: list[dict], actor: str) -> dict:
    """Create contacts one by one; a bad card is reported, not fatal.
    Returns {"created": [ids], "failed": ["name: reason"]}."""
    created, failed = [], []
    for card in cards:
        try:
            created.append(create_contact(card, actor)["id"])
        except (Invalid, NotFound) as e:
            failed.append(f"{vcard.display_name(card)}: {e}")
    return {"created": created, "failed": failed}


# ================================================================== meta
def describe_system() -> dict:
    """Everything an agent needs to orient itself: domains, tables, counts, notes."""
    with db.conn() as c:
        domains = c.execute("SELECT * FROM meta.domains ORDER BY name").fetchall()
        cols = c.execute(
            """SELECT table_schema, table_name, string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) AS columns
                 FROM information_schema.columns WHERE table_schema IN ('crm', 'meta')
                GROUP BY table_schema, table_name ORDER BY table_schema, table_name""").fetchall()
        counts = c.execute(
            """SELECT (SELECT count(*) FROM crm.contacts WHERE archived_at IS NULL) AS active_contacts,
                      (SELECT count(*) FROM crm.contacts WHERE archived_at IS NOT NULL) AS archived_contacts,
                      (SELECT count(*) FROM crm.interactions) AS interactions,
                      (SELECT count(*) FROM crm.tags) AS tags,
                      (SELECT count(*) FROM crm.relationships) AS relationships,
                      (SELECT count(*) FROM crm.follow_ups_due) AS follow_ups_due""").fetchone()
        topics = c.execute("SELECT topic, count(*) AS notes, max(updated_at) AS last_updated FROM meta.agent_notes "
                           "GROUP BY topic ORDER BY topic").fetchall()
        changes = c.execute("SELECT migration, summary, applied_at FROM meta.changelog ORDER BY id DESC LIMIT 10").fetchall()
    return _plain({
        "system": "cove-db: personal life databases. Postgres + this MCP server + a web UI at http://127.0.0.1:8080.",
        "domains": domains, "tables": cols, "counts": counts, "agent_note_topics": topics,
        "recent_changes": changes,
        "guidance": ["Use the domain tools for writes; sql_query is read-only.",
                     "Contact edits need the current `version` (optimistic locking).",
                     "Record durable learnings about the user's preferences with write_note."],
    })


def read_notes(topic: str | None = None) -> list[dict]:
    with db.conn() as c:
        if topic:
            rows = c.execute("SELECT * FROM meta.agent_notes WHERE topic=%s ORDER BY updated_at DESC", (topic,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM meta.agent_notes ORDER BY topic, updated_at DESC").fetchall()
    return _plain(rows)


def write_note(topic: str, note: str, author: str, note_id: int | None = None) -> dict:
    if not topic.strip() or not note.strip():
        raise Invalid("topic and note are required")
    with db.conn() as c:
        if note_id:
            row = c.execute("UPDATE meta.agent_notes SET topic=%s, note=%s, author=%s, updated_at=now() "
                            "WHERE id=%s RETURNING *", (topic.strip(), note.strip(), author, note_id)).fetchone()
            if not row:
                raise NotFound(f"no note {note_id}")
        else:
            row = c.execute("INSERT INTO meta.agent_notes (topic, note, author) VALUES (%s,%s,%s) RETURNING *",
                            (topic.strip(), note.strip(), author)).fetchone()
        _audit(c, author, "meta.agent_note", row["id"], "update" if note_id else "create", {"topic": topic})
    return _plain(row)


def delete_note(note_id: int, actor: str) -> None:
    with db.conn() as c:
        r = c.execute("DELETE FROM meta.agent_notes WHERE id=%s RETURNING topic", (note_id,)).fetchone()
        if not r:
            raise NotFound(f"no note {note_id}")
        _audit(c, actor, "meta.agent_note", note_id, "delete", {"topic": r["topic"]})


def sql_query(sql: str, max_rows: int = 200) -> dict:
    """Run a read-only query as cove_readonly (read-only session, 5s timeout)."""
    with db.ro_conn() as c:  # role defaults: read-only transactions, 5s statement_timeout
        cur = c.execute(sql)
        if cur.description is None:
            return {"columns": [], "rows": [], "truncated": False}
        rows = cur.fetchmany(max_rows + 1)
        c.rollback()
    return _plain({"columns": [d.name for d in cur.description], "rows": rows[:max_rows],
                   "truncated": len(rows) > max_rows})


def audit_trail(entity_id: Any, limit: int = 20) -> list[dict]:
    with db.conn() as c:
        return _plain(c.execute("SELECT at, actor, action, diff FROM meta.audit_log WHERE entity_id=%s "
                                "ORDER BY at DESC LIMIT %s", (str(entity_id), limit)).fetchall())
