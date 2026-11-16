from cove.crm.vcard import card_to_vcard, parse_vcards, parse_vcards_detailed

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


def _photo_card(eol: str, name: str = "Pic Person") -> str:
    import base64, os
    b64 = base64.b64encode(os.urandom(600)).decode()
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{name}", "PHOTO;ENCODING=b;TYPE=JPEG:" + b64[:48]]
    lines += [" " + b64[i:i + 74] for i in range(48, len(b64), 74)]
    lines += ["END:VCARD", ""]
    return eol.join(lines)


def test_doubled_carriage_returns_with_folded_photo():
    # CR CR LF line endings tore folded base64 photos apart (real-world upload failure)
    for eol in ("\r\n", "\n", "\r", "\r\r\n"):
        cards, errors = parse_vcards_detailed(_photo_card(eol))
        assert errors == [], eol
        photo = next(iter(cards[0]["media"].values()))["uri"]
        assert photo.startswith("data:image/jpeg;base64,") and len(photo) > 800, eol


def test_bad_card_does_not_sink_batch():
    bad = "BEGIN:VCARD\nVERSION:3.0\nFN:Broken\nBDAY;VALUE=date:\x00\x01garbage\nN:;;;\nEMAIL;TYPE=\"unterminated:x@y\nEND:VCARD\n"
    text = _photo_card("\n", "First") + bad + _photo_card("\n", "Last")
    cards, errors = parse_vcards_detailed(text)
    names = [c["name"]["full"] for c in cards]
    assert "First" in names and "Last" in names
    assert len(cards) + len(errors) == 3
