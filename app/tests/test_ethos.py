"""The ethos page renders ETHOS.md for a signed-in user, with every principle and its anchor."""

import html
import re

from cove.web import ethos


def _principles(text: str) -> list[tuple[str, str]]:
    return re.findall(r"^### (\d+)\.\s+(.+)$", text, flags=re.M)


def test_ethos_file_has_principles():
    text = ethos.ETHOS_PATH.read_text()
    found = _principles(text)
    assert 7 <= len(found) <= 12, "the ethos should stay short enough to read"
    doc = ethos.parse(text)
    assert len(doc["principles"]) == len(found)
    for p in doc["principles"]:
        assert p["statement"] and p["why"] and p["practice"], f"principle {p['title']!r} is missing a part"
    assert doc["meta"].get("version") and doc["meta"].get("last reviewed")


def test_ethos_page_renders_every_principle(browser):
    r = browser.get("/ethos")
    assert r.status_code == 200
    body = html.unescape(r.text)
    for _, title in _principles(ethos.ETHOS_PATH.read_text()):
        assert title in body
        assert f'id="{ethos.slug(title)}"' in body
    for anchor in ("the-heart", "principles", "checklist-for-a-new-service", "living-document"):
        assert f'id="{anchor}"' in body


def test_ethos_is_behind_sign_in(database):
    from fastapi.testclient import TestClient
    from cove.web.app import app
    r = TestClient(app, base_url="https://cove.test", follow_redirects=False).get("/ethos")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_ethos_follows_the_file(browser, tmp_path, monkeypatch):
    f = tmp_path / "ETHOS.md"
    f.write_text("# Test ethos\n\nIntro.\n\n## Principles\n\n### 1. Be kind\n\nAlways.\n\n"
                 "**Why:** Because.\n\n**In practice:** Say thanks.\n")
    monkeypatch.setattr(ethos, "ETHOS_PATH", f)
    r = browser.get("/ethos")
    assert r.status_code == 200 and "Be kind" in r.text and 'id="be-kind"' in r.text
    monkeypatch.setattr(ethos, "ETHOS_PATH", tmp_path / "missing.md")
    assert browser.get("/ethos").status_code == 404
