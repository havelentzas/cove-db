"""Notes pages (/notes) and the device-token page (/settings/devices)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

from cove import brand
from cove.crm.service import NotFound
from cove.notes import markdown as md
from cove.notes import service as notes

router = APIRouter(tags=["notes"])
settings = APIRouter(tags=["core"])
SVC = None


def _svc() -> dict:
    global SVC
    SVC = SVC or brand.service("notes")
    return SVC


def _pages():
    from cove.web.app import pages
    return pages


def _actor(request: Request) -> str:
    from cove.web.app import actor
    return actor(request)


def _resolver(targets: list[str]):
    found = notes.resolve(targets)

    def resolve(target: str):
        hit = found.get(md.link_key(target))
        if not hit:
            return f"/notes/open?target={quote(target)}", False, "missing"
        if hit["kind"] == "attachment":
            return f"/notes/file/{hit['id']}/raw", True, "attachment"
        return f"/notes/view/{hit['id']}", True, "note"
    return resolve


# ---------------------------------------------------------------- browse
@router.get("/notes", response_class=HTMLResponse)
def notes_home(request: Request, q: str = "", tag: str = "", folder: str = ""):
    results = notes.search(q or None, tag or None, folder or None, limit=100)
    return _pages().TemplateResponse(request, "notes_list.html", {
        "svc": _svc(), "results": results, "q": q, "tag": tag, "folder": folder,
        "folders": notes.folders(), "tags": notes.tags()[:40], "stats": notes.stats()})


@router.get("/notes/view/{file_id}", response_class=HTMLResponse)
def view_note(request: Request, file_id: str, raw: bool = False):
    try:
        f = notes.get(file_id)
    except NotFound:
        return HTMLResponse("Note not found", status_code=404)
    if f["deleted"]:
        return HTMLResponse("This note was deleted.", status_code=410)
    if f["kind"] == "attachment":
        return RedirectResponse(f"/notes/file/{f['id']}/raw")
    out = notes.outgoing(f["id"])
    html = None
    if f["kind"] == "note" and not raw:
        _, body = md.split_frontmatter(f["content"] or "")
        html = md.render_markdown(body, _resolver([l["target"] for l in out]))
    crumbs = f["path"].split("/")[:-1]
    return _pages().TemplateResponse(request, "notes_view.html", {
        "svc": _svc(), "f": f, "html": html, "raw": raw or f["kind"] != "note", "crumbs": crumbs,
        "backlinks": notes.backlinks(f["id"]),
        "outgoing": [l for l in out if l["kind"] != "embed"],
        "history": notes.history(f["id"], 8)})


@router.get("/notes/open")
def open_link(request: Request, target: str):
    """Follow a [[wikilink]] (resolved at click time, like Obsidian)."""
    hit = notes.resolve([target]).get(md.link_key(target))
    if hit:
        return RedirectResponse(f"/notes/view/{hit['id']}" if hit["kind"] != "attachment"
                                else f"/notes/file/{hit['id']}/raw")
    return _pages().TemplateResponse(request, "notes_missing.html", {"svc": _svc(), "target": target},
                                     status_code=404)


SAFE_INLINE = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp",
               "application/pdf", "audio/", "video/", "text/plain")


@router.get("/notes/file/{file_id}/raw")
def raw_file(file_id: str):
    """The file's exact bytes. Anything that could run script (HTML, SVG…) is sent as a download."""
    try:
        f = notes.get(file_id)
    except NotFound:
        return PlainTextResponse("not found", status_code=404)
    name = f["path"].rsplit("/", 1)[-1]
    headers = {"X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; sandbox"}
    if f["kind"] != "attachment":
        return Response(f.get("content") or "", media_type="text/markdown; charset=utf-8",
                        headers={**headers, "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
    mime = notes.guess_mime(f["path"])
    inline = any(mime.startswith(p) for p in SAFE_INLINE)
    return FileResponse(notes.blob_file(f["blob_sha256"]), media_type=mime if inline else "application/octet-stream",
                        headers={**headers, "Content-Disposition":
                                 f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}"})


# ---------------------------------------------------------------- devices
@settings.get("/settings/devices", response_class=HTMLResponse)
def devices(request: Request):
    return _pages().TemplateResponse(request, "devices.html", {"svc": {"key": "", "name": "cove"},
                                                               "tokens": notes.list_tokens(), "new": None})


@settings.post("/settings/devices", response_class=HTMLResponse)
def create_device(request: Request, name: str = Form(...)):
    new = notes.create_token(name, _actor(request))
    return _pages().TemplateResponse(request, "devices.html", {"svc": {"key": "", "name": "cove"},
                                                               "tokens": notes.list_tokens(), "new": new})


@settings.post("/settings/devices/{token_id}/revoke")
def revoke_device(request: Request, token_id: int):
    notes.revoke_token(token_id, _actor(request))
    return RedirectResponse("/settings/devices", status_code=303)
