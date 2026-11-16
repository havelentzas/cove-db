"""cove brand registry (brand/services.json) for templates in any service."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

BRAND_DIR = Path(os.environ.get("COVE_BRAND_DIR", Path(__file__).resolve().parents[3] / "brand"))


@lru_cache(maxsize=1)
def registry() -> dict:
    return json.loads((BRAND_DIR / "services.json").read_text())


def service(key: str) -> dict:
    for s in registry()["services"]:
        if s["key"] == key:
            return s
    raise KeyError(key)


def mark_svg() -> str:
    """The cove mark, inline (inherits colour via currentColor)."""
    svg = (BRAND_DIR / "logo" / "mark.svg").read_text()
    return svg.replace("<svg ", '<svg class="mark" aria-hidden="true" ', 1)
