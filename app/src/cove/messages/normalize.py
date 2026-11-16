"""Pure helpers for Messages: handle normalisation and one-time-code detection. No data access."""

from __future__ import annotations

import re
import unicodedata

from cove import phones

PLATFORMS = ("imessage", "sms", "rcs", "whatsapp", "signal", "telegram", "other")

# A one-time / verification code: a code-ish word and a 4–8 digit number (or 123-456), in a short message.
_OTP_WORDS = re.compile(
    r"\b(code|codes|otp|passcode|pass code|password|verification|verify|one[- ]time|2fa|two[- ]factor|"
    r"security code|login|log in|sign[- ]in|pin|tan|token|"
    r"verificatiecode|inlogcode|bevestigingscode|beveiligingscode|wachtwoord|"
    r"código|codigo|verificación|bestätigungscode|sicherheitscode|anmeldecode|"
    r"vérification|mot de passe|codice|verifica)\b|验证码|認証", re.I)
_OTP_NUMBER = re.compile(r"(?<![\d])(\d{4,8}|\d{3}[ -]\d{3})(?![\d])")


def is_sensitive(body: str | None) -> bool:
    """True for one-time and verification codes, which agents never see."""
    if not body or len(body) > 600:
        return False
    return bool(_OTP_WORDS.search(body) and _OTP_NUMBER.search(body))


def clean_name(name: str) -> str:
    """Display names from exports: NFC, invisible direction marks and odd spaces removed."""
    s = unicodedata.normalize("NFC", name or "")
    s = re.sub(r"[‎‏‪-‮⁦-⁩﻿]", "", s)
    s = re.sub(r"[    ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def handle_value(platform: str, raw: str, region: str | None = None) -> str:
    """The canonical (platform, value) value: E.164 phone, lower-case email, `name:<display name>`, else trimmed."""
    s = (raw or "").strip()
    if s.lower().startswith("name:"):
        return "name:" + clean_name(s[5:])[:300]
    s = s.removeprefix("tel:").removeprefix("mailto:")
    if "@" in s and phones.email(s):
        return phones.email(s)
    if phones.looks_like_phone(s):
        return phones.phone(s, region) or s
    return s.lower() if platform in ("imessage", "sms", "rcs") else s


def is_name_handle(value: str) -> bool:
    return value.startswith("name:")
