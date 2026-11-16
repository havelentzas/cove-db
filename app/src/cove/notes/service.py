"""Notes service layer — the ONLY code that writes notes.

The web UI, the MCP server and the sync API (used by the Obsidian plugin) all call these
functions, so versioning, merging, conflict copies, parsing and audit logging are identical
for every writer.

Sync model (see docs/notes-sync.md):
- every file has a stable id (chosen by the client that creates it) and a version;
- writes carry the version the writer started from (``base_version``);
- if the server moved on, text is 3-way merged against that base version; overlapping edits
  produce a "(conflict …)" copy instead of overwriting anything;
- every write takes a value from a global sequence; clients poll the change feed by ``seq``;
- deletes are tombstones, so offline devices learn about them.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import merge3
from psycopg.types.json import Jsonb

from cove import context, db
from cove.crm.service import Invalid, NotFound, _audit, _plain  # shared errors + helpers
from cove.notes import markdown as md

BLOB_DIR = Path(os.environ.get("COVE_BLOB_DIR", "/data/blobs"))
MAX_TEXT = 5 * 1024 * 1024          # 5 MB per note
MAX_BLOB = 200 * 1024 * 1024        # 200 MB per attachment
TEXT_KINDS = ("note", "canvas")


class PathTaken(RuntimeError):
    """Another live file already has this path (case-insensitive)."""

    def __init__(self, path: str, existing: dict):
        super().__init__(f"path {path!r} is already used by another file")
        self.existing = existing


# ================================================================ helpers
def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def normalize_path(path: str) -> str:
    p = md.nfc(str(path or "")).replace("\\", "/").strip()
    p = re.sub(r"/+", "/", p).lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    if not p or any(part in ("", ".", "..") for part in p.split("/")):
        raise Invalid(f"invalid vault path {path!r}")
    if p.startswith(".obsidian/") or p == ".obsidian":
        raise Invalid(".obsidian/ holds app settings and is not synced")
    if len(p) > 1024:
        raise Invalid("path too long")
    return p


def kind_for(path: str) -> str:
    low = path.lower()
    if low.endswith(".md"):
        return "note"
    if low.endswith(".canvas"):
        return "canvas"
    return "attachment"


def _uuid(value: Any) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        raise Invalid(f"{value!r} is not a valid file id (UUID)") from None


META_COLS = ("id, kind, path, content_hash, size, version, seq, title, frontmatter, tags, aliases, "
             "blob_sha256, created_at, updated_at, updated_by, deleted_at")


def _meta(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = {k: row[k] for k in ("id", "kind", "path", "content_hash", "size", "version", "seq", "title",
                               "frontmatter", "tags", "aliases", "blob_sha256", "created_at", "updated_at",
                               "updated_by", "deleted_at") if k in row}
    out["deleted"] = row.get("deleted_at") is not None
    return _plain(out)


def _derive(kind: str, path: str, body: str | None) -> md.Parsed:
    if kind == "note":
        return md.parse_note(path, body or "")
    if kind == "canvas":
        return md.parse_canvas(path, body or "")
    return md.Parsed(title=md.title_for(path))


def _ensure_path_free(c, path: str, file_id: uuid.UUID) -> None:
    other = c.execute(f"SELECT {META_COLS} FROM notes.files WHERE lower(path)=lower(%s) AND deleted_at IS NULL "
                      "AND id <> %s", (path, file_id)).fetchone()
    if other:
        raise PathTaken(path, _meta(other))


def _write_links(c, file_id: uuid.UUID, links: list[dict]) -> None:
    c.execute("DELETE FROM notes.links WHERE from_id=%s", (file_id,))
    if links:
        c.cursor().executemany(
            "INSERT INTO notes.links (from_id, target, target_key, kind, anchor, alias) VALUES (%s,%s,%s,%s,%s,%s)",
            [(file_id, l["target"], l["target_key"], l["kind"], l["anchor"], l["alias"]) for l in links])


def _save(c, *, file_id: uuid.UUID, kind: str, path: str, body: str | None, blob: str | None, actor: str,
          action: str, existing: dict | None, deleted: bool = False) -> dict:
    """Insert or update one file row + version row + links + audit. Caller holds the row lock."""
    content = body if kind in TEXT_KINDS else None
    chash = sha256(content) if content is not None else (blob or (existing or {}).get("content_hash", ""))
    size = len(content.encode("utf-8")) if content is not None else (
        c.execute("SELECT size FROM notes.blobs WHERE sha256=%s", (blob,)).fetchone()["size"] if blob else 0)
    parsed = _derive(kind, path, content)
    params = dict(id=file_id, kind=kind, path=path, body=content, blob=blob, hash=chash, size=size,
                  title=parsed.title, fm=Jsonb(parsed.frontmatter), tags=parsed.tags, aliases=parsed.aliases,
                  actor=actor, deleted=deleted)
    if existing is None:
        row = c.execute(
            f"""INSERT INTO notes.files (id, kind, path, body, blob_sha256, content_hash, size, title, frontmatter,
                                         tags, aliases, updated_by)
                VALUES (%(id)s, %(kind)s, %(path)s, %(body)s, %(blob)s, %(hash)s, %(size)s, %(title)s, %(fm)s,
                        %(tags)s, %(aliases)s, %(actor)s) RETURNING {META_COLS}""", params).fetchone()
    else:
        row = c.execute(
            f"""UPDATE notes.files SET kind=%(kind)s, path=%(path)s, body=%(body)s, blob_sha256=%(blob)s,
                       content_hash=%(hash)s, size=%(size)s, title=%(title)s, frontmatter=%(fm)s, tags=%(tags)s,
                       aliases=%(aliases)s, updated_by=%(actor)s, version=version+1,
                       deleted_at = CASE WHEN %(deleted)s THEN coalesce(deleted_at, now()) END
                 WHERE id=%(id)s RETURNING {META_COLS}""", params).fetchone()
    c.execute("INSERT INTO notes.versions (file_id, version, path, body, blob_sha256, content_hash, deleted, actor) "
              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
              (file_id, row["version"], path, content, blob, chash, deleted, actor))
    _write_links(c, file_id, [] if deleted else parsed.links)
    diff = {"path": path, "version": row["version"]}
    if existing and existing["path"] != path:
        diff["from_path"] = existing["path"]
    _audit(c, actor, "notes.file", file_id, action, diff)
    return _meta(row)


def _lock(c, file_id: uuid.UUID) -> dict | None:
    return c.execute(f"SELECT {META_COLS}, body FROM notes.files WHERE id=%s FOR UPDATE", (file_id,)).fetchone()


def _base(c, file_id: uuid.UUID, version: int | None) -> dict | None:
    if not version:
        return None
    return c.execute("SELECT * FROM notes.versions WHERE file_id=%s AND version=%s", (file_id, version)).fetchone()


def _merge(base: str, server: str, client: str) -> str | None:
    """3-way merge; None on overlapping edits."""
    m = merge3.Merge3(base.splitlines(True), client.splitlines(True), server.splitlines(True))
    if any(r[0] == "conflict" for r in m.merge_regions()):
        return None
    return "".join(m.merge_lines())


def _conflict_path(c, path: str, actor: str) -> str:
    stem, dot, ext = path.rpartition(".")
    if not dot or "/" in ext:
        stem, ext = path, ""
    who = re.sub(r"[^\w-]+", "-", actor.split(":", 1)[-1]).strip("-")[:24] or "unknown"
    base = f"{stem} (conflict {datetime.now(timezone.utc):%Y-%m-%d} {who})"
    for n in range(1, 100):
        cand = f"{base}{'' if n == 1 else f' {n}'}{'.' + ext if ext else ''}"
        if not c.execute("SELECT 1 FROM notes.files WHERE lower(path)=lower(%s) AND deleted_at IS NULL",
                         (cand,)).fetchone():
            return cand
    raise Invalid("too many conflict copies")


# ================================================================ writes
def put_text(file_id: Any, path: str, content: str, base_version: int | None, actor: str) -> dict:
    """Create or update a note/canvas. Returns {"status", "file", "content"?, "conflict_copy"?}.

    status: created | updated | unchanged | merged | conflict | restored
    - merged: your edit and a newer server edit were combined; "content" is the merged text.
    - conflict: overlapping edits; the server copy is kept and yours is saved as "conflict_copy".
    """
    fid, path = _uuid(file_id), normalize_path(path)
    kind = kind_for(path)
    if kind not in TEXT_KINDS:
        raise Invalid(f"{path!r} is not a note or canvas; upload it as an attachment")
    if not isinstance(content, str):
        raise Invalid("content must be text")
    if len(content.encode("utf-8")) > MAX_TEXT:
        raise Invalid("note is larger than 5 MB")
    with db.conn() as c:
        row = _lock(c, fid)
        if row is None:
            _ensure_path_free(c, path, fid)
            f = _save(c, file_id=fid, kind=kind, path=path, body=content, blob=None, actor=actor,
                      action="create", existing=None)
            return {"status": "created", "file": f}

        if row["deleted_at"] is not None:   # edit beats delete: bring it back
            _ensure_path_free(c, path, fid)
            f = _save(c, file_id=fid, kind=kind, path=path, body=content, blob=None, actor=actor,
                      action="restore", existing=row)
            return {"status": "restored", "file": f}

        same_content = row["content_hash"] == sha256(content)
        if same_content and row["path"] == path:
            return {"status": "unchanged", "file": _meta(row)}

        if base_version == row["version"] or same_content:
            if path != row["path"]:
                _ensure_path_free(c, path, fid)
            f = _save(c, file_id=fid, kind=kind, path=path, body=content, blob=None, actor=actor,
                      action="update", existing=row)
            return {"status": "updated", "file": f}

        # The server moved on since the writer's base version.
        base = _base(c, fid, base_version)
        new_path = path if (base and base["path"] == row["path"]) else row["path"]   # keep server-side renames
        merged = _merge(base["body"] or "", row["body"] or "", content) if base and kind == "note" else None
        if merged is not None:
            if new_path != row["path"]:
                _ensure_path_free(c, new_path, fid)
            f = _save(c, file_id=fid, kind=kind, path=new_path, body=merged, blob=None, actor=actor,
                      action="merge", existing=row)
            return {"status": "merged", "file": f, "content": merged}

        copy_path = _conflict_path(c, path, actor)
        copy = _save(c, file_id=uuid.uuid4(), kind=kind, path=copy_path, body=content, blob=None, actor=actor,
                     action="conflict_copy", existing=None)
        return {"status": "conflict", "file": _meta(row), "conflict_copy": copy}


def put_attachment(file_id: Any, path: str, blob_sha256: str, base_version: int | None, actor: str) -> dict:
    fid, path = _uuid(file_id), normalize_path(path)
    if kind_for(path) != "attachment":
        raise Invalid(f"{path!r} is a note or canvas; send it as text")
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM notes.blobs WHERE sha256=%s", (blob_sha256,)).fetchone():
            raise Invalid(f"blob {blob_sha256} has not been uploaded; PUT /api/notes/blobs/{{sha256}} first")
        row = _lock(c, fid)
        if row is None:
            _ensure_path_free(c, path, fid)
            return {"status": "created", "file": _save(c, file_id=fid, kind="attachment", path=path, body=None,
                                                         blob=blob_sha256, actor=actor, action="create", existing=None)}
        if row["deleted_at"] is not None:
            _ensure_path_free(c, path, fid)
            return {"status": "restored", "file": _save(c, file_id=fid, kind="attachment", path=path, body=None,
                                                          blob=blob_sha256, actor=actor, action="restore", existing=row)}
        if row["blob_sha256"] == blob_sha256 and row["path"] == path:
            return {"status": "unchanged", "file": _meta(row)}
        if base_version == row["version"] or row["blob_sha256"] == blob_sha256:
            if path != row["path"]:
                _ensure_path_free(c, path, fid)
            return {"status": "updated", "file": _save(c, file_id=fid, kind="attachment", path=path, body=None,
                                                         blob=blob_sha256, actor=actor, action="update", existing=row)}
        copy = _save(c, file_id=uuid.uuid4(), kind="attachment", path=_conflict_path(c, path, actor), body=None,
                     blob=blob_sha256, actor=actor, action="conflict_copy", existing=None)
        return {"status": "conflict", "file": _meta(row), "conflict_copy": copy}


def rename(file_id: Any, new_path: str, base_version: int | None, actor: str) -> dict:
    fid, new_path = _uuid(file_id), normalize_path(new_path)
    with db.conn() as c:
        row = _lock(c, fid)
        if row is None or row["deleted_at"] is not None:
            raise NotFound(f"no live file {fid}")
        if row["path"] == new_path:
            return {"status": "unchanged", "file": _meta(row)}
        if kind_for(new_path) != row["kind"]:
            raise Invalid("renaming can't change a file's type (extension)")
        base = _base(c, fid, base_version)
        if base_version != row["version"] and base and base["path"] != row["path"]:
            return {"status": "conflict", "file": _meta(row)}   # renamed elsewhere too: server path wins
        _ensure_path_free(c, new_path, fid)
        f = _save(c, file_id=fid, kind=row["kind"], path=new_path, body=row["body"], blob=row["blob_sha256"],
                  actor=actor, action="rename", existing=row)
        return {"status": "updated", "file": f}


def delete(file_id: Any, base_version: int | None, actor: str) -> dict:
    fid = _uuid(file_id)
    with db.conn() as c:
        row = _lock(c, fid)
        if row is None:
            raise NotFound(f"no file {fid}")
        if row["deleted_at"] is not None:
            return {"status": "unchanged", "file": _meta(row)}
        if base_version != row["version"]:
            base = _base(c, fid, base_version)
            if not base or base["content_hash"] != row["content_hash"]:
                # someone edited it since you last saw it: keep it (edit beats delete)
                return {"status": "conflict", "file": _meta(row)}
        f = _save(c, file_id=fid, kind=row["kind"], path=row["path"], body=row["body"], blob=row["blob_sha256"],
                  actor=actor, action="delete", existing=row, deleted=True)
        return {"status": "deleted", "file": f}


# ================================================================ blobs
def _blob_root() -> Path:
    """Each workspace has its own blob folder (BLOB_DIR/<workspace short id>), so one workspace can't
    read or probe another's attachments. Tests (no workspace) use BLOB_DIR directly."""
    p = context.current()
    if p is None:
        if db.test_mode():
            return BLOB_DIR
        raise context.NoWorkspace("no workspace selected for this request")
    return BLOB_DIR / p.workspace.id.replace("-", "")[:12]


def _blob_path(sha: str) -> Path:
    return _blob_root() / sha[:2] / sha[2:4] / sha


def has_blob(sha: str) -> bool:
    with db.conn() as c:
        return c.execute("SELECT 1 FROM notes.blobs WHERE sha256=%s", (sha,)).fetchone() is not None


def store_blob(sha: str, data: bytes, mime: str | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise Invalid("blob id must be a lowercase sha256 hex digest")
    if len(data) > MAX_BLOB:
        raise Invalid("attachment is larger than 200 MB")
    if sha256(data) != sha:
        raise Invalid("uploaded bytes do not match the sha256 in the URL")
    target = _blob_path(sha)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    with db.conn() as c:
        c.execute("INSERT INTO notes.blobs (sha256, size, mime) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                  (sha, len(data), mime))
    return {"sha256": sha, "size": len(data)}


def blob_file(sha: str) -> Path:
    p = _blob_path(sha)
    if not p.exists():
        raise NotFound(f"blob {sha} not found")
    return p


# ================================================================ reads
def get(file_id: Any, *, with_content: bool = True) -> dict:
    fid = _uuid(file_id)
    with db.conn() as c:
        row = c.execute(f"SELECT {META_COLS}, body FROM notes.files WHERE id=%s", (fid,)).fetchone()
    if not row:
        raise NotFound(f"no file {fid}")
    out = _meta(row)
    if with_content and row["kind"] in TEXT_KINDS:
        out["content"] = row["body"]
    return out


def get_by_path(path: str, *, with_content: bool = True) -> dict:
    p = normalize_path(path)
    with db.conn() as c:
        row = c.execute("SELECT id FROM notes.files WHERE lower(path)=lower(%s) AND deleted_at IS NULL", (p,)).fetchone()
    if not row:
        raise NotFound(f"no note at {p!r}")
    return get(row["id"], with_content=with_content)


def find(ref: str, *, with_content: bool = True) -> dict:
    """Look a file up by id, exact path, or link-style name ("Meeting notes")."""
    try:
        return get(uuid.UUID(str(ref)), with_content=with_content)
    except (ValueError, NotFound):
        pass
    try:
        return get_by_path(ref, with_content=with_content)
    except (Invalid, NotFound):
        pass
    hit = resolve([ref]).get(md.link_key(ref))
    if not hit:
        raise NotFound(f"no note matching {ref!r}")
    return get(hit["id"], with_content=with_content)


def changes(since: int = 0, limit: int = 500) -> dict:
    limit = max(1, min(int(limit), 2000))
    with db.conn() as c:
        rows = c.execute(f"SELECT {META_COLS} FROM notes.files WHERE seq > %s ORDER BY seq LIMIT %s",
                         (int(since), limit + 1)).fetchall()
        head = c.execute("SELECT coalesce(max(seq), 0) AS s FROM notes.files").fetchone()["s"]
    more = len(rows) > limit
    rows = rows[:limit]
    return {"changes": [_meta(r) for r in rows], "last_seq": rows[-1]["seq"] if rows else int(since),
            "head_seq": head, "more": more}


def manifest() -> dict:
    with db.conn() as c:
        rows = c.execute("SELECT id, kind, path, content_hash, version FROM notes.files WHERE deleted_at IS NULL "
                         "ORDER BY lower(path)").fetchall()
        head = c.execute("SELECT coalesce(max(seq), 0) AS s FROM notes.files").fetchone()["s"]
    return {"files": _plain(rows), "head_seq": head}


def resolve(targets: Iterable[str]) -> dict[str, dict]:
    """Resolve link targets the way Obsidian does: exact path first, then file name (shortest path wins)."""
    keys = sorted({md.link_key(t) for t in targets if t})
    if not keys:
        return {}
    with db.conn() as c:
        rows = c.execute(
            """SELECT id, kind, path,
                      lower(regexp_replace(path, '\\.md$', '', 'i')) AS pkey,
                      lower(regexp_replace(regexp_replace(path, '^.*/', ''), '\\.md$', '', 'i')) AS nkey
                 FROM notes.files
                WHERE deleted_at IS NULL
                  AND (lower(regexp_replace(path, '\\.md$', '', 'i')) = ANY(%(k)s)
                       OR lower(regexp_replace(regexp_replace(path, '^.*/', ''), '\\.md$', '', 'i')) = ANY(%(k)s))
                ORDER BY length(path)""", {"k": keys}).fetchall()
    out: dict[str, dict] = {}
    for r in rows:   # exact path matches take priority over name matches
        if r["pkey"] in keys:
            out.setdefault(r["pkey"], _plain(r))
    for r in rows:
        if r["nkey"] in keys:
            out.setdefault(r["nkey"], _plain(r))
    return out


def backlinks(file_id: Any) -> list[dict]:
    f = get(file_id, with_content=False)
    full, name = md.path_keys(f["path"])
    with db.conn() as c:
        rows = c.execute(
            """SELECT DISTINCT ON (s.id) s.id, s.path, s.title, l.kind
                 FROM notes.links l JOIN notes.files s ON s.id = l.from_id
                WHERE s.deleted_at IS NULL AND s.id <> %s AND l.target_key IN (%s, %s)
                ORDER BY s.id""", (f["id"], full, name)).fetchall()
    return sorted(_plain(rows), key=lambda r: r["path"].lower())


def outgoing(file_id: Any) -> list[dict]:
    with db.conn() as c:
        links = c.execute("SELECT target, kind, anchor, alias FROM notes.links WHERE from_id=%s",
                          (_uuid(file_id),)).fetchall()
    res = resolve([l["target"] for l in links])
    return [{**l, "resolved": res.get(md.link_key(l["target"]))} for l in links]


def search(query: str | None = None, tag: str | None = None, folder: str | None = None,
           limit: int = 50, include_attachments: bool = False) -> list[dict]:
    where, params = ["deleted_at IS NULL"], []
    if not include_attachments:
        where.append("kind <> 'attachment'")
    if folder:
        where.append("path ILIKE %s")
        params.append(folder.strip("/").replace("%", r"\%") + "/%")
    if tag:
        where.append("EXISTS (SELECT 1 FROM unnest(tags) t WHERE lower(t) = lower(%s) OR lower(t) LIKE lower(%s))")
        params += [tag.lstrip("#"), tag.lstrip("#") + "/%"]
    rank, order = "0", "updated_at DESC"
    if query and query.strip():
        q = query.strip()
        where.append("(search @@ websearch_to_tsquery('simple', %s) OR path ILIKE %s OR %s = ANY(aliases))")
        params += [q, f"%{q}%", q]
        rank = "ts_rank(search, websearch_to_tsquery('simple', %s)) + CASE WHEN path ILIKE %s THEN 1 ELSE 0 END"
        order = "rank DESC, updated_at DESC"
    sql = f"""SELECT id, kind, path, title, tags, updated_at, updated_by, size, version,
                     left(regexp_replace(coalesce(body, ''), '^---.*?---\\s*', ''), 240) AS excerpt,
                     {rank} AS rank
                FROM notes.files WHERE {' AND '.join(where)} ORDER BY {order} LIMIT %s"""
    if query and query.strip():
        params = [query.strip(), f"%{query.strip()}%"] + params
    params.append(max(1, min(int(limit), 500)))
    with db.conn() as c:
        rows = c.execute(sql, params).fetchall()
    return _plain([{k: v for k, v in r.items() if k != "rank"} for r in rows])


def folders() -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            """SELECT CASE WHEN position('/' in path) > 0 THEN split_part(path, '/', 1) ELSE '' END AS folder,
                      count(*) AS files
                 FROM notes.files WHERE deleted_at IS NULL AND kind <> 'attachment'
                GROUP BY 1 ORDER BY 1""").fetchall()
    return _plain(rows)


def tags() -> list[dict]:
    with db.conn() as c:
        rows = c.execute("""SELECT t AS tag, count(*) AS notes FROM notes.files, unnest(tags) t
                             WHERE deleted_at IS NULL GROUP BY t ORDER BY count(*) DESC, lower(t) LIMIT 200""").fetchall()
    return _plain(rows)


def history(file_id: Any, limit: int = 20) -> list[dict]:
    with db.conn() as c:
        rows = c.execute("SELECT version, path, content_hash, deleted, actor, created_at FROM notes.versions "
                         "WHERE file_id=%s ORDER BY version DESC LIMIT %s", (_uuid(file_id), limit)).fetchall()
    return _plain(rows)


def stats() -> dict:
    with db.conn() as c:
        return _plain(c.execute(
            """SELECT count(*) FILTER (WHERE kind='note') AS notes,
                      count(*) FILTER (WHERE kind='attachment') AS attachments,
                      count(*) FILTER (WHERE kind='canvas') AS canvases,
                      (SELECT count(*) FROM meta.api_tokens WHERE revoked_at IS NULL AND 'notes' = ANY(scopes)) AS devices
                 FROM notes.files WHERE deleted_at IS NULL""").fetchone())


# ================================================================ conveniences for agents
def create_note(path: str, content: str, actor: str) -> dict:
    p = normalize_path(path if path.lower().endswith((".md", ".canvas")) else path + ".md")
    res = put_text(uuid.uuid4(), p, content, None, actor)
    return res


def append(ref: str, text: str, actor: str, heading: str | None = None, create: bool = True) -> dict:
    """Append text to a note (optionally at the end of a '## heading' section), creating it if needed."""
    for _ in range(3):   # retry if someone else writes in between
        try:
            f = find(ref)
        except NotFound:
            if not create:
                raise
            body = (f"## {heading}\n\n" if heading else "") + text.rstrip() + "\n"
            return create_note(ref, body, actor)
        body = f.get("content") or ""
        addition = text.rstrip() + "\n"
        if heading:
            m = re.search(rf"^(#+)\s+{re.escape(heading)}\s*$", body, re.M)
            if m:
                level = len(m.group(1))
                nxt = re.search(rf"^#{{1,{level}}}\s", body[m.end():], re.M)
                at = m.end() + (nxt.start() if nxt else len(body) - m.end())
                before = body[:at].rstrip("\n") + "\n"
                after = body[at:]
                body = before + addition + ("\n" + after if after else "")
            else:
                body = body.rstrip("\n") + f"\n\n## {heading}\n\n" + addition
        else:
            body = (body.rstrip("\n") + "\n\n" if body.strip() else "") + addition
        res = put_text(f["id"], f["path"], body, f["version"], actor)
        if res["status"] != "conflict":
            return res
    raise Invalid("note kept changing; try again")


# ================================================================ device tokens (see cove/auth/tokens.py)
from cove.auth import tokens as _tokens  # noqa: E402

TOKEN_PREFIX = _tokens.ANY_PREFIX
create_token = _tokens.create
list_tokens = _tokens.list_all
revoke_token = _tokens.revoke
verify_token = _tokens.verify


def guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


# ================================================================ graph & folder tree
_cache: dict[tuple[str, str], tuple[tuple, Any]] = {}   # (workspace, name) -> (feed head, value)


def _head_seq(c) -> tuple:
    """Cache key that changes on any write (seq alone repeats if the schema is ever recreated)."""
    r = c.execute("SELECT coalesce(max(seq), 0) AS s, count(*) AS n, max(updated_at) AS u FROM notes.files").fetchone()
    return (r["s"], r["n"], r["u"])


def _cached(name: str, build):
    """Recompute only when the vault changed (any write bumps the change-feed head)."""
    with db.conn() as c:
        head = _head_seq(c)
    p = context.current()
    key = (p.workspace.id if p else "", name)     # never share between workspaces
    hit = _cache.get(key)
    if hit and hit[0] == head:
        return hit[1]
    value = build()
    _cache[key] = (head, value)
    return value


def _graph_full() -> dict:
    with db.conn() as c:
        files = c.execute("SELECT id, kind, path, title, tags FROM notes.files WHERE deleted_at IS NULL").fetchall()
        links = c.execute("""SELECT l.from_id, l.target, l.target_key, l.kind FROM notes.links l
                               JOIN notes.files f ON f.id = l.from_id AND f.deleted_at IS NULL""").fetchall()
    by_path: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for f in sorted(files, key=lambda f: len(f["path"])):          # shortest path wins name matches
        full, name = md.path_keys(f["path"])
        by_path.setdefault(full, f)
        by_name.setdefault(name, f)
    nodes = {str(f["id"]): {"id": str(f["id"]), "kind": f["kind"], "title": f["title"], "path": f["path"],
                            "folder": f["path"].rsplit("/", 1)[0] if "/" in f["path"] else "",
                            "tags": f["tags"] or []} for f in files}
    edges, unresolved = set(), {}
    for l in links:
        src = str(l["from_id"])
        hit = by_path.get(l["target_key"]) or by_name.get(l["target_key"])
        if hit:
            dst = str(hit["id"])
            if dst != src:
                edges.add((src, dst, "embed" if l["kind"] == "embed" else "link"))
        else:
            key = "missing:" + l["target_key"]
            unresolved.setdefault(key, {"id": key, "kind": "unresolved", "title": l["target"], "path": None,
                                        "folder": "", "tags": []})
            edges.add((src, key, "unresolved"))
    return {"nodes": nodes, "edges": edges, "unresolved": unresolved}


def graph(focus: str | None = None, depth: int = 1, attachments: bool = False, tags: bool = False,
          unresolved: bool = False, orphans: bool = True) -> dict:
    """Obsidian-style graph: notes are nodes, resolved [[links]]/embeds are edges.
    `focus` + `depth` gives a local graph (neighbourhood, links followed in both directions)."""
    full = _cached("graph", _graph_full)
    nodes = dict(full["nodes"])
    edges = list(full["edges"])
    if unresolved:
        nodes.update(full["unresolved"])
    if tags:
        for n in list(nodes.values()):
            for t in n["tags"]:
                tid = "tag:" + t.lower()
                nodes.setdefault(tid, {"id": tid, "kind": "tag", "title": "#" + t, "path": None, "folder": "", "tags": []})
                edges.append((n["id"], tid, "tag"))
    keep = {k for k, n in nodes.items() if n["kind"] != "attachment" or attachments}
    edges = [e for e in edges if e[0] in keep and e[1] in keep]
    if focus:
        focus = str(focus)
        adj: dict[str, set[str]] = {}
        for a, b, _ in edges:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        seen, frontier = {focus}, {focus}
        for _ in range(max(1, min(int(depth), 4))):
            frontier = {m for n in frontier for m in adj.get(n, ())} - seen
            seen |= frontier
        keep &= seen
        edges = [e for e in edges if e[0] in keep and e[1] in keep]
    degree: dict[str, int] = {}
    for a, b, _ in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    if not orphans and not focus:
        keep = {k for k in keep if degree.get(k)}
    out_nodes = [{**{k: v for k, v in nodes[k].items() if k != "tags"}, "degree": degree.get(k, 0)} for k in keep]
    return {"nodes": out_nodes, "links": [{"source": a, "target": b, "kind": k} for a, b, k in edges
                                          if a in keep and b in keep], "focus": focus}


def _tree_full() -> dict:
    with db.conn() as c:
        rows = c.execute("SELECT id, kind, path, title FROM notes.files WHERE deleted_at IS NULL").fetchall()
    root = {"name": "", "path": "", "folders": {}, "files": [], "count": 0}
    for r in rows:
        parts = r["path"].split("/")
        node = root
        node["count"] += 1
        for i, part in enumerate(parts[:-1]):
            node = node["folders"].setdefault(part, {"name": part, "path": "/".join(parts[:i + 1]),
                                                      "folders": {}, "files": [], "count": 0})
            node["count"] += 1
        node["files"].append({"id": str(r["id"]), "kind": r["kind"], "name": parts[-1], "title": r["title"],
                              "path": r["path"]})

    def finish(n: dict) -> dict:
        return {**n, "folders": [finish(f) for f in sorted(n["folders"].values(), key=lambda f: f["name"].lower())],
                "files": sorted(n["files"], key=lambda f: f["name"].lower())}
    return finish(root)


def tree() -> dict:
    """The vault's folder structure (all live files), sorted like Obsidian's file explorer."""
    return _cached("tree", _tree_full)


def folder(path: str) -> dict | None:
    """One folder of the tree: its direct subfolders and files."""
    node = tree()
    for part in [p for p in (path or "").strip("/").split("/") if p]:
        node = next((f for f in node["folders"] if f["name"] == part), None)
        if node is None:
            return None
    return node
