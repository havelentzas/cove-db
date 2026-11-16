"""Messages JSON API: /api/v1/messages/… (docs/messages.md).

- Ingest (collectors, `cove_ing_` token or anyone with messages:ingest): POST /ingest/batch, GET /ingest/state.
- Reading and curating: the same service functions as the web UI and MCP, so the agent rules apply to
  personal-token callers (hidden conversations and one-time codes left out).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from cove.crm.service import Invalid
from cove.messages import ingest, service as messages
from cove.web.api_v1 import PREFIX, service_on

router = APIRouter(prefix=f"{PREFIX}/messages", tags=["messages"], dependencies=[service_on("messages")])


def _actor(request: Request) -> str:
    from cove import context
    p = context.current()
    return p.actor if p else "api"


# ---------------------------------------------------------------- ingest (collectors)
@router.post("/ingest/batch")
async def ingest_batch(request: Request) -> dict:
    """Store a batch in the cove.messages.ingest/v1 format (schemas/contracts/messages-ingest.v1.json).
    Idempotent. Returns {accepted, updated, duplicates, rejected: [{external_id, reason}]}."""
    from starlette.concurrency import run_in_threadpool
    try:
        batch = json.loads(await request.body())
    except ValueError:
        raise Invalid("the body must be JSON") from None
    if not isinstance(batch, dict):
        raise Invalid("the body must be a JSON object")
    return await run_in_threadpool(ingest.ingest_batch, batch, _actor(request))


@router.post("/ingest/whatsapp")
async def ingest_whatsapp(request: Request) -> dict:
    """Import a WhatsApp "Export chat" file (.txt or .zip) as multipart form data: `file`, and optionally
    `my_name` (how you appear in the export), `title` and `date_order` (DMY, MDY or YMD). Re-importing a
    newer export of the same chat only adds what's new."""
    from starlette.concurrency import run_in_threadpool
    from cove.messages.importers import whatsapp
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise Invalid("attach the export as the `file` field")
    data = await upload.read()
    await form.close()
    order = (str(form.get("date_order") or "").upper() or None)
    if order not in (None, "DMY", "MDY", "YMD"):
        raise Invalid("date_order must be DMY, MDY or YMD")
    return await run_in_threadpool(whatsapp.import_export, upload.filename or "chat.txt", data, _actor(request),
                                   str(form.get("my_name") or "").strip() or None,
                                   str(form.get("title") or "").strip() or None, order)


@router.get("/ingest/state")
def ingest_state(source: str = Query(..., min_length=1, max_length=100)) -> dict:
    """Where a collector left off: the `high_water` it sent with its last successful batch."""
    return ingest.ingest_state(source)


# ---------------------------------------------------------------- reading
@router.get("/conversations")
def list_conversations(person: str | None = None, q: str | None = None, platform: str | None = None,
                       active_since: str | None = None, include_archived: bool = False, unresolved: bool = False,
                       cursor: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict:
    """Conversations, most recently active first. `person`: a contact id or ref, a phone/email, or a name."""
    return messages.list_conversations(person, q, platform, active_since, include_archived, unresolved, cursor, limit)


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, around: str | None = None, before: int = Query(50, ge=0, le=100),
                     after: int = Query(0, ge=0, le=100)) -> dict:
    """A window of a thread: the latest messages, or `before`/`after` around a message id or a date."""
    return messages.get_conversation(conversation_id, None, around, before, after)


@router.get("/search")
def search(q: str | None = None, person: str | None = None, conversation: str | None = None,
           platform: str | None = None, since: str | None = None, until: str | None = None,
           from_me: bool | None = None, limit: int = Query(50, ge=1, le=100)) -> dict:
    """Messages matching words and filters, newest first."""
    return messages.search_messages(q, person, conversation, platform, since, until, from_me, limit)


@router.get("/changes")
def changes(since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000)) -> dict:
    """The message change feed: messages stored or changed after `since` (a `seq`), oldest first."""
    return messages.changes(since, limit)


@router.get("/activity")
def activity(person: str | None = None, conversation: str | None = None,
             period_days: int = Query(90, ge=1, le=3650)) -> dict:
    """Volume, last in/out, typical reply times and the trend, for a person or a conversation."""
    return messages.message_activity(person, conversation, period_days)


@router.get("/awaiting-reply")
def awaiting_reply(days: int = Query(14, ge=1, le=365), limit: int = Query(20, ge=1, le=200),
                   person: str | None = None) -> dict:
    """Threads whose last message is from someone else and still unanswered, longest wait first."""
    return messages.awaiting_reply(days, limit, person)


@router.get("/handles")
def handles(state: str | None = None, q: str | None = None, limit: int = Query(200, ge=1, le=1000)) -> dict:
    """Senders and their link to People. state: suggested | none | linked | me."""
    return messages.list_handles(state, q, limit)


@router.get("/sources")
def sources() -> dict:
    """Collectors and importers, with when each last sent something."""
    return {"items": messages.list_sources()}


# ---------------------------------------------------------------- curating (messages:write)
class Link(BaseModel):
    contact: str


class Visible(BaseModel):
    visible: bool


@router.put("/handles/{handle}/contact")
def link_handle(handle: str, body: Link, request: Request) -> dict:
    """Link a sender to a person in People (contact id or ref)."""
    return messages.link_handle(handle, body.contact, _actor(request))


@router.delete("/handles/{handle}/contact")
def unlink_handle(handle: str, request: Request) -> dict:
    return messages.unlink_handle(handle, _actor(request))


@router.put("/conversations/{conversation_id}/agent-visible")
def set_agent_visible(conversation_id: str, body: Visible, request: Request) -> dict:
    """Show or hide a conversation from agents."""
    return messages.set_conversation_agent_visible(conversation_id, body.visible, _actor(request))
