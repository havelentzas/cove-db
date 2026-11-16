"""Generic record templates: map form fields onto paths inside a JSON record.

A template (see templates/contact-default.json) lists sections of fields. Each
field has a slash-separated ``path`` into the record:

    name/full                          plain keys
    name/components[kind=given]/value  array item whose ``kind`` == ``given``
    anniversaries{kind=birth}/date     id-keyed map entry whose ``kind`` == ``birth``
    organizations{*}/name              first entry of an id-keyed map

A section with ``"repeat": "<map>"`` binds to an id-keyed map (e.g. JSContact
``emails``); its field paths are relative to one entry. Writing a form back
starts from the existing record, so properties the template doesn't know about
are preserved.

Nothing here is specific to contacts: another domain only needs its own
JSON Schema and template file.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

TEMPLATE_DIR = Path(os.environ.get("COVE_TEMPLATE_DIR", Path(__file__).resolve().parents[3] / "templates"))

_SEG_RE = re.compile(r"^(?P<name>[^\[\]{}]+)(?:\[(?P<ak>[^=\]]+)=(?P<av>[^\]]*)\]|\{(?P<star>\*)\}|\{(?P<mk>[^=}]+)=(?P<mv>[^}]*)\})?$")


class FormError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Seg:
    name: str
    kind: str | None = None  # None | "arr" | "map" | "first"
    key: str | None = None
    value: str | None = None


@lru_cache(maxsize=None)
def parse_path(path: str) -> tuple[Seg, ...]:
    segs = []
    for part in path.split("/"):
        m = _SEG_RE.match(part)
        if not m:
            raise ValueError(f"bad path segment {part!r} in {path!r}")
        if m["ak"]:
            segs.append(Seg(m["name"], "arr", m["ak"], m["av"]))
        elif m["mk"]:
            segs.append(Seg(m["name"], "map", m["mk"], m["mv"]))
        elif m["star"]:
            segs.append(Seg(m["name"], "first"))
        else:
            segs.append(Seg(m["name"]))
    return tuple(segs)


def load_template(name: str) -> dict:
    return json.loads((TEMPLATE_DIR / f"{name}.json").read_text())


# --------------------------------------------------------------------------- paths
def _find(container: Any, seg: Seg) -> tuple[Any, Any]:
    """Return (key_or_index, item) selected by seg inside container, or (None, None)."""
    if seg.kind == "arr" and isinstance(container, list):
        for i, item in enumerate(container):
            if isinstance(item, dict) and str(item.get(seg.key)) == seg.value:
                return i, item
    elif seg.kind == "map" and isinstance(container, dict):
        for k, item in container.items():
            if isinstance(item, dict) and str(item.get(seg.key)) == seg.value:
                return k, item
    elif seg.kind == "first" and isinstance(container, dict) and container:
        k = next(iter(container))
        return k, container[k]
    return None, None


def get_path(record: Mapping, path: str) -> Any:
    node: Any = record
    for seg in parse_path(path):
        if not isinstance(node, Mapping):
            return None
        node = node.get(seg.name)
        if seg.kind:
            _, node = _find(node, seg)
        if node is None:
            return None
    return node


def new_id(existing: Mapping, prefix: str) -> str:
    n = 1
    while f"{prefix}{n}" in existing:
        n += 1
    return f"{prefix}{n}"


def _is_empty(d: Any, ignore: set[str]) -> bool:
    return isinstance(d, dict) and all(k in ignore for k in d)


def set_path(record: dict, path: str, value: Any) -> None:
    """Set value at path (creating selected items). value None deletes and prunes."""
    _set(record, parse_path(path), value)


def _set(node: dict, segs: tuple[Seg, ...], value: Any) -> None:
    seg, rest = segs[0], segs[1:]
    if seg.kind is None:
        if not rest:
            if value is None:
                node.pop(seg.name, None)
            else:
                node[seg.name] = value
            return
        child = node.get(seg.name)
        if not isinstance(child, dict):
            if value is None:
                return
            child = node[seg.name] = {}
        _set(child, rest, value)
        if value is None and not child:
            node.pop(seg.name, None)
        return

    if not rest:
        raise ValueError("a selector cannot be the last path segment")
    container = node.get(seg.name)
    key, item = _find(container, seg)
    ignore = {"@type"} | ({seg.key} if seg.key else set())
    if item is None:
        if value is None:
            return
        item = {seg.key: seg.value} if seg.key else {}
        if seg.kind == "arr":
            if not isinstance(container, list):
                container = node[seg.name] = []
            container.append(item)
            key = len(container) - 1
        else:
            if not isinstance(container, dict):
                container = node[seg.name] = {}
            key = new_id(container, seg.name[:1])
            container[key] = item
    _set(item, rest, value)
    if value is None and _is_empty(item, ignore):
        del container[key]
        if not container:
            node.pop(seg.name, None)


# --------------------------------------------------------------------- value codecs
_DATE_RE = re.compile(r"^(?:(?P<y>\d{4})(?:-(?P<m>\d{2})(?:-(?P<d>\d{2}))?)?|--(?P<m2>\d{2})-(?P<d2>\d{2}))$")


def date_to_form(v: Any) -> str:
    if not isinstance(v, dict):
        return ""
    if v.get("@type") == "Timestamp":
        return str(v.get("utc", ""))[:10]
    y, m, d = v.get("year"), v.get("month"), v.get("day")
    if y and m and d:
        return f"{y:04d}-{m:02d}-{d:02d}"
    if m and d:
        return f"--{m:02d}-{d:02d}"
    if y and m:
        return f"{y:04d}-{m:02d}"
    return f"{y:04d}" if y else ""


def date_from_form(s: str) -> dict:
    m = _DATE_RE.match(s.strip())
    if not m:
        raise ValueError(f"{s!r} is not a date (use YYYY-MM-DD or --MM-DD)")
    out: dict[str, Any] = {"@type": "PartialDate"}
    if m["y"]:
        out["year"] = int(m["y"])
        if m["m"]:
            out["month"] = int(m["m"])
        if m["d"]:
            out["day"] = int(m["d"])
    else:
        out["month"], out["day"] = int(m["m2"]), int(m["d2"])
    return out


def to_form_value(field: dict, raw: Any) -> Any:
    t = field.get("type", "text")
    if t == "set":
        return [k for k, v in (raw or {}).items() if v] if isinstance(raw, dict) else []
    if t == "date":
        return date_to_form(raw)
    return "" if raw is None else str(raw)


def from_form_value(field: dict, raw: Any) -> Any:
    """Convert submitted form input to the stored value (None = absent)."""
    t = field.get("type", "text")
    if t == "set":
        vals = [v for v in (raw or []) if v]
        return {v: True for v in vals} or None
    s = (raw or "").strip() if isinstance(raw, str) else ("" if raw is None else str(raw))
    if not s:
        return None
    if t == "date":
        return date_from_form(s)
    if t == "select" and field.get("options") and s not in field["options"]:
        raise ValueError(f"{s!r} is not an allowed value")
    return s


# --------------------------------------------------------------------- form <-> record
def record_to_form(template: dict, record: Mapping) -> dict:
    """Values for rendering: {"fields": {key: value}, "repeats": {map: [row, ...]}}."""
    fields: dict[str, Any] = {}
    repeats: dict[str, list[dict]] = {}
    for section in template["sections"]:
        if rep := section.get("repeat"):
            rows = []
            for entry_id, entry in (record.get(rep) or {}).items():
                row = {"_id": entry_id}
                for f in section["fields"]:
                    row[f["key"]] = to_form_value(f, get_path(entry, f["path"]))
                rows.append(row)
            repeats[rep] = rows
        else:
            for f in section["fields"]:
                fields[f["key"]] = to_form_value(f, get_path(record, f["path"]))
    return {"fields": fields, "repeats": repeats}


def _getlist(form: Any, name: str) -> list:
    if hasattr(form, "getlist"):
        return list(form.getlist(name))
    v = form.get(name)
    return v if isinstance(v, list) else ([] if v is None else [v])


def form_to_record(template: dict, form: Any, base: Mapping | None = None) -> dict:
    """Apply submitted form data onto a copy of ``base`` and return the new record.

    Field names: scalar ``f__<key>``; repeat rows ``r__<map>__<i>__<key>`` with
    ``r__<map>__<i>___id`` carrying the existing entry id.
    """
    record = copy.deepcopy(dict(base or {}))
    errors: list[str] = []

    def read(f: dict, name: str) -> Any:
        if f.get("type") == "set":
            return _getlist(form, name)
        return form.get(name)

    for section in template["sections"]:
        rep = section.get("repeat")
        if not rep:
            for f in section["fields"]:
                try:
                    set_path(record, f["path"], from_form_value(f, read(f, f"f__{f['key']}")))
                except ValueError as e:
                    errors.append(f"{f['label']}: {e}")
            continue

        old = record.get(rep) or {}
        prefix = f"r__{rep}__"
        indices = sorted({int(k[len(prefix):].split("__")[0]) for k in form.keys()
                          if k.startswith(prefix) and k[len(prefix):].split("__")[0].isdigit()})
        new: dict[str, Any] = {}
        for i in indices:
            entry_id = (form.get(f"{prefix}{i}___id") or "").strip()
            entry = copy.deepcopy(old.get(entry_id, {})) if entry_id else {}
            values = {}
            for f in section["fields"]:
                try:
                    values[f["key"]] = from_form_value(f, read(f, f"{prefix}{i}__{f['key']}"))
                except ValueError as e:
                    errors.append(f"{section['title']} #{i + 1} {f['label']}: {e}")
                    values[f["key"]] = None
            # a row with no user-facing values is a blank/removed row
            if all(v is None for v in values.values()):
                continue
            missing = [f["label"] for f in section["fields"] if f.get("required") and values[f["key"]] is None]
            if missing:
                errors.append(f"{section['title']} #{i + 1}: {', '.join(missing)} required")
                continue
            for f in section["fields"]:
                set_path(entry, f["path"], values[f["key"]])
            key = entry_id if entry_id and entry_id not in new else new_id({**old, **new}, rep[:1])
            new[key] = entry
        if new:
            record[rep] = new
        else:
            record.pop(rep, None)

    if errors:
        raise FormError(errors)
    return record
