"""Obsidian Flavored Markdown: parsing (for the database) and rendering (for the web UI).

Parsing never changes the note; it only derives title, frontmatter, tags, aliases and links.
Rendering produces sanitized HTML: user notes may contain raw HTML (Obsidian allows it), so
everything goes through nh3 (ammonia) with an allow-list before it reaches the browser.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote, unquote

import nh3
import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"(!?)\[[^\]\n]*\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
TAG_RE = re.compile(r"(?<![\w#/&])#([A-Za-z0-9_/\-]*[A-Za-z_/\-][A-Za-z0-9_/\-]*)")
EXTERNAL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.I)
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"}


# =========================================================================== keys
def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def link_key(target: str) -> str:
    """Normalised link target used for resolution: lower-case, no leading ./ or /, no .md."""
    t = nfc(unquote(target)).strip().lstrip("./").lstrip("/")
    t = re.sub(r"\.md$", "", t, flags=re.I)
    return t.lower()


def path_keys(path: str) -> tuple[str, str]:
    """(full-path key, file-name key) for a vault path, matching link_key()."""
    full = link_key(path)
    return full, full.rsplit("/", 1)[-1]


def split_target(raw: str) -> tuple[str, str | None, str | None]:
    """'Folder/Note#Heading|Alias' -> ('Folder/Note', 'Heading', 'Alias')."""
    alias = None
    if "|" in raw:
        raw, alias = raw.split("|", 1)
    anchor = None
    if "#" in raw:
        raw, anchor = raw.split("#", 1)
    return raw.strip(), (anchor or None), (alias.strip() if alias else None)


# =========================================================================== parse
@dataclass
class Parsed:
    title: str
    frontmatter: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return _jsonable(data), text[m.end():]


def _jsonable(v):
    """YAML can produce dates etc.; make everything JSON-friendly."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"[,\s]+", v) if p.strip()]
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def strip_code(body: str) -> str:
    return INLINE_CODE_RE.sub("", FENCE_RE.sub("", body))


def title_for(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return re.sub(r"\.(md|canvas)$", "", name, flags=re.I)


def parse_note(path: str, text: str) -> Parsed:
    fm, body = split_frontmatter(text)
    clean = strip_code(body)
    tags = {t.lstrip("#") for t in _as_list(fm.get("tags") or fm.get("tag"))}
    tags |= set(TAG_RE.findall(clean))
    aliases = _as_list(fm.get("aliases") or fm.get("alias"))
    links = []
    for bang, raw in WIKILINK_RE.findall(clean):
        target, anchor, alias = split_target(raw)
        if target:
            links.append({"target": target, "target_key": link_key(target), "kind": "embed" if bang else "link",
                          "anchor": anchor, "alias": alias})
    for bang, url in MDLINK_RE.findall(clean):
        if EXTERNAL_RE.match(url) or url.startswith("#"):
            continue
        target, anchor, _ = split_target(unquote(url))
        if target:
            links.append({"target": target, "target_key": link_key(target), "kind": "embed" if bang else "markdown",
                          "anchor": anchor, "alias": None})
    return Parsed(title=title_for(path), frontmatter=fm, tags=sorted(tags, key=str.lower),
                  aliases=aliases, links=links)


def parse_canvas(path: str, text: str) -> Parsed:
    """JSON Canvas: file nodes become embeds; text nodes are parsed as markdown."""
    import json
    p = Parsed(title=title_for(path))
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return p
    for node in data.get("nodes", []) if isinstance(data, dict) else []:
        if node.get("type") == "file" and node.get("file"):
            p.links.append({"target": node["file"], "target_key": link_key(node["file"]), "kind": "embed",
                            "anchor": None, "alias": None})
        elif node.get("type") == "text" and node.get("text"):
            sub = parse_note(path, node["text"])
            p.links += sub.links
            p.tags = sorted(set(p.tags) | set(sub.tags), key=str.lower)
    return p


# =========================================================================== render
# A resolver maps a link target to (href, exists, kind) where kind is note|attachment|missing.
Resolver = Callable[[str], tuple[str, bool, str]]


def _wikilink_rule(state, silent: bool) -> bool:
    src, pos = state.src, state.pos
    embed = src.startswith("![[", pos)
    if not embed and not src.startswith("[[", pos):
        return False
    start = pos + (3 if embed else 2)
    end = src.find("]]", start)
    if end < 0 or "\n" in src[start:end] or "[[" in src[start:end]:
        return False
    if not silent:
        tok = state.push("wikilink", "", 0)
        tok.meta = {"raw": src[start:end], "embed": embed}
    state.pos = end + 2
    return True


def _tag_rule(state, silent: bool) -> bool:
    src, pos = state.src, state.pos
    if src[pos] != "#" or (pos > 0 and not src[pos - 1].isspace() and src[pos - 1] not in "([{"):
        return False
    m = TAG_RE.match(src, pos)
    if not m:
        return False
    if not silent:
        tok = state.push("cove_tag", "", 0)
        tok.content = m.group(1)
    state.pos = m.end()
    return True


def _highlight_rule(state, silent: bool) -> bool:
    src, pos = state.src, state.pos
    if not src.startswith("==", pos) or src.startswith("===", pos):
        return False
    end = src.find("==", pos + 2)
    if end < 0 or end == pos + 2 or "\n" in src[pos + 2:end]:
        return False
    if not silent:
        state.push("mark_open", "mark", 1)
        t = state.push("text", "", 0)
        t.content = src[pos + 2:end]
        state.push("mark_close", "mark", -1)
    state.pos = end + 2
    return True


def _callout_rule(state) -> None:
    """Obsidian callouts: '> [!note] Title' blockquotes become <div class="callout">."""
    tokens = state.tokens
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.type == "blockquote_open" and i + 2 < len(tokens) and tokens[i + 1].type == "paragraph_open" \
                and tokens[i + 2].type == "inline":
            m = re.match(r"\[!([\w-]+)\]([+-]?)[ \t]*([^\n]*)\n?", tokens[i + 2].content)
            if m:
                kind = m.group(1).lower()
                title = m.group(3).strip() or kind.capitalize()
                t.tag = "div"
                t.attrSet("class", f"callout callout-{kind}")
                depth = 0
                for j in range(i + 1, len(tokens)):
                    if tokens[j].type == "blockquote_open":
                        depth += 1
                    elif tokens[j].type == "blockquote_close":
                        if depth == 0:
                            tokens[j].tag = "div"
                            break
                        depth -= 1
                tokens[i + 2].content = tokens[i + 2].content[m.end():]
                head = Token("html_block", "", 0)
                head.content = f'<div class="callout-title">{html.escape(title)}</div>\n'
                tokens.insert(i + 1, head)
        i += 1


def _render_wikilink(self, tokens, idx, options, env) -> str:
    raw = tokens[idx].meta["raw"]
    target, anchor, alias = split_target(raw)
    resolve: Resolver = env.get("resolve")
    href, exists, kind = resolve(target) if resolve else ("#", False, "missing")
    frag = f"#{quote(anchor)}" if anchor and kind == "note" else ""
    label = alias or (f"{target} › {anchor}" if anchor and target else (anchor or target))
    if tokens[idx].meta["embed"]:
        ext = target.rsplit(".", 1)[-1].lower() if "." in target else ""
        if kind == "attachment" and ext in IMAGE_EXT:
            width = f' width="{int(alias)}"' if alias and alias.isdigit() else ""
            return f'<img src="{html.escape(href)}" alt="{html.escape(target)}"{width}>'
        cls = "embed" + ("" if exists else " unresolved")
        return f'<a class="{cls}" href="{html.escape(href + frag)}">⧉ {html.escape(label)}</a>'
    cls = "internal-link" + ("" if exists else " unresolved")
    return f'<a class="{cls}" href="{html.escape(href + frag)}">{html.escape(label)}</a>'


def _render_tag(self, tokens, idx, options, env) -> str:
    tag = tokens[idx].content
    return f'<a class="tag" href="/notes?tag={quote(tag)}">#{html.escape(tag)}</a>'


def _build_md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": False, "breaks": False})
    md.enable(["table", "strikethrough"])
    md.use(front_matter_plugin).use(footnote_plugin).use(tasklists_plugin)
    md.inline.ruler.before("link", "wikilink", _wikilink_rule)
    md.inline.ruler.before("emphasis", "cove_highlight", _highlight_rule)
    md.inline.ruler.push("cove_tag", _tag_rule)
    md.core.ruler.after("block", "callout", _callout_rule)
    md.add_render_rule("wikilink", _render_wikilink)
    md.add_render_rule("cove_tag", _render_tag)

    # Standard markdown links/images that point inside the vault ("pic.png", "Other note.md")
    # are resolved like wikilinks; external URLs are left alone.
    default_image = md.renderer.rules.get("image")
    default_link_open = md.renderer.rules.get("link_open")

    def _vault_url(url: str, env) -> str | None:
        if not url or EXTERNAL_RE.match(url) or url.startswith(("#", "/")):
            return None
        resolve: Resolver = env.get("resolve")
        if not resolve:
            return None
        target, anchor, _ = split_target(unquote(url))
        href, _, kind = resolve(target)
        return href + (f"#{quote(anchor)}" if anchor and kind == "note" else "")

    def _image(self, tokens, idx, options, env):
        new = _vault_url(tokens[idx].attrGet("src") or "", env)
        if new:
            tokens[idx].attrSet("src", new)
        return default_image(tokens, idx, options, env) if default_image else self.image(tokens, idx, options, env)

    def _link_open(self, tokens, idx, options, env):
        new = _vault_url(tokens[idx].attrGet("href") or "", env)
        if new:
            tokens[idx].attrSet("href", new)
            tokens[idx].attrSet("class", "internal-link")
        return (default_link_open(tokens, idx, options, env) if default_link_open
                else self.renderToken(tokens, idx, options, env))

    md.add_render_rule("image", _image)
    md.add_render_rule("link_open", _link_open)
    return md


_MD = _build_md()

ALLOWED_TAGS = set(nh3.ALLOWED_TAGS) | {"input", "section", "mark", "details", "summary", "figure", "figcaption",
                                         "div", "span", "img", "sup", "sub", "kbd", "hr", "table", "thead", "tbody",
                                         "tr", "th", "td", "del", "s"}
ALLOWED_ATTRS = {
    "*": {"class", "id", "title", "lang", "dir"},
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "input": {"type", "checked", "disabled"},
    "th": {"align", "colspan", "rowspan"}, "td": {"align", "colspan", "rowspan"},
    "ol": {"start"}, "li": {"value"},
}


def render_markdown(text: str, resolve: Resolver | None = None) -> str:
    """Render a note body (frontmatter is skipped) to sanitized HTML."""
    raw = _MD.render(text or "", {"resolve": resolve})
    return nh3.clean(raw, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
                     url_schemes={"http", "https", "mailto", "tel", "obsidian"},
                     link_rel="noopener noreferrer")
