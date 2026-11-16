import uuid

import pytest

from cove.notes import markdown as md
from cove.notes import service as n

A = "device:laptop"
B = "device:phone"


def nid():
    return str(uuid.uuid4())


# ------------------------------------------------------------------ markdown (no DB)
NOTE = """---
tags: [project, "area/home"]
aliases: Kitchen plan
status: active
---
# Kitchen

Talk to [[People/Meg Okafor|Meg]] about [[Budget#Q1]] and ![[sketch.png]].
See [the old list](Old%20List.md) and https://example.com.
Inline #idea and #area/garden but not `#code` or # Heading.

```
#notatag [[NotALink]]
```
"""


def test_parse_note():
    p = md.parse_note("Projects/Kitchen.md", NOTE)
    assert p.title == "Kitchen"
    assert p.frontmatter["status"] == "active"
    assert p.aliases == ["Kitchen", "plan"] or p.aliases == ["Kitchen plan"]
    assert set(p.tags) == {"project", "area/home", "idea", "area/garden"}
    got = {(l["target"], l["kind"], l["anchor"], l["alias"]) for l in p.links}
    assert ("People/Meg Okafor", "link", None, "Meg") in got
    assert ("Budget", "link", "Q1", None) in got
    assert ("sketch.png", "embed", None, None) in got
    assert ("Old List.md", "markdown", None, None) in got
    assert not any(l["target"] == "NotALink" for l in p.links)


def test_render_markdown_obsidian_features_and_sanitizes():
    def resolve(t):
        if t == "Budget":
            return "/notes/view/b", True, "note"
        if t == "sketch.png":
            return "/notes/file/s/raw", True, "attachment"
        return "/notes/open?target=x", False, "missing"
    body = md.split_frontmatter(NOTE)[1] + """
> [!warning] Careful
> Hot surfaces

- [x] done
- [ ] todo

This is ==important==.<script>alert(1)</script><img src=x onerror=alert(1)>

| a | b |
|---|---|
| 1 | 2 |
"""
    html = md.render_markdown(body, resolve)
    assert '<a class="internal-link" href="/notes/view/b#Q1" rel="noopener noreferrer">Budget › Q1</a>' in html
    assert 'class="internal-link unresolved"' in html            # Meg not in vault
    assert '<img src="/notes/file/s/raw" alt="sketch.png">' in html
    assert 'class="callout callout-warning"' in html and "Careful" in html
    assert 'type="checkbox"' in html and "<mark>important</mark>" in html
    assert "<table>" in html
    assert 'class="tag"' in html
    assert "<script" not in html and "onerror" not in html


# ------------------------------------------------------------------ service
def test_create_update_and_changes_feed(database):
    f = nid()
    r = n.put_text(f, "Inbox/Idea.md", "hello\n", None, A)
    assert r["status"] == "created" and r["file"]["version"] == 1
    seq1 = r["file"]["seq"]
    r = n.put_text(f, "Inbox/Idea.md", "hello\nworld\n", 1, A)
    assert r["status"] == "updated" and r["file"]["version"] == 2
    assert n.put_text(f, "Inbox/Idea.md", "hello\nworld\n", 1, B)["status"] == "unchanged"
    ch = n.changes(seq1)
    assert [c["id"] for c in ch["changes"]] == [f] and ch["last_seq"] > seq1 and not ch["more"]


def test_concurrent_edits_merge_or_conflict(database):
    f = nid()
    n.put_text(f, "Plan.md", "a\nb\nc\n", None, A)
    n.put_text(f, "Plan.md", "A\nb\nc\n", 1, A)               # laptop edits line 1 -> v2
    r = n.put_text(f, "Plan.md", "a\nb\nc\nd\n", 1, B)         # phone appended, based on v1
    assert r["status"] == "merged" and r["content"] == "A\nb\nc\nd\n" and r["file"]["version"] == 3
    n.put_text(f, "Plan.md", "B\nb\nc\nd\n", 3, A)             # laptop rewrites line 1 again -> v4
    r = n.put_text(f, "Plan.md", "X\nb\nc\nd\n", 3, B)         # phone rewrote line 1 from v3 too: overlap
    assert r["status"] == "conflict"
    assert r["conflict_copy"]["path"].startswith("Plan (conflict ") and r["conflict_copy"]["path"].endswith("phone).md")
    assert n.get(f)["content"] == "B\nb\nc\nd\n"                # server copy untouched
    assert n.get(r["conflict_copy"]["id"])["content"] == "X\nb\nc\nd\n"   # phone's version kept as a copy


def test_rename_delete_restore(database):
    f = nid()
    n.put_text(f, "a.md", "x", None, A)
    r = n.rename(f, "Folder/b.md", 1, A)
    assert r["file"]["path"] == "Folder/b.md" and r["file"]["id"] == f
    n.put_text(f, "Folder/b.md", "x2", 2, B)                    # phone edits (v3)
    assert n.delete(f, 2, A)["status"] == "conflict"           # laptop deletes an older version: refused
    assert n.delete(f, 3, A)["status"] == "deleted"
    feed = n.changes(0)["changes"]
    assert feed[-1]["id"] == f and feed[-1]["deleted"]
    assert n.put_text(f, "Folder/b.md", "back", 4, B)["status"] == "restored"


def test_path_taken_and_validation(database):
    n.put_text(nid(), "Same.md", "1", None, A)
    with pytest.raises(n.PathTaken):
        n.put_text(nid(), "same.md", "2", None, B)
    for bad in ["../x.md", "a/../../x.md", ".obsidian/app.json", ""]:
        with pytest.raises(n.Invalid):
            n.put_text(nid(), bad, "x", None, A)


def test_links_backlinks_and_append(database):
    meg = nid()
    n.put_text(meg, "People/Meg Okafor.md", "# Meg\n", None, A)
    plan = nid()
    n.put_text(plan, "Plan.md", "Ask [[Meg Okafor]] and [[Nobody]]", None, A)
    assert [b["id"] for b in n.backlinks(meg)] == [plan]
    out = {l["target"]: l["resolved"] for l in n.outgoing(plan)}
    assert out["Meg Okafor"]["id"] == meg and out["Nobody"] is None
    n.append("Plan", "- call her", "mcp:test", heading="Todo")
    n.append("Plan", "- and Tom", "mcp:test", heading="Todo")
    assert n.find("Plan")["content"].endswith("## Todo\n\n- call her\n- and Tom\n")
    created = n.append("Daily/2026-11-16", "first entry", "mcp:test")
    assert created["status"] == "created" and created["file"]["path"] == "Daily/2026-11-16.md"
    assert n.search("call her")[0]["id"] == plan


def test_attachments_and_tokens(database, blobs):
    data = b"\x89PNG fake"
    sha = n.sha256(data)
    with pytest.raises(n.Invalid):
        n.store_blob(sha, b"different")
    n.store_blob(sha, data, "image/png")
    assert n.has_blob(sha) and n.blob_file(sha).read_bytes() == data
    f = nid()
    r = n.put_attachment(f, "img/pic.png", sha, None, A)
    assert r["status"] == "created" and r["file"]["kind"] == "attachment"
    tok = n.create_token("laptop", "web")
    assert n.verify_token(tok["token"])["name"] == "laptop"
    n.revoke_token(tok["id"], "web")
    assert n.verify_token(tok["token"]) is None


# ------------------------------------------------------------------ HTTP API
def test_sync_api(database, blobs):
    from fastapi.testclient import TestClient
    from cove.web.app import app
    c = TestClient(app)
    assert c.get("/api/notes/whoami").status_code == 401
    tok = n.create_token("phone", "web")["token"]
    H = {"Authorization": f"Bearer {tok}"}
    assert c.get("/api/notes/whoami", headers=H).json()["device"] == "phone"
    f = nid()
    r = c.put(f"/api/notes/files/{f}", json={"path": "Hello.md", "content": "hi\n"}, headers=H)
    assert r.status_code == 201 and r.json()["file"]["version"] == 1
    r = c.put(f"/api/notes/files/{f}", json={"path": "Hello.md", "content": "hey\n", "base_version": 1}, headers=H)
    assert r.status_code == 200
    r = c.put(f"/api/notes/files/{f}", json={"path": "Hello.md", "content": "yo\n", "base_version": 1}, headers=H)
    assert r.status_code == 409 and r.json()["conflict_copy"]["path"].startswith("Hello (conflict")
    g = nid()
    r = c.put(f"/api/notes/files/{g}", json={"path": "hello.md", "content": "dup"}, headers=H)
    assert r.status_code == 409 and r.json()["status"] == "path_taken"
    data = b"bytes"
    sha = n.sha256(data)
    assert c.head(f"/api/notes/blobs/{sha}", headers=H).status_code == 404
    assert c.put(f"/api/notes/blobs/{sha}", content=data, headers=H).status_code == 200
    assert c.head(f"/api/notes/blobs/{sha}", headers=H).status_code == 200
    r = c.put(f"/api/notes/files/{nid()}", json={"path": "doc.pdf", "blob_sha256": sha}, headers=H)
    assert r.status_code == 201
    feed = c.get("/api/notes/changes?since=0", headers=H).json()
    assert {x["path"] for x in feed["changes"]} >= {"Hello.md", "doc.pdf"}
    from conftest import Browser
    page = Browser().get(f"/notes/view/{f}")
    assert page.status_code == 200 and "Hello" in page.text
    assert "device (phone)" in page.text


def test_graph_and_folder_tree(database):
    a, b, c, d = nid(), nid(), nid(), nid()
    n.put_text(a, "Areas/Econ/Hub.md", "[[Leaf]] [[Other]] [[Ghost]] #topic", None, A)
    n.put_text(b, "Areas/Econ/Leaf.md", "back to [[Hub]]", None, A)
    n.put_text(c, "Inbox/Other.md", "[[Far]]", None, A)
    n.put_text(d, "Far.md", "end", None, A)
    g = n.graph()
    ids = {x["id"] for x in g["nodes"]}
    assert ids == {a, b, c, d}
    pairs = {(l["source"], l["target"]) for l in g["links"]}
    assert (a, b) in pairs and (b, a) in pairs and (a, c) in pairs and (c, d) in pairs
    hub = next(x for x in g["nodes"] if x["id"] == a)
    assert hub["degree"] == 3
    local1 = {x["id"] for x in n.graph(a, 1)["nodes"]}
    assert local1 == {a, b, c}                         # Far is two hops away
    assert d in {x["id"] for x in n.graph(a, 2)["nodes"]}
    full = n.graph(tags=True, unresolved=True)
    assert any(x["kind"] == "tag" and x["title"] == "#topic" for x in full["nodes"])
    assert any(x["kind"] == "unresolved" and x["title"] == "Ghost" for x in full["nodes"])
    t = n.tree()
    assert [f["name"] for f in t["folders"]] == ["Areas", "Inbox"] and t["count"] == 4
    econ = n.folder("Areas/Econ")
    assert [f["name"] for f in econ["files"]] == ["Hub.md", "Leaf.md"] and n.folder("Nope") is None
    n.rename(d, "Inbox/Far.md", 1, A)                  # cache invalidates on any write
    assert [f["name"] for f in n.folder("Inbox")["files"]] == ["Far.md", "Other.md"]
