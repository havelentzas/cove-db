"""cove MCP server (streamable HTTP at http://127.0.0.1:8765/mcp).

Thin adapter over ``cove.crm.service``: every tool delegates to the same
functions the web UI uses, tagging writes with ``mcp:<client name>``.
"""

from __future__ import annotations

import functools
import os
from typing import Any, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from cove.crm import service as svc

INSTRUCTIONS = """cove-db holds the user's personal databases. Currently: a personal CRM (people, interactions,
follow-up reminders, tags/groups, relationships). Contacts are JSContact (RFC 9553) cards.

Start a session with `describe_system` (domains, tables, counts) and `read_notes` (durable notes left by
earlier agents about the user's preferences and conventions). When you learn something worth remembering
across sessions, save it with `write_note`.

Writes go through the domain tools (never SQL). Updating a contact needs its current `version`; on a
version conflict, re-read the contact and retry. The user also edits this data in a web UI at
http://127.0.0.1:8080, so re-read before editing rather than relying on stale results."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

mcp = MCPServer(name="cove", title="cove-db", instructions=INSTRUCTIONS, version="0.1.0")


def _actor(ctx: Context | None) -> str:
    name = None
    try:
        params = ctx.session.client_params if ctx else None
        name = params.client_info.name if params and params.client_info else None
    except Exception:
        name = None
    return f"mcp:{name or 'unknown'}"


async def _call(fn, *args, **kwargs) -> Any:
    """Run a blocking service call in a worker thread and map errors to ToolError."""
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    except svc.Conflict as e:
        raise ToolError(f"Version conflict: {e}. Current version is {e.current_version}.") from None
    except (svc.NotFound, svc.Invalid) as e:
        raise ToolError(str(e)) from None


# ============================================================== system / meta
@mcp.tool(annotations=READ, meta={"service": "core"})
async def describe_system() -> dict:
    """Overview of cove-db: registered domains and their conventions, every table with its columns,
    row counts, agent-note topics and recent schema changes. Call this first."""
    return await _call(svc.describe_system)


@mcp.tool(annotations=READ, meta={"service": "core"})
async def read_notes(topic: str | None = None) -> list[dict]:
    """Read durable notes that agents have saved about the user and this system (optionally one topic)."""
    return await _call(svc.read_notes, topic)


@mcp.tool(annotations=WRITE, meta={"service": "core"})
async def write_note(topic: str, note: str, ctx: Context, note_id: int | None = None) -> dict:
    """Save a durable note for future agent sessions (e.g. topic 'preferences': 'User calls their
    sister Meg'). Pass note_id to update an existing note instead of adding a new one."""
    return await _call(svc.write_note, topic, note, _actor(ctx), note_id)


@mcp.tool(annotations=READ, meta={"service": "core"})
async def sql_query(sql: str, max_rows: int = 200) -> dict:
    """Run a read-only SQL query (Postgres) for questions the other tools can't answer.
    Schemas: crm.*, meta.*. Useful views: crm.contact_overview, crm.follow_ups_due.
    Runs as a read-only role with a 5 second timeout."""
    try:
        return await _call(svc.sql_query, sql, max_rows)
    except ToolError:
        raise
    except Exception as e:  # SQL errors are useful feedback for the agent
        raise ToolError(f"SQL error: {e}") from None


# ============================================================== contacts
@mcp.tool(annotations=READ, meta={"service": "people"})
async def search_contacts(query: str | None = None, tag: str | None = None, due_only: bool = False,
                          include_archived: bool = False, limit: int = 50) -> list[dict]:
    """Find contacts by name, email, phone digits or note text (fuzzy), optionally filtered to a tag/group
    or to contacts whose follow-up is due. Returns summaries; use get_contact for full details."""
    return await _call(svc.search_contacts, query, tag, due_only, include_archived, limit)


@mcp.tool(annotations=READ, meta={"service": "people"})
async def get_contact(contact_id: str, interactions: int = 10) -> dict:
    """Full contact: JSContact card, version (needed for updates), tags, relationships,
    recent interactions and follow-up status."""
    return await _call(svc.get_contact, contact_id, interactions=interactions)


@mcp.tool(annotations=WRITE, meta={"service": "people"})
async def create_contact(ctx: Context, full_name: str | None = None, given: str | None = None,
                         surname: str | None = None, emails: list[str] | None = None,
                         phones: list[str] | None = None, organization: str | None = None,
                         title: str | None = None, birthday: str | None = None, notes: str | None = None,
                         how_met: str | None = None, links: list[str] | None = None,
                         tags: list[str] | None = None, card: dict | None = None) -> dict:
    """Create a contact from simple fields, or pass `card` as a complete JSContact Card (RFC 9553) for
    anything richer. birthday: 'YYYY-MM-DD' or '--MM-DD'. Search first to avoid duplicates."""
    if card is None:
        card = await _call(svc.build_card, full_name=full_name, given=given, surname=surname,
                           emails=emails or [], phones=phones or [], organization=organization, title=title,
                           birthday=birthday, notes=notes, how_met=how_met, links=links or [])
    if tags:
        card = {**card, "keywords": {**card.get("keywords", {}), **{t: True for t in tags}}}
    return await _call(svc.create_contact, card, _actor(ctx))


@mcp.tool(annotations=WRITE, meta={"service": "people"})
async def update_contact(contact_id: str, patch: dict, expected_version: int, ctx: Context) -> dict:
    """Edit a contact's JSContact card with a JSON Merge Patch (RFC 7396): keys you send are replaced,
    keys set to null are removed, everything else is kept. Collections are id-keyed maps, e.g.
    {"emails": {"e2": {"address": "new@x.com"}}} adds an email; {"emails": {"e1": null}} removes one.
    expected_version must equal the contact's current version (from get_contact)."""
    return await _call(svc.update_contact, contact_id, patch, expected_version, _actor(ctx))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False), meta={"service": "people"})
async def archive_contact(contact_id: str, ctx: Context, archived: bool = True) -> dict:
    """Archive (hide) a contact, or restore it with archived=false. Nothing is deleted."""
    return await _call(svc.archive_contact, contact_id, _actor(ctx), archived)


# ============================================================== CRM
@mcp.tool(annotations=WRITE, meta={"service": "people"})
async def log_interaction(contact_ids: list[str], summary: str, ctx: Context,
                          kind: Literal["call", "meeting", "message", "email", "other"] = "other",
                          occurred_at: str | None = None, notes: str | None = None,
                          calendar_event_id: str | None = None) -> dict:
    """Record a call/meeting/message with one or more contacts. occurred_at is ISO 8601 (default now).
    calendar_event_id can link a Google Calendar event. Logging resets the follow-up clock."""
    return await _call(svc.log_interaction, contact_ids, summary, _actor(ctx), kind, occurred_at, notes,
                       calendar_event_id)


@mcp.tool(annotations=READ, meta={"service": "people"})
async def list_interactions(contact_id: str | None = None, since: str | None = None, limit: int = 20) -> list[dict]:
    """Recent interactions, newest first — for one contact or everyone, optionally since an ISO date."""
    return await _call(svc.list_interactions, contact_id, limit, since)


@mcp.tool(annotations=WRITE, meta={"service": "people"})
async def set_follow_up(contact_id: str, ctx: Context, every_days: int | None = None,
                        snoozed_until: str | None = None) -> dict:
    """Set how often to reach out to a contact (every_days), optionally snoozing until an ISO date.
    every_days=null removes the reminder."""
    return await _call(svc.set_follow_up, contact_id, every_days, _actor(ctx), snoozed_until)


@mcp.tool(annotations=READ, meta={"service": "people"})
async def follow_ups_due(limit: int = 50) -> list[dict]:
    """Contacts the user is due to reach out to (last contact + cadence has passed), most overdue first."""
    return await _call(svc.follow_ups_due, limit)


@mcp.tool(annotations=READ, meta={"service": "people"})
async def list_tags() -> list[dict]:
    """All tags and groups with how many active contacts each has."""
    return await _call(svc.list_tags)


@mcp.tool(annotations=WRITE, meta={"service": "people"})
async def tag_contacts(contact_ids: list[str], tags: list[str], ctx: Context,
                       kind: Literal["tag", "group"] = "tag") -> list[dict]:
    """Add tags to contacts; unknown tags are created with the given kind ('group' for things like
    'Family' or 'Climbing club')."""
    return await _call(svc.tag_contacts, contact_ids, tags, _actor(ctx), kind)


@mcp.tool(annotations=WRITE, meta={"service": "people"})
async def untag_contacts(contact_ids: list[str], tags: list[str], ctx: Context) -> str:
    """Remove tags from contacts."""
    await _call(svc.untag_contacts, contact_ids, tags, _actor(ctx))
    return "ok"


@mcp.tool(annotations=WRITE, meta={"service": "people"})
async def relate_contacts(from_id: str, to_id: str, relation: str, ctx: Context, note: str | None = None) -> dict:
    """Record that `from` relates to `to`. relation uses the vCard/JSContact vocabulary where possible:
    friend, spouse, parent, child, sibling, colleague, co-worker, neighbor, acquaintance, met, emergency
    (custom words are allowed)."""
    return await _call(svc.relate_contacts, from_id, to_id, relation, _actor(ctx), note)


@mcp.tool(annotations=READ, meta={"service": "people"})
async def export_vcard(contact_ids: list[str] | None = None, tag: str | None = None) -> str:
    """Export contacts (all active, a list of ids, or one tag) as vCard 4.0 text."""
    return await _call(svc.export_vcard, contact_ids, tag)


def main() -> None:
    port = int(os.environ.get("COVE_MCP_PORT", "8765"))
    # Reachable via the 127.0.0.1 port mapping and (optionally) `tailscale serve`. Any other
    # Host/Origin header is rejected so a web page in a browser can't talk to it (DNS rebinding).
    # COVE_MCP_EXTRA_HOSTS: comma-separated extra hostnames, e.g. the machine's tailnet name.
    extra = [h.strip() for h in os.environ.get("COVE_MCP_EXTRA_HOSTS", "").split(",") if h.strip()]
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"]
    origins = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    for h in extra:
        hosts += [h, f"{h}:*"]
        origins += [f"https://{h}", f"https://{h}:*"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins,
    )
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, transport_security=security)


if __name__ == "__main__":
    main()
