"""Notes on the fabric (docs/fabric.md): what Notes shares with other services.

Provides identity.describe (notes by ref), timeline.items (notes edited, and notes that mention a person)
and search.query. Mentions are found by the person's display name and aliases, which Notes asks for through
identity.describe: without People (or without `people:read`) there are no mention items, and the caller's
coverage already says People is missing.
"""

from __future__ import annotations

import re
import uuid
from datetime import timezone
from typing import Any

from cove import db, fabric
from cove.crm.service import _parse_dt
from cove.cursors import decode as decode_cursor, encode as encode_cursor

KEY = "notes"


def _iso(dt) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def file_ref(file_id: Any) -> str:
    return fabric.ref(KEY, "file", file_id)


def _file_ids(refs: list[str]) -> list[uuid.UUID]:
    out = []
    for r in refs or []:
        p = fabric.parse_ref(r)
        if p and p[0] == KEY and p[1] == "file":
            try:
                out.append(uuid.UUID(p[2]))
            except ValueError:
                pass
    return out


def _excerpt(body: str | None, around: str | None = None, width: int = 220) -> str | None:
    text = re.sub(r"^---.*?---\s*", "", body or "", flags=re.S).strip()
    if not text:
        return None
    if around:
        m = re.search(re.escape(around), text, re.I)
        if m:
            start = max(0, m.start() - width // 3)
            return ("…" if start else "") + text[start:start + width].replace("\n", " ")
    return text[:width].replace("\n", " ")


# ---------------------------------------------------------------- identity.describe
def describe(refs: list[str]) -> list[dict]:
    with db.conn() as c:
        rows = c.execute("SELECT id, kind, path, title, tags, aliases, body FROM notes.files "
                         "WHERE id = ANY(%s) AND deleted_at IS NULL", (_file_ids(refs),)).fetchall()
    return [{"ref": file_ref(r["id"]), "label": r["title"] or r["path"], "kind": r["kind"],
             "url": f"/notes/view/{r['id']}" if r["kind"] != "attachment" else f"/notes/file/{r['id']}/raw",
             "summary": r["path"], "aliases": list(r["aliases"] or [])} for r in rows]


# ---------------------------------------------------------------- timeline.items
def _names(subject_ref: str) -> list[str]:
    """The person's names, from whoever owns the ref (People), through the fabric."""
    owner = fabric.parse_ref(subject_ref)[0]
    r = fabric.ask("identity.describe", provider=owner, refs=[subject_ref])
    if not r.ok:
        return []
    names = []
    for d in r.value or []:
        names += [d.get("label") or "", *(d.get("aliases") or [])]
    return sorted({n.strip() for n in names if len(n.strip()) >= 3}, key=len, reverse=True)[:8]


def timeline(subject_ref: str | None = None, since: str | None = None, until: str | None = None,
             limit: int = 50, cursor: str | None = None) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    where, params = ["deleted_at IS NULL", "kind = 'note'"], []
    names: list[str] = []
    if subject_ref:
        p = fabric.parse_ref(subject_ref)
        if not p or p[0] == KEY:
            return {"items": [], "next_cursor": None}
        names = _names(subject_ref)
        if not names:
            return {"items": [], "next_cursor": None}
        # whole-word, case-insensitive mention of any name, or a [[link]] to a note named after them
        pattern = "|".join(re.escape(n) for n in names)
        where.append("(body ~* %s OR id IN (SELECT from_id FROM notes.links WHERE target_key = ANY(%s)))")
        params += [rf"\m({pattern})\M", [n.lower() for n in names]]
    if since:
        where.append("updated_at >= %s")
        params.append(_parse_dt(since))
    if until:
        where.append("updated_at < %s")
        params.append(_parse_dt(until))
    after = decode_cursor(cursor, 2)
    if after:
        where.append("(updated_at, id) < (%s::timestamptz, %s::uuid)")
        params += after
    with db.conn() as c:
        rows = c.execute(f"SELECT id, path, title, body, created_at, updated_at FROM notes.files "
                         f"WHERE {' AND '.join(where)} ORDER BY updated_at DESC, id DESC LIMIT %s",
                         [*params, limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        hit = next((n for n in names if re.search(rf"\b{re.escape(n)}\b", r["body"] or "", re.I)), None)
        items.append({"ref": file_ref(r["id"]), "service": KEY,
                      "kind": "note.mention" if subject_ref else ("note.created" if r["created_at"] == r["updated_at"]
                                                                  else "note.edited"),
                      "occurred_at": _iso(r["updated_at"]), "title": r["title"] or r["path"],
                      "excerpt": _excerpt(r["body"], hit), "participants": [subject_ref] if subject_ref else [],
                      "url": f"/notes/view/{r['id']}"})
    return {"items": items,
            "next_cursor": encode_cursor(_iso(rows[-1]["updated_at"]), str(rows[-1]["id"])) if more and rows else None}


# ---------------------------------------------------------------- search.query
def search(q: str, limit: int = 20) -> list[dict]:
    from cove.notes import service as notes
    limit = max(1, min(int(limit or 20), 100))
    return [{"ref": file_ref(r["id"]), "service": KEY, "title": r["title"] or r["path"],
             "excerpt": (r.get("excerpt") or None), "score": round(1 - i / (limit + 1), 3),
             "occurred_at": r["updated_at"]} for i, r in enumerate(notes.search(q, limit=limit))]


# Providers are checked by the fabric before it calls them, and again here for direct callers.
from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {_policy.PURE: ['file_ref'], 'notes:read': ['describe', 'timeline', 'search']})


def _cap(name, fn, description):
    return fabric.Capability(name, fn, "notes:read", description)


MANIFEST = fabric.register(fabric.Manifest(
    key=KEY, version=1, read_scope="notes:read",
    provides={
        "identity.describe": _cap("identity.describe", describe, "Notes, canvases and attachments, by ref"),
        "timeline.items": _cap("timeline.items", timeline, "Notes edited, and notes that mention a person"),
        "search.query": _cap("search.query", search, "Your notes vault"),
    },
    consumes=("identity.describe",),
    publishes=("notes.file.changed",),
))
