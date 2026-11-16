"""Opaque pagination cursors: the sort key of the last item, base64-encoded JSON. Shared by every service."""

from __future__ import annotations

import base64
import json
from typing import Any


def encode(*values: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, default=str).encode()).decode().rstrip("=")


def decode(cursor: str | None, n: int) -> list | None:
    """The values in `cursor` (None for no cursor). A malformed cursor is Invalid, not a crash."""
    from cove.crm.service import Invalid
    if not cursor:
        return None
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except ValueError:
        raise Invalid("bad cursor") from None
    if not isinstance(values, list) or len(values) != n:
        raise Invalid("bad cursor")
    return values
