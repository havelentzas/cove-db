"""Notes pages (/notes) and the device-token page (/settings/devices)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from cove import brand
from cove.crm.service import NotFound
from cove.notes import markdown as md
from cove.notes import service as notes
from cove.web.api_v1 import service_on

router = APIRouter(tags=["notes"], dependencies=[service_on("notes")])
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


def _ctx(here: str = "", **kw) -> dict:
    """Common context for every Notes page: the service, the vault's folder tree, and where we are in it."""
    return {"svc": _svc(), "tree": notes.tree(), "here": here, **kw}


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
    folder = folder.strip("/")
    listing = notes.folder(folder) if folder and not q and not tag else None   # a folder view, like Obsidian
    if folder and not q and not tag and listing is None:
        return HTMLResponse("Folder not found", status_code=404)
    results = [] if listing else notes.search(q or None, tag or None, folder or None, limit=100)
    return _pages().TemplateResponse(request, "notes_list.html", _ctx(
        here=folder, results=results, listing=listing, q=q, tag=tag, folder=folder,
        tags=notes.tags()[:40], stats=notes.stats()))


def _node_url(n: dict) -> str | None:
    if n["kind"] in ("note", "canvas"):
        return f"/notes/view/{n['id']}"
    if n["kind"] == "attachment":
        return f"/notes/file/{n['id']}/raw"
    if n["kind"] == "tag":
        return f"/notes?tag={quote(n['title'].lstrip('#'))}"
    return f"/notes/open?target={quote(n['title'])}"


@router.get("/notes/graph.json")
def graph_json(focus: str | None = None, depth: int = 1, attachments: bool = False, tags: bool = False,
               unresolved: bool = False, orphans: bool = True):
    """Graph data (nodes + links) for the whole vault, or a local graph around `focus`."""
    g = notes.graph(focus, depth, attachments, tags, unresolved, orphans)
    for n in g["nodes"]:
        n["url"] = _node_url(n)
    return JSONResponse(g)


@router.get("/notes/graph", response_class=HTMLResponse)
def graph_page(request: Request, focus: str | None = None, depth: int = 2):
    title = None
    if focus:
        try:
            title = notes.get(focus, with_content=False)["title"]
        except NotFound:
            focus = None
    data = notes.graph(focus, depth, orphans=True)
    for n in data["nodes"]:
        n["url"] = _node_url(n)
    return _pages().TemplateResponse(request, "notes_graph.html", _ctx(focus=focus, depth=depth, focus_title=title,
                                                                      graph=data))


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
    local = notes.graph(f["id"], 1, unresolved=True)
    for n in local["nodes"]:
        n["url"] = _node_url(n)
    return _pages().TemplateResponse(request, "notes_view.html", _ctx(
        here=f["path"], f=f, html=html, raw=raw or f["kind"] != "note", crumbs=crumbs,
        backlinks=notes.backlinks(f["id"]), outgoing=[l for l in out if l["kind"] != "embed"],
        history=notes.history(f["id"], 8), local_graph=local))


@router.get("/notes/open")
def open_link(request: Request, target: str):
    """Follow a [[wikilink]] (resolved at click time, like Obsidian)."""
    hit = notes.resolve([target]).get(md.link_key(target))
    if hit:
        return RedirectResponse(f"/notes/view/{hit['id']}" if hit["kind"] != "attachment"
                                else f"/notes/file/{hit['id']}/raw")
    return _pages().TemplateResponse(request, "notes_missing.html", _ctx(target=target), status_code=404)


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


# ---------------------------------------------------------------- plugin download
PLUGIN_DIR = __import__("pathlib").Path("/srv/obsidian-plugin")
PLUGIN_FILES = {"main.js": "text/javascript", "manifest.json": "application/json", "styles.css": "text/css"}


@settings.get("/plugin/cove-sync/{name}")
def plugin_file(name: str):
    """The cove sync Obsidian plugin files (built from obsidian-plugin/ in the repo)."""
    if name not in PLUGIN_FILES or not (PLUGIN_DIR / name).exists():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(PLUGIN_DIR / name, media_type=PLUGIN_FILES[name],
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


def plugin_version() -> str | None:
    try:
        import json
        return json.loads((PLUGIN_DIR / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------- devices
@settings.get("/settings/devices", response_class=HTMLResponse)
def devices(request: Request):
    return _pages().TemplateResponse(request, "devices.html", {**_ctx(), "svc": {"key": "", "name": "cove"},
                                                               "tokens": notes.list_tokens("device"), "new": None,
                                                               "plugin_version": plugin_version()})


@settings.post("/settings/devices", response_class=HTMLResponse)
def create_device(request: Request, name: str = Form(...)):
    new = notes.create_token(name, _actor(request))
    return _pages().TemplateResponse(request, "devices.html", {**_ctx(), "svc": {"key": "", "name": "cove"},
                                                               "tokens": notes.list_tokens("device"), "new": new,
                                                               "plugin_version": plugin_version()})


@settings.post("/settings/devices/{token_id}/revoke")
def revoke_device(request: Request, token_id: int):
    notes.revoke_token(token_id, _actor(request))
    return RedirectResponse("/settings/devices", status_code=303)
