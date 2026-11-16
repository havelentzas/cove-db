"""Phone numbers and other identifiers in one canonical form, shared by every service.

Phone numbers become E.164 (`+31612345678`). Numbers written without a country code are read in the default
region (`COVE_PHONE_REGION`, default NL; ISO 3166 alpha-2). Short codes and anything that doesn't look like a
full number keep their digits only, so they still compare equal to themselves. Stdlib only, deliberately
small: it covers the trunk-prefix rules of the regions listed in REGIONS.
"""

from __future__ import annotations

import os
import re

# region -> (country calling code, national trunk prefix)
REGIONS = {
    "NL": ("31", "0"), "BE": ("32", "0"), "DE": ("49", "0"), "FR": ("33", "0"), "GB": ("44", "0"),
    "IE": ("353", "0"), "LU": ("352", ""), "AT": ("43", "0"), "CH": ("41", "0"), "ES": ("34", ""),
    "PT": ("351", ""), "IT": ("39", ""), "DK": ("45", ""), "NO": ("47", ""), "SE": ("46", "0"),
    "FI": ("358", "0"), "PL": ("48", ""), "CZ": ("420", ""), "GR": ("30", ""), "TR": ("90", "0"),
    "US": ("1", "1"), "CA": ("1", "1"), "MX": ("52", ""), "BR": ("55", "0"), "AR": ("54", "0"),
    "AU": ("61", "0"), "NZ": ("64", "0"), "IN": ("91", "0"), "ZA": ("27", "0"), "JP": ("81", "0"),
    "SG": ("65", ""), "HK": ("852", ""), "AE": ("971", "0"), "IL": ("972", "0"),
}
_PHONEISH = re.compile(r"^\+?[\d\s().\-/]{3,}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


def default_region() -> str:
    return (os.environ.get("COVE_PHONE_REGION") or "NL").upper()


def looks_like_phone(value: str) -> bool:
    return bool(_PHONEISH.match((value or "").strip())) and sum(ch.isdigit() for ch in value) >= 3


def phone(raw: str, region: str | None = None) -> str | None:
    """E.164 for a full number, bare digits for a short code, None if there are no digits."""
    s = (raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if s.startswith("+"):
        return "+" + digits
    if digits.startswith("00") and len(digits) > 8:
        return "+" + digits[2:]
    cc, trunk = REGIONS.get((region or default_region()).upper(), (None, None))
    if cc is None or len(digits) < 7:      # unknown region, or a short code
        return digits
    if cc == "1":                          # North America: 10 digits, optional leading 1
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return digits
    if trunk and digits.startswith(trunk) and not digits.startswith(trunk * 2):
        return "+" + cc + digits[len(trunk):]
    if not trunk and len(digits) >= 8:
        return "+" + cc + digits
    return digits


def email(raw: str) -> str | None:
    s = (raw or "").strip().lower().removeprefix("mailto:")
    return s if _EMAIL.match(s) else None


def identifier(raw: str, region: str | None = None) -> str:
    """Canonical form of any identifier: email lower-cased, phone as E.164, anything else trimmed."""
    s = (raw or "").strip()
    if "@" in s and email(s):
        return email(s)
    if looks_like_phone(s):
        return phone(s, region) or s
    return s
