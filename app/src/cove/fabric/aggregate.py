"""Cross-service answers built only from capabilities: "everything about X" and "search everything".

Nothing here knows any service by name. Each part comes from `fabric.gather`, and the result carries the
merged `coverage`, so a missing or failing service shows up as missing instead of as "nothing found".
"""

from __future__ import annotations

import re
from typing import Any

from cove.fabric.core import ask, coverage, gather, parse_ref

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[\d\s().-]{6,}$")


def identifier(text: str) -> str:
    """Normalise free text into the form identity.resolve expects (email lower-cased, phone digits kept)."""
    t = (text or "").strip()
    if _EMAIL.match(t):
        return t.lower()
    if _PHONE.match(t):
        return ("+" if t.startswith("+") else "") + re.sub(r"\D", "", t)
    return t


def resolve_subject(text: str) -> dict:
    """{ref, candidates, coverage}: a ref passes straight through; anything else goes to identity.resolve.

    ref is None when nothing matched or when several people match about equally well (see candidates)."""
    if parse_ref(text):
        return {"ref": text.strip(), "candidates": [], "coverage": coverage()}
    g = gather("identity.resolve", identifiers=[identifier(text)])
    cands = sorted((dict(m, service=s) for s, ms in g.values.items() for m in (ms or [])),
                   key=lambda m: (-m["confidence"], m["service"] != "people", m["label"]))
    chosen = None
    if cands:
        top = cands[0]
        rivals = [c for c in cands[1:] if c["confidence"] >= top["confidence"] - 0.05 and c["ref"] != top["ref"]
                  and parse_ref(c["ref"])[0] == parse_ref(top["ref"])[0]]
        chosen = None if rivals else top["ref"]
    return {"ref": chosen, "candidates": cands[:10], "coverage": g.coverage}


def _ts(item: dict) -> str:
    return item.get("occurred_at") or ""


def context_for(ref_or_name: str, since: str | None = None, until: str | None = None, limit: int = 50) -> dict:
    """Everything about a person (or any ref): description, last contact, awaiting replies and a merged timeline."""
    limit = max(1, min(int(limit), 200))
    subject = resolve_subject(ref_or_name)
    ref = subject["ref"]
    if ref is None:
        return {"subject": None, "candidates": subject["candidates"], "timeline": [], "last_contact": {},
                "awaiting_reply": [], "coverage": subject["coverage"],
                "note": ("Several matches; call again with one of the candidate refs." if subject["candidates"]
                         else "Nobody matched. Try a full name, phone number or email.")}
    owner = parse_ref(ref)[0]
    desc = ask("identity.describe", provider=owner, refs=[ref])
    described = next((d for d in (desc.value or []) if d["ref"] == ref), None) if desc.ok else None
    timeline = gather("timeline.items", subject_ref=ref, since=since, until=until, limit=limit)
    last = gather("activity.last_contact", refs=[ref])
    waiting = gather("activity.awaiting_reply", refs=[ref], days=1, limit=10)
    items = sorted((i for v in timeline.values.values() for i in (v or {}).get("items", [])), key=_ts, reverse=True)
    last_contact: dict[str, Any] = {s: v[ref] for s, v in last.values.items() if v and ref in v}
    return {
        "subject": described or {"ref": ref, "label": None, "kind": parse_ref(ref)[1], "url": None, "summary": None},
        "candidates": subject["candidates"][1:4] if subject["candidates"] else [],
        "last_contact": last_contact,
        "awaiting_reply": [w for v in waiting.values.values() for w in (v or [])],
        "timeline": items[:limit],
        "coverage": coverage(subject["coverage"], [desc] if desc.service == owner else [], timeline.results,
                             last.results, waiting.results),
    }


def search_everything(q: str, limit: int = 20) -> dict:
    limit = max(1, min(int(limit), 100))
    g = gather("search.query", q=q, limit=limit)
    hits = sorted((h for v in g.values.values() for h in (v or [])),
                  key=lambda h: (-(h.get("score") or 0), h.get("occurred_at") or ""))
    return {"hits": hits[:limit], "coverage": g.coverage}


from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {
    _policy.PURE: ["identifier"],
    None: ["resolve_subject", "context_for", "search_everything"],   # each capability checks its own scope
})
