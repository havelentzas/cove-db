"""vCard <-> JSContact conversion, following the RFC 9555 mapping for the
properties cove supports.

* Import accepts vCard 3.0 and 4.0 (e.g. a Google Contacts export) via vobject.
* Export writes vCard 4.0 (RFC 6350) by hand, so the output is exactly 4.0.
* vCard properties without a JSContact equivalent are kept in the card's
  ``vCardProps`` (jCard-style ``[name, params, type, value]``) and re-emitted.
* JSContact properties without a vCard equivalent (e.g. ``cove.local:*``) are
  exported as ``JSPROP;JSPTR=...`` lines and restored on import (RFC 9555 §3.3).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Iterable

import vobject

# We report per-card errors ourselves; vobject otherwise logs every skipped line.
logging.getLogger("vobject").setLevel(logging.CRITICAL)

from cove.templating import date_from_form, date_to_form, new_id

# ---------------------------------------------------------------- shared vocab
CONTEXT_FROM_VCARD = {"work": "work", "home": "private"}
CONTEXT_TO_VCARD = {"work": "work", "private": "home"}
PHONE_FEATURES = {"cell": "mobile", "voice": "voice", "text": "text", "video": "video",
                  "fax": "fax", "pager": "pager", "textphone": "textphone"}
PHONE_FEATURES_REV = {v: k for k, v in PHONE_FEATURES.items()}
N_PARTS = ["surname", "given", "given2", "title", "credential"]  # vCard N order
ADR_PARTS = ["postOfficeBox", "apartment", "name", "locality", "region", "postcode", "country"]
SKIP_PROPS = {"VERSION", "PRODID", "REV", "BEGIN", "END"}
# JSContact properties handled explicitly on export
HANDLED_KEYS = {"@type", "version", "uid", "kind", "created", "updated", "name", "nicknames", "emails",
                "phones", "addresses", "onlineServices", "links", "media", "organizations", "titles",
                "anniversaries", "notes", "keywords", "relatedTo", "vCardProps", "language"}


# ======================================================================= IMPORT
def _params(line) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, vals in (line.params or {}).items():
        flat: list[str] = []
        for v in vals:
            flat.extend(p.strip() for p in str(v).split(","))
        out[k.upper()] = [v for v in flat if v]
    return out


def _types(line) -> set[str]:
    return {t.lower() for t in _params(line).get("TYPE", [])}


def _contexts(types: set[str]) -> dict[str, bool] | None:
    ctx = {CONTEXT_FROM_VCARD[t]: True for t in types if t in CONTEXT_FROM_VCARD}
    return ctx or None


def _pref(line, types: set[str]) -> int | None:
    p = _params(line).get("PREF")
    if p and p[0].isdigit():
        return max(1, min(100, int(p[0])))
    return 1 if "pref" in types else None


def _split_structured(v: Any, n: int) -> list[str]:
    """Return n components from a vobject structured value or a raw string."""
    if isinstance(v, str):
        parts = re.split(r"(?<!\\);", v)
        parts = [p.replace("\\;", ";").replace("\\,", ",") for p in parts]
    elif hasattr(v, "__dict__"):  # vobject Name / Address objects
        attrs = (["family", "given", "additional", "prefix", "suffix"] if n == 5
                 else ["box", "extended", "street", "city", "region", "code", "country"])
        parts = [getattr(v, a, "") for a in attrs]
    else:
        parts = list(v)
    parts = [" ".join(p) if isinstance(p, (list, tuple)) else (p or "") for p in parts]
    return (parts + [""] * n)[:n]


def _text_list(v: Any) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip().replace("\\,", ",") for s in re.split(r"(?<!\\),", str(v)) if s.strip()]


def _set(card: dict, prop: str, value: dict, prefix: str | None = None) -> None:
    m = card.setdefault(prop, {})
    m[new_id(m, prefix or prop[:1])] = {k: v for k, v in value.items() if v not in (None, "", {}, [])}


def _vcard_date(s: str) -> dict | None:
    s = str(s).strip()
    s = s.split("T")[0]
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    elif re.fullmatch(r"--\d{4}", s):
        s = f"--{s[2:4]}-{s[4:]}"
    try:
        return date_from_form(s)
    except ValueError:
        return None


def vcard_component_to_card(comp) -> dict:
    card: dict[str, Any] = {"@type": "Card", "version": "1.0"}
    extra: list[list] = []
    for line in comp.getChildren():
        name = line.name.upper()
        if name in SKIP_PROPS:
            continue
        value = line.value
        types = _types(line)
        if name == "FN":
            card.setdefault("name", {})["full"] = str(value).strip()
        elif name == "N":
            parts = _split_structured(value, 5)
            comps = [{"kind": kind, "value": p.strip()}
                     for kind, p in zip(["surname", "given", "given2", "title", "credential"], parts) if p.strip()]
            order = {"title": 0, "given": 1, "given2": 2, "surname": 3, "credential": 4}
            comps.sort(key=lambda c: order[c["kind"]])
            if comps:
                card.setdefault("name", {})["components"] = comps
        elif name == "NICKNAME":
            for nick in _text_list(value):
                _set(card, "nicknames", {"name": nick})
        elif name == "EMAIL":
            _set(card, "emails", {"address": str(value).strip(), "contexts": _contexts(types),
                                  "pref": _pref(line, types)})
        elif name == "TEL":
            number = str(value).strip()
            feats = {PHONE_FEATURES[t]: True for t in types if t in PHONE_FEATURES}
            _set(card, "phones", {"number": number, "features": feats or None,
                                  "contexts": _contexts(types), "pref": _pref(line, types)})
        elif name == "ADR":
            parts = _split_structured(value, 7)
            comps = [{"kind": k, "value": p.strip()} for k, p in zip(ADR_PARTS, parts) if p.strip()]
            label = _params(line).get("LABEL")
            _set(card, "addresses", {"components": comps, "full": label[0] if label else None,
                                     "contexts": _contexts(types), "pref": _pref(line, types)})
        elif name == "ORG":
            parts = value if isinstance(value, (list, tuple)) else _split_structured(value, 8)
            parts = [p.strip() for p in parts if p and p.strip()]
            if parts:
                _set(card, "organizations", {"name": parts[0], "units": [{"name": u} for u in parts[1:]]})
        elif name in ("TITLE", "ROLE"):
            _set(card, "titles", {"name": str(value).strip(), "kind": "title" if name == "TITLE" else "role"})
        elif name in ("BDAY", "ANNIVERSARY"):
            d = _vcard_date(value)
            if d:
                _set(card, "anniversaries", {"kind": "birth" if name == "BDAY" else "wedding", "date": d})
            else:
                extra.append([name.lower(), {}, "text", str(value)])
        elif name == "NOTE":
            _set(card, "notes", {"note": str(value)})
        elif name == "URL":
            _set(card, "links", {"uri": str(value).strip()})
        elif name in ("IMPP", "X-SOCIALPROFILE"):
            svc = (_params(line).get("X-SERVICE-TYPE") or [None])[0] or next(iter(t for t in types if t != "pref"), None)
            _set(card, "onlineServices", {"uri": str(value).strip(), "service": svc}, "s")
        elif name == "PHOTO":
            if isinstance(value, bytes):
                mime = "image/" + next(iter(t for t in types if t in ("png", "gif", "jpeg", "jpg")), "jpeg").replace("jpg", "jpeg")
                uri = f"data:{mime};base64,{base64.b64encode(value).decode()}"
            else:
                uri = str(value).strip()
            _set(card, "media", {"kind": "photo", "uri": uri})
        elif name == "CATEGORIES":
            for kw in _text_list(value):
                card.setdefault("keywords", {})[kw] = True
        elif name == "RELATED":
            rel = {t: True for t in types} or {"contact": True}
            card.setdefault("relatedTo", {})[str(value).strip()] = {"relation": rel}
        elif name == "UID":
            card["cove.local:importedUid"] = str(value).strip()
        elif name == "KIND":
            card["kind"] = str(value).strip().lower()
        elif name == "JSPROP":
            ptr = (_params(line).get("JSPTR") or [None])[0]
            if ptr and "/" not in ptr:
                raw = str(value).replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n").replace("\\\\", "\\")
                try:
                    card[ptr] = json.loads(raw)
                except json.JSONDecodeError:
                    card[ptr] = raw
        else:
            params = {k.lower(): (v[0] if len(v) == 1 else v) for k, v in _params(line).items()}
            if line.group:
                params["group"] = line.group
            val = base64.b64encode(value).decode() if isinstance(value, bytes) else str(value)
            extra.append([name.lower(), params, "unknown", val])
    if extra:
        card["vCardProps"] = extra
    return card


_BLOCK_RE = re.compile(r"^BEGIN:VCARD[ \t]*$.*?^END:VCARD[ \t]*$", re.I | re.M | re.S)
_FN_RE = re.compile(r"^FN[;:](?:[^:\n]*:)?(.*)$", re.I | re.M)


def normalize_newlines(text: str) -> str:
    """Accept CRLF, LF, bare CR and doubled CR (CRCRLF, produced by some export/transfer
    tools). Without this, folded lines (e.g. base64 photos) are torn apart."""
    return re.sub(r"\r+\n|\r", "\n", text.lstrip("﻿"))


def parse_vcards_detailed(text: str) -> tuple[list[dict], list[str]]:
    """Parse vCards one block at a time so a single malformed card doesn't sink the batch.
    Returns (cards, errors)."""
    cards, errors = [], []
    for n, block in enumerate(_BLOCK_RE.findall(normalize_newlines(text)), 1):
        fn = _FN_RE.search(block)
        label = f"card #{n}" + (f" ({fn.group(1).strip()[:60]})" if fn else "")
        try:
            comp = vobject.readOne(block, ignoreUnreadable=True)
            cards.append(vcard_component_to_card(comp))
        except Exception as e:  # vobject raises many exception types
            errors.append(f"{label}: {str(e).splitlines()[0][:200] if str(e) else type(e).__name__}")
    return cards, errors


def parse_vcards(text: str) -> list[dict]:
    """Parse one or more vCards (2.1/3.0/4.0) into JSContact cards, skipping malformed ones."""
    return parse_vcards_detailed(text)[0]


# ======================================================================= EXPORT
def _esc(s: Any) -> str:
    return (str(s).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "")
            .replace(",", "\\,").replace(";", "\\;"))


def _esc_component(s: Any) -> str:
    return _esc(s)


def _param_val(v: str) -> str:
    v = str(v).replace('"', "'").replace("\n", " ")
    return f'"{v}"' if any(c in v for c in ':;,') else v


def _fold(line: str) -> str:
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        limit = 75 if not out else 74
        if len(cur) + len(b) > limit:
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def _line(name: str, value: str, params: dict[str, Any] | None = None) -> str:
    p = ""
    for k, v in (params or {}).items():
        if v in (None, "", []):
            continue
        if isinstance(v, (list, tuple, set)):
            v = ",".join(sorted(str(x) for x in v))
        p += f";{k.upper()}={_param_val(v)}"
    return _fold(f"{name}{p}:{value}")


def _types_param(entry: dict, extra: Iterable[str] = ()) -> list[str]:
    t = [CONTEXT_TO_VCARD[c] for c in (entry.get("contexts") or {}) if c in CONTEXT_TO_VCARD]
    return t + list(extra)


def display_name(card: dict) -> str:
    name = card.get("name") or {}
    if name.get("full"):
        return name["full"]
    comps = [c["value"] for c in name.get("components", []) if c.get("kind") not in ("separator",)]
    if comps:
        return " ".join(comps)
    for e in (card.get("emails") or {}).values():
        return e.get("address", "")
    for o in (card.get("organizations") or {}).values():
        if o.get("name"):
            return o["name"]
    return "Unnamed contact"


def _vdate(d: dict) -> str | None:
    s = date_to_form(d)
    if not s:
        return None
    return s.replace("-", "") if not s.startswith("--") else "--" + s[2:].replace("-", "")


def card_to_vcard(card: dict, keywords: Iterable[str] = (), related: Iterable[tuple[str, str]] = ()) -> str:
    """Serialize one JSContact card as vCard 4.0. ``keywords``/``related`` add
    CRM tags and relationships (uid, type) that live outside the card."""
    L = ["BEGIN:VCARD", "VERSION:4.0"]
    if card.get("uid"):
        L.append(_line("UID", card["uid"]))
    if card.get("kind") and card["kind"] != "individual":
        L.append(_line("KIND", card["kind"]))
    L.append(_line("FN", _esc(display_name(card))))
    comps = (card.get("name") or {}).get("components") or []
    if comps:
        by_kind: dict[str, list[str]] = {}
        for c in comps:
            by_kind.setdefault(c.get("kind"), []).append(c.get("value", ""))
        n = [",".join(_esc_component(x) for x in by_kind.get(k, [])) for k in N_PARTS]
        L.append(_line("N", ";".join(n)))
    nicks = [n["name"] for n in (card.get("nicknames") or {}).values() if n.get("name")]
    if nicks:
        L.append(_line("NICKNAME", ",".join(_esc(n) for n in nicks)))
    for e in (card.get("emails") or {}).values():
        L.append(_line("EMAIL", _esc(e["address"]), {"TYPE": _types_param(e), "PREF": e.get("pref")}))
    for p in (card.get("phones") or {}).values():
        feats = [PHONE_FEATURES_REV[f] for f in (p.get("features") or {}) if f in PHONE_FEATURES_REV]
        L.append(_line("TEL", _esc(p["number"]), {"TYPE": _types_param(p, feats), "PREF": p.get("pref")}))
    for a in (card.get("addresses") or {}).values():
        parts = {k: [] for k in ADR_PARTS}
        for c in a.get("components") or []:
            kind = c.get("kind")
            if kind == "number":  # house number joins the street line
                parts["name"].insert(0, c.get("value", ""))
            elif kind in parts:
                parts[kind].append(c.get("value", ""))
        value = ";".join(_esc_component(" ".join(parts[k])) for k in ADR_PARTS)
        L.append(_line("ADR", value, {"TYPE": _types_param(a), "LABEL": a.get("full"), "PREF": a.get("pref")}))
    for o in (card.get("organizations") or {}).values():
        if o.get("name"):
            units = [u.get("name", "") for u in o.get("units") or []]
            L.append(_line("ORG", ";".join(_esc_component(x) for x in [o["name"], *units])))
    for t in (card.get("titles") or {}).values():
        L.append(_line("ROLE" if t.get("kind") == "role" else "TITLE", _esc(t["name"])))
    for a in (card.get("anniversaries") or {}).values():
        d = _vdate(a.get("date") or {})
        if d:
            L.append(_line("BDAY" if a.get("kind") == "birth" else "ANNIVERSARY", d))
    for n in (card.get("notes") or {}).values():
        L.append(_line("NOTE", _esc(n["note"])))
    for link in (card.get("links") or {}).values():
        L.append(_line("URL", link["uri"]))
    for s in (card.get("onlineServices") or {}).values():
        if s.get("uri"):
            L.append(_line("IMPP", s["uri"], {"X-SERVICE-TYPE": s.get("service")}))
    for m in (card.get("media") or {}).values():
        if m.get("kind") == "photo" and m.get("uri"):
            L.append(_line("PHOTO", m["uri"]))
        elif m.get("kind") == "logo" and m.get("uri"):
            L.append(_line("LOGO", m["uri"]))
    kws = sorted(set(keywords) | {k for k, v in (card.get("keywords") or {}).items() if v})
    if kws:
        L.append(_line("CATEGORIES", ",".join(_esc(k) for k in kws)))
    rels: dict[str, set[str]] = {}
    for uid, rel in (card.get("relatedTo") or {}).items():
        rels.setdefault(uid, set()).update(k for k, v in ((rel or {}).get("relation") or {}).items() if v)
    for uid, rtype in related:
        rels.setdefault(uid, set()).add(rtype)
    for uid, types in rels.items():
        L.append(_line("RELATED", uid, {"TYPE": sorted(types - {"contact"})}))
    for key, val in card.items():
        if key not in HANDLED_KEYS and not key.startswith("@"):
            L.append(_line("JSPROP", _esc(json.dumps(val, ensure_ascii=False)), {"JSPTR": key}))
    for prop in card.get("vCardProps") or []:
        name, params, _typ, val = prop[0], dict(prop[1] or {}), prop[2], prop[3]
        group = params.pop("group", None)
        L.append((f"{group}." if group else "") + _line(name.upper(), str(val), params))
    if card.get("updated"):
        L.append(_line("REV", card["updated"].replace("-", "").replace(":", "").split(".")[0].rstrip("Z") + "Z"))
    L.append("END:VCARD")
    return "\r\n".join(L) + "\r\n"
