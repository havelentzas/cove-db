from cove.crm.vcard import card_to_vcard, parse_vcards

GOOGLE_V3 = """BEGIN:VCARD
VERSION:3.0
FN:Grace Hopper
N:Hopper;Grace;Brewster;Rear Admiral;
NICKNAME:Amazing Grace
EMAIL;TYPE=INTERNET;TYPE=WORK:grace@navy.example
TEL;TYPE=CELL:+1 555 0100
ADR;TYPE=HOME:;;1 Main St;Arlington;VA;22201;USA
ORG:US Navy;Computing
TITLE:Computer scientist
BDAY:1906-12-09
NOTE:Invented the compiler\\, roughly.
URL:https://example.com/grace
CATEGORIES:myContacts,Mentors
item1.X-ABLabel:Custom label
END:VCARD
"""


def test_import_google_v3():
    [card] = parse_vcards(GOOGLE_V3)
    assert card["name"]["full"] == "Grace Hopper"
    kinds = {c["kind"]: c["value"] for c in card["name"]["components"]}
    assert kinds["surname"] == "Hopper" and kinds["given"] == "Grace" and kinds["title"] == "Rear Admiral"
    email = next(iter(card["emails"].values()))
    assert email["address"] == "grace@navy.example" and email["contexts"] == {"work": True}
    phone = next(iter(card["phones"].values()))
    assert phone["features"] == {"mobile": True}
    adr = next(iter(card["addresses"].values()))
    assert {c["kind"]: c["value"] for c in adr["components"]}["locality"] == "Arlington"
    assert next(iter(card["organizations"].values())) == {"name": "US Navy", "units": [{"name": "Computing"}]}
    assert next(iter(card["anniversaries"].values()))["date"] == {"@type": "PartialDate", "year": 1906, "month": 12, "day": 9}
    assert next(iter(card["notes"].values()))["note"] == "Invented the compiler, roughly."
    assert card["keywords"] == {"myContacts": True, "Mentors": True}
    assert card["vCardProps"][0][0] == "x-ablabel"


def test_round_trip_v4():
    [card] = parse_vcards(GOOGLE_V3)
    card["uid"] = "urn:uuid:00000000-0000-4000-8000-000000000001"
    card["cove.local:howWeMet"] = "At a conference; she gave the keynote"
    text = card_to_vcard(card, keywords=["Friends"], related=[("urn:uuid:00000000-0000-4000-8000-000000000002", "friend")])
    assert "VERSION:4.0" in text and 'JSPROP;JSPTR="cove.local:howWeMet"' in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    [back] = parse_vcards(text)
    assert back["name"]["full"] == "Grace Hopper"
    assert {e["address"] for e in back["emails"].values()} == {"grace@navy.example"}
    assert back["keywords"] == {"myContacts": True, "Mentors": True, "Friends": True}
    assert back["cove.local:howWeMet"] == "At a conference; she gave the keynote"
    assert back["relatedTo"]["urn:uuid:00000000-0000-4000-8000-000000000002"]["relation"] == {"friend": True}
    assert next(iter(back["anniversaries"].values()))["date"]["year"] == 1906
    assert next(iter(back["notes"].values()))["note"] == "Invented the compiler, roughly."


def test_long_unicode_line_folds():
    card = {"@type": "Card", "version": "1.0", "name": {"full": "Zoë " + "ü" * 80}}
    text = card_to_vcard(card)
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert parse_vcards(text)[0]["name"]["full"] == "Zoë " + "ü" * 80
