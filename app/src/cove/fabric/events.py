"""Change events: an outbox in each workspace (meta.events) that consumers pull with their own cursor.

Producers call `publish(c, …)` with the connection of the write that caused the event, so the event commits
(or rolls back) with it. Payloads are small and never carry message text. Consumers are declared in service
manifests and run by the worker (cove.fabric.worker); a failing consumer doesn't advance its cursor.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from psycopg.types.json import Jsonb

from cove import db
from cove.auth import policy

log = logging.getLogger("cove.fabric")
RETENTION_DAYS = 30


def publish(c, service: str, type: str, ref: str | None = None, payload: dict | None = None) -> None:
    """Record an event on connection `c`, inside the caller's transaction."""
    c.execute("INSERT INTO meta.events (service, type, ref, payload) VALUES (%s, %s, %s, %s)",
              (service, type, ref, Jsonb(payload or {})))


def _readable_services() -> list[str]:
    from cove.fabric.core import manifests
    from cove.fabric.services import states
    st = states()
    return [k for k, m in manifests().items() if st.get(k) == "enabled" and policy.allows(m.read_scope)]


def events(since: int = 0, types: Iterable[str] | None = None, limit: int = 500) -> dict:
    """Events after `since`, oldest first, from services the caller may read: {events, last_seq, more}."""
    from cove.crm.service import _plain
    limit = max(1, min(int(limit), 1000))
    types = [t for t in (types or []) if t]
    with db.conn() as c:
        rows = c.execute(
            "SELECT seq, service, type, ref, occurred_at, payload FROM meta.events "
            "WHERE seq > %s AND service = ANY(%s) AND (cardinality(%s::text[]) = 0 OR type = ANY(%s)) "
            "ORDER BY seq LIMIT %s",
            (int(since), _readable_services(), types, types, limit + 1)).fetchall()
        head = c.execute("SELECT coalesce(max(seq), 0) AS s FROM meta.events").fetchone()["s"]
    more = len(rows) > limit
    rows = rows[:limit]
    return _plain({"events": rows, "last_seq": rows[-1]["seq"] if rows else int(since), "head_seq": head, "more": more})


def consume(consumer: str, types: Iterable[str], handler: Callable[[list[dict]], Any], batch: int = 500) -> int:
    """Feed new events of `types` to `handler`, advancing the consumer's cursor only after it succeeds.

    Returns how many events were handled. If the handler raises, the cursor stays put (retried next round)
    and the error class is recorded on the cursor row."""
    types = list(types)
    with db.conn() as c:
        c.execute("INSERT INTO meta.event_cursors (consumer, last_seq) VALUES (%s, 0) ON CONFLICT DO NOTHING", (consumer,))
        since = c.execute("SELECT last_seq FROM meta.event_cursors WHERE consumer = %s", (consumer,)).fetchone()["last_seq"]
        rows = c.execute("SELECT seq, service, type, ref, occurred_at, payload FROM meta.events "
                         "WHERE seq > %s ORDER BY seq LIMIT %s", (since, batch)).fetchall()
    if not rows:
        return 0
    wanted = [r for r in rows if r["type"] in types]
    try:
        if wanted:
            handler(wanted)
    except Exception as e:  # noqa: BLE001 - a broken consumer only hurts itself
        log.warning("fabric: consumer %s failed: %s", consumer, type(e).__name__)
        with db.conn() as c:
            c.execute("UPDATE meta.event_cursors SET last_error = %s, updated_at = now() WHERE consumer = %s",
                      (type(e).__name__, consumer))
        return 0
    with db.conn() as c:
        c.execute("UPDATE meta.event_cursors SET last_seq = %s, last_error = NULL, updated_at = now() "
                  "WHERE consumer = %s", (rows[-1]["seq"], consumer))
    return len(wanted)


def prune(days: int = RETENTION_DAYS) -> int:
    """Delete events older than `days` that every consumer has already seen."""
    with db.conn() as c:
        r = c.execute("DELETE FROM meta.events WHERE occurred_at < now() - make_interval(days => %s) "
                      "AND seq <= coalesce((SELECT min(last_seq) FROM meta.event_cursors), 0)", (int(days),))
        return r.rowcount


def cursors() -> list[dict]:
    from cove.crm.service import _plain
    with db.conn() as c:
        return _plain(c.execute("SELECT consumer, last_seq, updated_at, last_error FROM meta.event_cursors "
                                "ORDER BY consumer").fetchall())


from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {
    # publish/consume/prune are called by service code and the worker (which runs as the workspace owner);
    # events() filters to services the caller can read.
    None: ["publish", "events", "consume", "prune", "cursors"],
})
