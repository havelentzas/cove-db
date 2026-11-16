from datetime import datetime, timedelta, timezone

import pytest

from cove.crm import service as s


def mk(name="Ada Lovelace", **kw):
    return s.create_contact(s.build_card(full_name=name, **kw), actor="test")


def test_create_get_and_derived_fields(database):
    c = mk(emails=["Ada@Example.com"], phones=["+44 20 7946 0000"], organization="Engines", birthday="--12-10")
    assert c["display_name"] == "Ada Lovelace" and c["version"] == 1
    assert c["card"]["uid"] == f"urn:uuid:{c['id']}"
    assert s.search_contacts("lovel")[0]["id"] == c["id"]
    assert s.search_contacts("example.com")[0]["id"] == c["id"]
    assert s.search_contacts("7946")[0]["id"] == c["id"]


def test_invalid_card_rejected(database):
    with pytest.raises(s.Invalid):
        s.create_contact({"@type": "Card", "version": "1.0"}, actor="test")
    with pytest.raises(s.Invalid):
        s.create_contact({"@type": "Card", "version": "1.0", "name": {"full": "X"}, "emails": {"e1": {}}}, actor="test")


def test_optimistic_locking_and_audit(database):
    c = mk()
    c2 = s.update_contact(c["id"], {"name": {"full": "Ada King"}}, expected_version=1, actor="mcp:test")
    assert c2["version"] == 2 and c2["display_name"] == "Ada King"
    with pytest.raises(s.Conflict) as e:
        s.update_contact(c["id"], {"name": {"full": "Stale"}}, expected_version=1, actor="web")
    assert e.value.current_version == 2
    trail = s.audit_trail(c["id"])
    assert [t["action"] for t in trail] == ["update", "create"]
    assert trail[0]["actor"] == "mcp:test" and "name" in trail[0]["diff"]


def test_keywords_become_tags(database):
    card = s.build_card(full_name="Grace")
    card["keywords"] = {"mentors": True}
    c = s.create_contact(card, actor="test")
    assert "keywords" not in c["card"]
    assert [t["name"] for t in c["tags"]] == ["mentors"]
    assert s.search_contacts(tag="Mentors")[0]["id"] == c["id"]


def test_interactions_and_follow_ups(database):
    a, b = mk("Ada"), mk("Grace")
    s.set_follow_up(a["id"], 14, actor="test")
    assert s.follow_ups_due() == []  # created just now, not due yet
    old = datetime.now(timezone.utc) - timedelta(days=30)
    s.log_interaction([a["id"], b["id"]], "Coffee", actor="test", kind="meeting", occurred_at=old.isoformat())
    due = s.follow_ups_due()
    assert [d["id"] for d in due] == [a["id"]]
    s.log_interaction([a["id"]], "Call", actor="test", kind="call")
    assert s.follow_ups_due() == []
    assert len(s.list_interactions(b["id"])) == 1
    with pytest.raises(s.Invalid):
        s.log_interaction([a["id"]], "x", actor="test", kind="carrier-pigeon")


def test_relationships_and_export(database):
    a, b = mk("Ada"), mk("Grace", emails=["grace@example.com"])
    s.relate_contacts(a["id"], b["id"], "friend", actor="test", note="met at uni")
    s.tag_contacts([a["id"], b["id"]], ["Climbing"], actor="test", kind="group")
    got = s.get_contact(b["id"])
    assert got["relationships"][0]["direction"] == "incoming"
    text = s.export_vcard()
    assert text.count("BEGIN:VCARD") == 2
    assert f"RELATED;TYPE=friend:urn:uuid:{b['id']}" in text
    assert "CATEGORIES:Climbing" in text


def test_import_preview_flags_duplicates(database):
    mk("Grace Hopper", emails=["grace@navy.example"])
    vcf = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:G. Hopper\r\nEMAIL:GRACE@navy.example\r\nEND:VCARD\r\n" \
          "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:New Person\r\nEND:VCARD\r\n"
    prev = s.preview_import(vcf)
    assert prev[0]["duplicates"] and not prev[1]["duplicates"]
    s.import_cards([prev[1]["card"]], actor="import")
    assert len(s.search_contacts()) == 2


def test_notes_and_describe(database):
    s.write_note("preferences", "User prefers first names", author="mcp:test")
    assert s.read_notes("preferences")[0]["note"] == "User prefers first names"
    d = s.describe_system()
    assert d["domains"][0]["name"] == "crm" and d["counts"]["active_contacts"] == 0
