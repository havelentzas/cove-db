"""Panels one service shows on another service's page, built only from fabric capabilities.

The Messages panel on a contact's page is the reference example of a non-critical integration:
- it asks Messages through the fabric (never its tables), as the signed-in user;
- if Messages is missing, turned off, not readable by this user, failing or slow, the panel simply isn't
  there, and the rest of the page is exactly what People shows on its own;
- each call has a short timeout, so a slow Messages can't slow the contact page down by more than that.
"""

from __future__ import annotations

from cove import fabric

TIMEOUT = 1.5   # seconds per call: the panel is a nicety


def messages_for_contact(contact_id: str) -> dict | None:
    """Last messaged, threads awaiting a reply and recent conversation days for one contact; None = no panel."""
    ref = fabric.ref("people", "contact", contact_id)
    last = fabric.ask("activity.last_contact", provider="messages", refs=[ref], timeout=TIMEOUT)
    if not last.ok:
        return None
    recent = fabric.ask("timeline.items", provider="messages", subject_ref=ref, limit=6, timeout=TIMEOUT)
    waiting = fabric.ask("activity.awaiting_reply", provider="messages", refs=[ref], days=30, limit=3,
                         timeout=TIMEOUT)
    return {"ref": ref, "last": (last.value or {}).get(ref),
            "recent": recent.value["items"] if recent.ok else [],
            "waiting": waiting.value if waiting.ok else [],
            "partial": not (recent.ok and waiting.ok)}
