"""Bulk ingest for message collectors (docs/messages.md › Ingest protocol, schemas/contracts/messages-ingest.v1.json).

- One batch is one transaction: handles, conversations, messages (upserted from arrays with `unnest`, so a
  1,000-message batch is a handful of statements), attachments, replies, edits, retractions, reactions.
- Idempotent on (source, external_id). A re-sent message that hasn't changed is a duplicate; a changed body,
  a newer edit or a retraction updates it in place. A retraction is final and drops the body.
- One audit row per batch, with counts only. Errors name fields and reasons, never message text.
- After the batch commits, new handles are matched to People through the fabric, as the caller. A collector's
  ingest token can't read People, so its handles are matched by the worker instead (messages.handle.created).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
from psycopg.types.json import Jsonb

from cove import db, fabric
from cove.crm.service import Invalid, _audit
from cove.messages.normalize import clean_name, handle_value, is_sensitive

FORMAT = "cove.messages.ingest/v1"
SCHEMA = Path(os.environ.get("COVE_SCHEMA_DIR", Path(__file__).resolve().parents[4] / "schemas")) / "contracts" / "messages-ingest.v1.json"
MAX_MESSAGES = 1000

# How an incoming copy of an existing message is merged into it (used in SET and in the "changed?" test).
_KEEP_OLD = "(m.edited_at IS NOT NULL AND (EXCLUDED.edited_at IS NULL OR EXCLUDED.edited_at < m.edited_at))"
_NEW = {
    "retracted": "(m.retracted OR EXCLUDED.retracted)",
    "edited_at": "greatest(m.edited_at, EXCLUDED.edited_at)",
    "body": f"CASE WHEN m.retracted OR EXCLUDED.retracted THEN NULL WHEN {_KEEP_OLD} THEN m.body ELSE EXCLUDED.body END",
    "is_sensitive": f"CASE WHEN m.retracted OR EXCLUDED.retracted THEN false WHEN {_KEEP_OLD} THEN m.is_sensitive "
                    "ELSE EXCLUDED.is_sensitive END",
    "kind": "EXCLUDED.kind",
    "platform_meta": "EXCLUDED.platform_meta",
    "sender_handle_id": "EXCLUDED.sender_handle_id",
}
_UPSERT = f"""
INSERT INTO messages.messages AS m (id, source_id, conversation_id, external_id, sender_handle_id, is_from_me,
                                    sent_at, edited_at, retracted, body, kind, platform_meta, is_sensitive)
SELECT i.id, %(src)s, i.conversation_id, i.external_id, i.sender_handle_id, i.is_from_me, i.sent_at, i.edited_at,
       i.retracted, CASE WHEN i.retracted THEN NULL ELSE i.body END, i.kind, i.platform_meta::jsonb,
       i.is_sensitive AND NOT i.retracted
  FROM unnest(%(id)s::uuid[], %(conv)s::uuid[], %(ext)s::text[], %(sender)s::uuid[], %(from_me)s::bool[],
              %(sent)s::timestamptz[], %(edited)s::timestamptz[], %(retracted)s::bool[], %(body)s::text[],
              %(kind)s::text[], %(meta)s::text[], %(sens)s::bool[])
       AS i(id, conversation_id, external_id, sender_handle_id, is_from_me, sent_at, edited_at, retracted, body,
            kind, platform_meta, is_sensitive)
ON CONFLICT (source_id, external_id) DO UPDATE SET {", ".join(f"{k} = {v}" for k, v in _NEW.items())}
 WHERE ({", ".join(f"m.{k}" for k in _NEW)}) IS DISTINCT FROM ({", ".join(_NEW.values())})
RETURNING m.id, m.external_id, m.conversation_id, (m.xmax = 0) AS inserted"""


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None     # the contract requires an offset; a naive time is ambiguous


def validate(batch: Any) -> None:
    """Raise Invalid naming the first problem's location and rule (never the offending value: it may be text)."""
    errors = sorted(_validator().iter_errors(batch), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.absolute_path) or "batch"
        rule = e.validator if e.validator not in ("const",) else "format"
        raise Invalid(f"{where}: fails '{rule}' (see schemas/contracts/messages-ingest.v1.json)")


def ingest_batch(batch: dict, actor: str) -> dict:
    """Store one batch. Returns {accepted, updated, duplicates, rejected: [{external_id, reason}], …}."""
    from cove.fabric import services
    validate(batch)
    region = services.settings("messages").get("default_region")
    src = batch["source"]
    rejected: list[dict] = []

    # ---- handles: everything mentioned anywhere in the batch, normalised
    handles: dict[tuple[str, str], dict] = {}

    def handle(h: dict | None) -> tuple[str, str] | None:
        if not h:
            return None
        key = (h["platform"], handle_value(h["platform"], h["value"], region))
        cur = handles.setdefault(key, {"display_name": None, "is_me": False})
        if h.get("display_name"):
            cur["display_name"] = clean_name(h["display_name"])[:300] or None
        cur["is_me"] = cur["is_me"] or bool(h.get("is_me"))
        return key

    for h in batch.get("handles") or []:
        handle(h)
    convs = {}
    for cv in batch.get("conversations") or []:
        parts = [handle(p) for p in cv.get("participants") or []]
        convs[cv["external_id"]] = {**cv, "_parts": [p for p in parts if p]}

    msgs: dict[str, dict] = {}
    duplicates = 0
    for m in batch.get("messages") or []:
        sent = _ts(m["sent_at"])
        if sent is None:
            rejected.append({"external_id": m["external_id"], "reason": "bad_timestamp"})
            continue
        if m["external_id"] in msgs:
            duplicates += 1                  # the same message twice in one batch: keep the last copy
        msgs[m["external_id"]] = {**m, "_sent": sent, "_sender": None if m["is_from_me"] else handle(m.get("sender"))}
    reactions = batch.get("reactions") or []
    for r in reactions:
        if not r["is_from_me"]:
            handle(r.get("sender"))

    with db.conn() as c:
        source_id = c.execute(
            """INSERT INTO messages.sources (key, platform, label, collector) VALUES (%s, %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label, collector = EXCLUDED.collector
               RETURNING id""", (src["key"], src["platform"], src["label"], src.get("collector"))).fetchone()["id"]

        hid: dict[tuple[str, str], uuid.UUID] = {}
        new_handles: list[tuple[uuid.UUID, str]] = []
        if handles:
            keys = list(handles)
            for r in c.execute(
                    """INSERT INTO messages.handles (platform, value, display_name, is_me)
                       SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::bool[])
                       ON CONFLICT (platform, value) DO UPDATE
                          SET display_name = coalesce(EXCLUDED.display_name, messages.handles.display_name),
                              is_me = messages.handles.is_me OR EXCLUDED.is_me
                       RETURNING id, platform, value, (xmax = 0) AS created""",
                    ([k[0] for k in keys], [k[1] for k in keys], [handles[k]["display_name"] for k in keys],
                     [handles[k]["is_me"] for k in keys])).fetchall():
                hid[(r["platform"], r["value"])] = r["id"]
                if r["created"]:
                    new_handles.append((r["id"], r["platform"]))

        # ---- conversations: those in the batch, plus earlier ones messages refer to
        cid: dict[str, uuid.UUID] = {}
        if convs:
            ext = list(convs)
            kinds = [convs[e].get("kind") or ("group" if len([p for p in convs[e]["_parts"] if not handles[p]["is_me"]]) > 1
                                             else "direct") for e in ext]
            for r in c.execute(
                    """INSERT INTO messages.conversations (source_id, external_id, platform, kind, title)
                       SELECT %s, * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[])
                       ON CONFLICT (source_id, external_id) DO UPDATE
                          SET title = coalesce(EXCLUDED.title, messages.conversations.title), kind = EXCLUDED.kind
                       RETURNING id, external_id""",
                    (source_id, ext, [convs[e]["platform"] for e in ext], kinds,
                     [(convs[e].get("title") or None) for e in ext])).fetchall():
                cid[r["external_id"]] = r["id"]
        missing = sorted({m["conversation"] for m in msgs.values()} - set(cid))
        if missing:
            for r in c.execute("SELECT id, external_id FROM messages.conversations WHERE source_id = %s "
                               "AND external_id = ANY(%s)", (source_id, missing)).fetchall():
                cid[r["external_id"]] = r["id"]
        pairs = {(cid[e], hid[p]) for e, cv in convs.items() for p in cv["_parts"]}
        for m in list(msgs.values()):
            if m["conversation"] not in cid:
                rejected.append({"external_id": m["external_id"], "reason": "unknown_conversation"})
                del msgs[m["external_id"]]
            elif m["_sender"]:
                pairs.add((cid[m["conversation"]], hid[m["_sender"]]))
        if pairs:
            pl = sorted(pairs)
            c.execute("INSERT INTO messages.conversation_handles SELECT * FROM unnest(%s::uuid[], %s::uuid[]) "
                      "ON CONFLICT DO NOTHING", ([p[0] for p in pl], [p[1] for p in pl]))

        # ---- messages
        accepted = updated = 0
        touched_convs: set[uuid.UUID] = set()
        ids_by_ext: dict[str, uuid.UUID] = {}
        if msgs:
            ms = list(msgs.values())
            rows = c.execute(_UPSERT, {
                "src": source_id,
                "id": [uuid.uuid4() for _ in ms],
                "conv": [cid[m["conversation"]] for m in ms],
                "ext": [m["external_id"] for m in ms],
                "sender": [hid[m["_sender"]] if m["_sender"] else None for m in ms],
                "from_me": [bool(m["is_from_me"]) for m in ms],
                "sent": [m["_sent"] for m in ms],
                "edited": [_ts(m.get("edited_at")) for m in ms],
                "retracted": [bool(m.get("retracted")) for m in ms],
                "body": [m.get("body") for m in ms],
                "kind": [m.get("kind") or ("attachment" if m.get("attachments") and not m.get("body") else "text")
                         for m in ms],
                "meta": [json.dumps(m.get("platform_meta") or {}) for m in ms],
                "sens": [is_sensitive(m.get("body")) for m in ms],
            }).fetchall()
            for r in rows:
                ids_by_ext[r["external_id"]] = r["id"]
                touched_convs.add(r["conversation_id"])
                if r["inserted"]:
                    accepted += 1
                else:
                    updated += 1
            duplicates += len(ms) - len(rows)
            # attachments (metadata only) for messages that were stored or changed
            att = [(ids_by_ext[m["external_id"]], i, a) for m in ms if m["external_id"] in ids_by_ext
                   for i, a in enumerate(m.get("attachments") or [])]
            if att:
                mids = sorted({a[0] for a in att})
                c.execute("DELETE FROM messages.attachments WHERE message_id = ANY(%s)", (mids,))
                c.execute("""INSERT INTO messages.attachments (message_id, ordinal, filename, mime, size, sha256)
                             SELECT * FROM unnest(%s::uuid[], %s::smallint[], %s::text[], %s::text[], %s::bigint[], %s::text[])""",
                          ([a[0] for a in att], [a[1] for a in att], [a[2].get("filename") for a in att],
                           [a[2].get("mime") for a in att], [a[2].get("size") for a in att],
                           [a[2].get("sha256") for a in att]))
            replies = [(m["external_id"], m["reply_to"]) for m in ms if m.get("reply_to")]
            if replies:
                c.execute("""UPDATE messages.messages m SET reply_to_id = r.id
                               FROM unnest(%s::text[], %s::text[]) AS i(external_id, reply_ext)
                               JOIN messages.messages r ON r.source_id = %s AND r.external_id = i.reply_ext
                              WHERE m.source_id = %s AND m.external_id = i.external_id
                                AND m.reply_to_id IS DISTINCT FROM r.id""",
                          ([x[0] for x in replies], [x[1] for x in replies], source_id, source_id))

        # ---- edits and retractions of earlier messages
        later_updates, later_dupes = _edits_and_retractions(c, source_id, batch, rejected, touched_convs)
        updated += later_updates
        duplicates += later_dupes
        r_updates, r_dupes = _reactions(c, source_id, reactions, hid, region, rejected, touched_convs)
        updated += r_updates
        duplicates += r_dupes

        if touched_convs:
            c.execute("""UPDATE messages.conversations c SET message_count = s.n, last_message_at = s.last
                           FROM (SELECT conversation_id, count(*) AS n, max(sent_at) AS last FROM messages.messages
                                  WHERE conversation_id = ANY(%s) GROUP BY conversation_id) s
                          WHERE c.id = s.conversation_id""", (sorted(touched_convs),))
        c.execute("UPDATE messages.sources SET last_ingest_at = now(), "
                  "high_water = coalesce(%s, high_water) WHERE id = %s",
                  (Jsonb(batch["high_water"]) if batch.get("high_water") is not None else None, source_id))
        result = {"accepted": accepted, "updated": updated, "duplicates": duplicates, "rejected": rejected,
                  "new_handles": len(new_handles)}
        _audit(c, actor, "messages.source", source_id, "ingest",
               {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()})
        for h, platform in new_handles:
            fabric.publish(c, "messages", "messages.handle.created", fabric.ref("messages", "handle", h),
                           {"platform": platform})

    if new_handles:
        from cove.messages import service
        service._resolve([h for h, _ in new_handles])   # as the caller; the worker covers callers without people:read
    return result


def _edits_and_retractions(c, source_id, batch: dict, rejected: list, touched: set) -> tuple[int, int]:
    updated = dupes = 0
    edits = [e for e in batch.get("edits") or []]
    rets = [r for r in batch.get("retractions") or []]
    wanted = sorted({e["external_id"] for e in edits} | {r["external_id"] for r in rets})
    if not wanted:
        return 0, 0
    known = {r["external_id"] for r in c.execute("SELECT external_id FROM messages.messages WHERE source_id = %s "
                                                  "AND external_id = ANY(%s)", (source_id, wanted)).fetchall()}
    for x in [*edits, *rets]:
        if x["external_id"] not in known:
            rejected.append({"external_id": x["external_id"], "reason": "target_not_found"})
    edits = [e for e in edits if e["external_id"] in known and _ts(e["edited_at"])]
    if edits:
        rows = c.execute(
            """UPDATE messages.messages m SET body = e.body, edited_at = e.edited_at, is_sensitive = e.sens
                 FROM unnest(%s::text[], %s::text[], %s::timestamptz[], %s::bool[]) AS e(external_id, body, edited_at, sens)
                WHERE m.source_id = %s AND m.external_id = e.external_id AND NOT m.retracted
                  AND (m.edited_at IS NULL OR m.edited_at < e.edited_at)
            RETURNING m.conversation_id""",
            ([e["external_id"] for e in edits], [e["body"] for e in edits], [_ts(e["edited_at"]) for e in edits],
             [is_sensitive(e["body"]) for e in edits], source_id)).fetchall()
        updated += len(rows)
        dupes += len(edits) - len(rows)
        touched.update(r["conversation_id"] for r in rows)
    rets = [r for r in rets if r["external_id"] in known]
    if rets:
        rows = c.execute(
            """UPDATE messages.messages m SET retracted = true, body = NULL, is_sensitive = false,
                      platform_meta = m.platform_meta || jsonb_build_object('retracted_at', r.at)
                 FROM unnest(%s::text[], %s::text[]) AS r(external_id, at)
                WHERE m.source_id = %s AND m.external_id = r.external_id AND NOT m.retracted
            RETURNING m.conversation_id""",
            ([r["external_id"] for r in rets], [r.get("retracted_at") for r in rets], source_id)).fetchall()
        updated += len(rows)
        dupes += len(rets) - len(rows)
        touched.update(r["conversation_id"] for r in rows)
    return updated, dupes


def _reactions(c, source_id, reactions: list, hid: dict, region, rejected: list, touched: set) -> tuple[int, int]:
    """Fold tapbacks into their target's `reactions` map, keyed by who and what, in time order."""
    if not reactions:
        return 0, 0
    targets = sorted({r["target"] for r in reactions})
    rows = {r["external_id"]: r for r in c.execute(
        "SELECT id, external_id, conversation_id, reactions FROM messages.messages WHERE source_id = %s "
        "AND external_id = ANY(%s)", (source_id, targets)).fetchall()}
    changed: dict[str, dict] = {}
    applied = 0
    for r in sorted(reactions, key=lambda r: r.get("at") or ""):
        row = rows.get(r["target"])
        if row is None:
            rejected.append({"external_id": r["target"], "reason": "target_not_found"})
            continue
        who = "me" if r["is_from_me"] else (
            str(hid[(r["sender"]["platform"], handle_value(r["sender"]["platform"], r["sender"]["value"], region))])
            if r.get("sender") else "unknown")
        what = r["type"] if r["type"] != "emoji" else f"emoji:{r.get('emoji') or ''}"
        key = f"{who}:{what}"
        current = changed.get(r["target"], dict(row["reactions"] or {}))
        if r.get("removed"):
            current.pop(key, None)
        else:
            current[key] = {"type": r["type"], "emoji": r.get("emoji"), "from_me": bool(r["is_from_me"]),
                            "handle_id": None if who in ("me", "unknown") else who, "at": r.get("at")}
        changed[r["target"]] = current
        applied += 1
    real = {t: v for t, v in changed.items() if v != (rows[t]["reactions"] or {})}
    if real:
        ts = sorted(real)
        c.execute("""UPDATE messages.messages m SET reactions = x.reactions::jsonb
                       FROM unnest(%s::uuid[], %s::text[]) AS x(id, reactions) WHERE m.id = x.id""",
                  ([rows[t]["id"] for t in ts], [json.dumps(real[t]) for t in ts]))
        touched.update(rows[t]["conversation_id"] for t in ts)
    return len(real), applied - len(real) if applied > len(real) else 0


def ingest_state(source: str) -> dict:
    """Where a collector left off: {source, high_water, last_ingest_at} (high_water null for a new source)."""
    from cove.crm.service import _plain
    with db.conn() as c:
        row = c.execute("SELECT key, platform, label, high_water, last_ingest_at FROM messages.sources WHERE key = %s",
                        (source,)).fetchone()
    if not row:
        return {"source": source, "high_water": None, "last_ingest_at": None}
    return _plain({"source": row["key"], "platform": row["platform"], "label": row["label"],
                   "high_water": row["high_water"] or None, "last_ingest_at": row["last_ingest_at"]})


from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {
    _policy.PURE: ["validate"],
    "messages:ingest": ["ingest_batch", "ingest_state"],
})
