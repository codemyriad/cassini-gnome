# summary.py
#
# Copyright 2026 Silvio Tomatis
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A small Markdown reader for the summary.md attachment.

Summaries are short LLM-written Markdown: headings, paragraphs, bullet and
task lists, emphasis and inline code. This turns them into blocks the window
renders with native widgets. Timestamps like 1:38 or [01:02:03] become
links that seek the player. Anything fancier degrades to plain text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Only bracketed times link: "(1:38)" or "[01:02:03]". A bare "4:15" is as
# likely a time of day as a position in the recording.
TIMESTAMP = re.compile(r"\s?[\[(]((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[( |x|X)\]\s+)?(.*)$")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(slots=True)
class Block:
    kind: str  # "heading", "paragraph", "item"
    markup: str  # Pango markup
    level: int = 0  # heading level
    checked: bool | None = None  # task-list state for items
    seek_ms: int | None = None  # first timestamp in the block


def parse_timestamp(text: str) -> int | None:
    parts = [int(p) for p in text.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    else:
        hours, minutes, seconds = parts
    if seconds >= 60 or (hours and minutes >= 60):
        return None
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def inline_markup(text: str, links: bool = True) -> tuple[str, int | None]:
    """Escape *text*, apply emphasis, and turn timestamps into seek links.

    With *links* false the timestamps are left out of the markup instead,
    for rows that show the time on their own.
    """
    first: list[int] = []
    out = []
    pos = 0
    for match in TIMESTAMP.finditer(text):
        ms = parse_timestamp(match.group(1))
        if ms is None:
            continue
        out.append(_emphasis(escape(text[pos : match.start()])))
        if links:
            stamp = match.group(0)
            pre, _, post = stamp.partition(match.group(1))
            out.append(f'{escape(pre)}<a href="seek:{ms}">{escape(match.group(1))}</a>{escape(post)}')
        first.append(ms)
        pos = match.end()
    out.append(_emphasis(escape(text[pos:])))
    return "".join(out), (first[0] if first else None)


def _emphasis(escaped: str) -> str:
    escaped = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", escaped)
    escaped = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<tt>\1</tt>", escaped)
    return escaped


def parse(markdown: str) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush():
        if paragraph:
            markup, ms = inline_markup(" ".join(paragraph))
            blocks.append(Block("paragraph", markup, seek_ms=ms))
            paragraph.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or re.fullmatch(r"[-*_]{3,}", stripped):
            flush()
            continue
        if heading := _HEADING.match(stripped):
            flush()
            markup, ms = inline_markup(heading.group(2))
            blocks.append(Block("heading", markup, level=len(heading.group(1)), seek_ms=ms))
            continue
        if bullet := _BULLET.match(line):
            flush()
            box, text = bullet.groups()
            markup, ms = inline_markup(text, links=False)
            checked = None if box is None else box.lower() == "x"
            blocks.append(Block("item", markup, checked=checked, seek_ms=ms))
            continue
        if blocks and blocks[-1].kind == "item" and not paragraph and line.startswith((" ", "\t")):
            markup, ms = inline_markup(stripped, links=False)
            if blocks[-1].seek_ms is None:
                blocks[-1].seek_ms = ms
            blocks[-1].markup += " " + markup
            continue
        paragraph.append(stripped)
    flush()
    return blocks
