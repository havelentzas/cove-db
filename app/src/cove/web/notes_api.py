"""Notes sync API (/api/notes/*) — used by the cove-sync Obsidian plugin and any other client.

Every request needs `Authorization: Bearer cove_…` (a device token from /settings/devices).
The protocol is documented in docs/notes-sync.md.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from cove.crm.service import Invalid, NotFound
from cove.notes import service as notes

API_VERSION = 1
router = APIRouter(prefix="/api/notes", tags=["notes"])


def device(authorization: Annotated[str | None, Header()] = None) -> dict:
    token = (authorization or "").removeprefix("Bearer ").strip()
    dev = notes.verify_token(token)
    if not dev:
        raise HTTPException(401, "missing or invalid device token", headers={"WWW-Authenticate": "Bearer"})
    return {**dev, "actor": f"device:{dev['name']}"}


Device = Annotated[dict, Depends(device)]


class PutFile(BaseModel):
    path: str
    content: str | None = Field(None, description="Full text for .md / .canvas files")
    blob_sha256: str | None = Field(None, description="Content hash of an uploaded blob, for attachments")
    base_version: int | None = Field(None, description="Version this edit started from; omit when creating")


class RenameFile(BaseModel):
    path: str
    base_version: int | None = None


def _result(res: dict) -> JSONResponse:
    code = {"created": 201, "conflict": 409}.get(res["status"], 200)
    return JSONResponse(res, status_code=code)


def _errors(fn):
    try:
        return fn()
    except notes.PathTaken as e:
        return JSONResponse({"status": "path_taken", "detail": str(e), "existing": e.existing}, status_code=409)
    except NotFound as e:
        return JSONResponse({"status": "not_found", "detail": str(e)}, status_code=404)
    except Invalid as e:
        return JSONResponse({"status": "invalid", "detail": str(e)}, status_code=422)


@router.get("/whoami")
def whoami(dev: Device) -> dict:
    """Check the token and get server info (call this first)."""
    return {"server": "cove", "api_version": API_VERSION, "device": dev["name"],
            "head_seq": notes.changes(10 ** 18)["head_seq"]}


@router.get("/manifest")
def manifest(dev: Device) -> dict:
    """Every live file (id, path, kind, content_hash, version) — for first-sync matching."""
    return notes.manifest()


@router.get("/changes")
def changes(dev: Device, since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000)) -> dict:
    """Files changed after `since` (a seq), oldest first, including tombstones. Page with `more`."""
    return notes.changes(since, limit)


@router.get("/files/{file_id}")
def get_file(file_id: str, dev: Device):
    """One file's metadata, plus `content` for notes and canvases."""
    return _errors(lambda: notes.get(file_id))


@router.put("/files/{file_id}")
def put_file(file_id: str, body: PutFile, dev: Device):
    """Create or update a file. The client chooses the id (a UUID) when creating, so retries are safe."""
    def run():
        if body.content is not None:
            return _result(notes.put_text(file_id, body.path, body.content, body.base_version, dev["actor"]))
        if body.blob_sha256:
            return _result(notes.put_attachment(file_id, body.path, body.blob_sha256, body.base_version, dev["actor"]))
        raise Invalid("send `content` (notes/canvases) or `blob_sha256` (attachments)")
    return _errors(run)


@router.post("/files/{file_id}/rename")
def rename_file(file_id: str, body: RenameFile, dev: Device):
    """Move/rename a file; its id, history and backlinks are kept."""
    return _errors(lambda: _result(notes.rename(file_id, body.path, body.base_version, dev["actor"])))


@router.delete("/files/{file_id}")
def delete_file(file_id: str, dev: Device, base_version: int | None = None):
    """Delete (tombstone). Refused with 409 if the file was edited since base_version."""
    return _errors(lambda: _result(notes.delete(file_id, base_version, dev["actor"])))


@router.get("/files/{file_id}/history")
def file_history(file_id: str, dev: Device, limit: int = 20):
    return _errors(lambda: notes.history(file_id, limit))


@router.head("/blobs/{sha256}")
def blob_exists(sha256: str, dev: Device):
    """200 if the server already has this attachment content (skip the upload), else 404."""
    return Response(status_code=200 if notes.has_blob(sha256) else 404)


@router.put("/blobs/{sha256}")
async def put_blob(sha256: str, request: Request, dev: Device):
    """Upload attachment bytes (raw request body). The URL is the sha256 of the bytes."""
    length = int(request.headers.get("content-length") or 0)
    if length > notes.MAX_BLOB:
        return JSONResponse({"status": "invalid", "detail": "attachment is larger than 200 MB"}, status_code=413)
    data = await request.body()
    return _errors(lambda: notes.store_blob(sha256, data, request.headers.get("content-type")))


@router.get("/blobs/{sha256}")
def get_blob(sha256: str, dev: Device):
    return _errors(lambda: FileResponse(notes.blob_file(sha256), media_type="application/octet-stream"))


@router.get("/search")
def search(dev: Device, q: str | None = None, tag: str | None = None, folder: str | None = None, limit: int = 50):
    return notes.search(q, tag, folder, limit)
