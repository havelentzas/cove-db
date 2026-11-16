"""The cove ethos at /ethos: ETHOS.md (repo root, copied into the image) rendered as a page.

The file is read on every request, so editing it changes the page; there is nothing to keep in sync.
Its structure is plain Markdown: `#` title and intro, `##` sections, `###` subsections. A subsection
titled "N. Something" is a principle, whose paragraphs are the statement, then **Why:** and **In practice:**.
Anything that doesn't fit that shape is still rendered, just without the principle styling.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from cove import brand
from cove.notes.markdown import render_markdown
from cove.web.hub import CORE

router = APIRouter(tags=["core"])

# Next to brand/: the repo root in a checkout, /srv in the image (app/Dockerfile copies it there).
ETHOS_PATH = Path(os.environ.get("COVE_ETHOS_PATH", brand.BRAND_DIR.parent / "ETHOS.md"))

PRINCIPLE_RE = re.compile(r"^(\d+)\.\s+(.+)$")
LABEL_RE = re.compile(r"^\*\*(Why|In practice):\*\*\s*", re.I)
META_RE = re.compile(r"\*\*(Version|Last reviewed):\*\*\s*([^·\n*]+)", re.I)


def slug(text: str) -> str:
    s = re.sub(r"[`*_]", "", text.lower())
    s = re.sub(r"^\d+\.\s+", "", s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "section"


def _split(lines: list[str], marker: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split lines on headings that start with `marker` (outside code fences): (preamble, [(heading, body)])."""
    pre: list[str] = []
    parts: list[tuple[str, list[str]]] = []
    fence = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and line.startswith(marker + " "):
            parts.append((line[len(marker) + 1:].strip(), []))
        elif parts:
            parts[-1][1].append(line)
        else:
            pre.append(line)
    return pre, parts


def _paragraphs(lines: list[str]) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", "\n".join(lines)) if p.strip()]


def _principle(number: str, title: str, body: list[str]) -> dict:
    statement, fields, rest = "", {}, []
    for p in _paragraphs(body):
        m = LABEL_RE.match(p)
        if m:
            fields[m.group(1).lower()] = render_markdown(p[m.end():])
        elif not statement:
            statement = render_markdown(p)
        else:
            rest.append(p)
    return {"number": int(number), "title": title, "id": slug(title), "statement": statement,
            "why": fields.get("why", ""), "practice": fields.get("in practice", ""),
            "extra": render_markdown("\n\n".join(rest)) if rest else ""}


def parse(text: str) -> dict:
    lines = text.splitlines()
    pre, sections = _split(lines, "##")
    _, titled = _split(pre, "#")
    title, intro = (titled[0][0], titled[0][1]) if titled else ("The cove ethos", pre)
    out = {"title": title, "intro": render_markdown("\n".join(intro)), "sections": [], "principles": [],
           "meta": {k.lower(): v.strip() for k, v in META_RE.findall(text)}}
    for heading, body in sections:
        sec_pre, subs = _split(body, "###")
        sec = {"title": heading, "id": slug(heading), "body": render_markdown("\n".join(sec_pre)),
               "subsections": [], "principles": []}
        for sub_title, sub_body in subs:
            m = PRINCIPLE_RE.match(sub_title)
            if m:
                sec["principles"].append(_principle(m.group(1), m.group(2).strip(), sub_body))
            else:
                sec["subsections"].append({"title": sub_title, "id": slug(sub_title),
                                           "body": render_markdown("\n".join(sub_body))})
        out["principles"] += sec["principles"]
        out["sections"].append(sec)
    return out


@router.get("/ethos", response_class=HTMLResponse)
def ethos(request: Request):
    """The principles every cove service is built by (rendered from ETHOS.md)."""
    from cove.web.app import pages  # shared Jinja environment
    try:
        doc = parse(ETHOS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return pages.TemplateResponse(request, "ethos.html", {"doc": None, "svc": CORE}, status_code=404)
    return pages.TemplateResponse(request, "ethos.html", {"doc": doc, "svc": CORE})
