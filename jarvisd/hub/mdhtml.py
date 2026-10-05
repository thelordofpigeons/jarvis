"""Markdown to HTML for the one dialect the digest uses, and nothing else.

Why not a library: the digest renderer (jarvisd/render.py) emits headings, bullets, numbered
lines, paragraphs, **bold** and `code`. Supporting that subset is forty lines, and a subset
cannot grow a way to emit a link, an image or raw HTML. Every character is escaped before any
markup is added, so text from a note can never become a tag.

Layer L3 (hub). No imports from other jarvisd modules.
"""
from __future__ import annotations

import html
import re

_HEADING = re.compile(r"^(#{1,4}) +(.*\S)\s*$")
_BULLET = re.compile(r"^\s*[-*] +(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)] +(.*)$")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


def inline(text: str) -> str:
    """Escape first, then add the only two inline marks."""
    safe = html.escape(text, quote=True)
    safe = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", safe)
    return _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", safe)


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """(flat key: value pairs, body). A note without a closing fence has no front matter."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            meta: dict[str, str] = {}
            for line in lines[1:end]:
                key, sep, value = line.partition(":")
                if sep and key.strip():
                    meta[key.strip()] = value.strip()
            return meta, "\n".join(lines[end + 1:])
    return {}, text


def render_markdown(text: str) -> str:
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_kind = ""

    def flush_para() -> None:
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if items:
            out.append(f"<{list_kind}>" + "".join(f"<li>{inline(i)}</li>" for i in items) + f"</{list_kind}>")
            items.clear()
        list_kind = ""

    for line in text.splitlines():
        heading, bullet, numbered = _HEADING.match(line), _BULLET.match(line), _NUMBERED.match(line)
        if heading:
            flush_para()
            flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif bullet or numbered:
            flush_para()
            kind = "ul" if bullet else "ol"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            items.append((bullet or numbered).group(1))  # type: ignore[union-attr]
        elif not line.strip():
            flush_para()
            flush_list()
        else:
            flush_list()
            para.append(line.strip())
    flush_para()
    flush_list()
    return "\n".join(out)


def section(body: str, heading: str) -> list[str]:
    """Lines of the `## heading` section (without the heading), or [] when it is absent."""
    lines = body.splitlines()
    want = heading.strip().lower()
    start = None
    for n, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and len(m.group(1)) == 2 and m.group(2).strip().lower() == want:
            start = n + 1
            break
    if start is None:
        return []
    found: list[str] = []
    for line in lines[start:]:
        m = _HEADING.match(line)
        if m and len(m.group(1)) <= 2:
            break
        found.append(line)
    return found
