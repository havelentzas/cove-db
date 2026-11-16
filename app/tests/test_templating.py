import pytest

from cove.templating import (FormError, date_from_form, date_to_form, form_to_record, get_path,
                             load_template, record_to_form, set_path)

CARD = {
    "@type": "Card", "version": "1.0",
    "name": {"full": "Ada Lovelace", "components": [{"kind": "given", "value": "Ada"}, {"kind": "surname", "value": "Lovelace"}]},
    "emails": {"e1": {"address": "ada@example.com", "contexts": {"work": True}, "pref": 1}},
    "anniversaries": {"a1": {"kind": "birth", "date": {"@type": "PartialDate", "month": 12, "day": 10}}},
    "organizations": {"o1": {"name": "Analytical Engines"}},
    "example.com:unknown": {"keep": "me"},
}


def test_get_path_selectors():
    assert get_path(CARD, "name/components[kind=surname]/value") == "Lovelace"
    assert get_path(CARD, "anniversaries{kind=birth}/date")["month"] == 12
    assert get_path(CARD, "organizations{*}/name") == "Analytical Engines"
    assert get_path(CARD, "name/components[kind=given2]/value") is None


def test_set_path_creates_and_prunes():
    rec = {}
    set_path(rec, "name/components[kind=given]/value", "Grace")
    set_path(rec, "anniversaries{kind=birth}/date", {"@type": "PartialDate", "year": 1906})
    assert rec["name"]["components"] == [{"kind": "given", "value": "Grace"}]
    assert list(rec["anniversaries"].values())[0]["kind"] == "birth"
    set_path(rec, "name/components[kind=given]/value", None)
    set_path(rec, "anniversaries{kind=birth}/date", None)
    assert rec == {}


def test_dates():
    assert date_from_form("1990-05-03") == {"@type": "PartialDate", "year": 1990, "month": 5, "day": 3}
    assert date_from_form("--12-10") == {"@type": "PartialDate", "month": 12, "day": 10}
    assert date_to_form({"@type": "PartialDate", "month": 12, "day": 10}) == "--12-10"
    with pytest.raises(ValueError):
        date_from_form("10/12/1990")


def test_form_round_trip_preserves_unknown():
    t = load_template("contact-default")
    form = record_to_form(t, CARD)
    assert form["fields"]["birthday"] == "--12-10"
    assert form["repeats"]["emails"][0]["context"] == ["work"]

    data = {"f__full_name": "Ada King", "f__given": "Ada", "f__surname": "King", "f__birthday": "1815-12-10",
            "f__org": "Analytical Engines",
            "r__emails__0___id": "e1", "r__emails__0__address": "ada@example.com", "r__emails__0__context": ["work"],
            "r__emails__1___id": "", "r__emails__1__address": "ada@home.example", "r__emails__1__context": ["private"],
            "r__phones__0___id": "", "r__phones__0__number": ""}
    rec = form_to_record(t, data, CARD)
    assert rec["name"]["full"] == "Ada King"
    assert rec["example.com:unknown"] == {"keep": "me"}           # unknown props survive
    assert rec["emails"]["e1"]["pref"] == 1                         # unknown entry fields survive
    assert {e["address"] for e in rec["emails"].values()} == {"ada@example.com", "ada@home.example"}
    assert "phones" not in rec                                      # blank row ignored
    assert list(rec["anniversaries"].values())[0]["date"]["year"] == 1815


def test_form_errors():
    t = load_template("contact-default")
    with pytest.raises(FormError) as e:
        form_to_record(t, {"f__birthday": "not a date", "r__emails__0__context": ["work"]}, {})
    assert any("Birthday" in m for m in e.value.errors)
    assert any("Address required" in m for m in e.value.errors)
