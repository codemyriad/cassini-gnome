# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline HTML projection of D84 ReadingRows; no second turn algorithm.

DOM model follows gocassini MeetingView/TranscriptWords: article, header,
one paragraph, inline interjection spans and independently seekable buttons.
"""

from html import escape
from pathlib import Path

from .meeting_text import row_caption
from .transcript import format_time, join_items


def row_html(turn, speaker_name):
    def word(owner, index):
        atom = owner.words[index]
        name = speaker_name(owner.speaker)
        caption = f"{owner.items[index]} — seek to {format_time(atom.start_ms)}, {name}"
        return (
            f'<button type="button" class="cassini-word" id="w{owner.word_indices[index]}" '
            f'data-start="{atom.start_ms}" data-end="{atom.end_ms}" '
            f'aria-label="{escape(caption, quote=True)}">{escape(owner.items[index])}</button>'
        )

    def chip(aside):
        name = speaker_name(aside.speaker)
        words = " ".join(word(aside, i) for i, value in enumerate(aside.items) if value)
        warning = " crosstalk" if aside.crosstalk else ""
        return (
            f'<span class="mv-interjection{warning}">(<b>{escape(name)}</b>: {words})</span>',
            f"({name}: {aside.text})",
        )

    prose = join_items(turn, word, chip)[0]
    name = "" if turn.continuation else f'<span class="mv-speaker-name">{escape(speaker_name(turn.speaker))}</span> '
    caption = escape(row_caption(turn, speaker_name))
    return (
        f'<article id="r{turn.index}" data-row="{turn.index}"><header>{name}'
        f'<button type="button" class="mv-turn-time" data-start="{turn.start_ms}">'
        f'{format_time(turn.start_ms)}</button> <span class="caption">{caption}</span></header>'
        f"<p>{prose}</p></article>"
    )


def document(rows, speaker_name, *, dark=False, plain=False):
    # Transcript data enters only escaped text/attributes; scripts and styles
    # are application-owned. No URL, font, image or remote dependency exists.
    assets = Path(__file__).parent
    css = (assets / "webkit_transcript.css").read_text()
    script = (assets / "webkit_transcript.js").read_text()
    body = "".join(row_html(row, speaker_name) for row in rows)
    mode = ("dark " if dark else "") + ("plain" if plain else "")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; '
        "script-src &#39;unsafe-inline&#39;; style-src &#39;unsafe-inline&#39;; "
        "connect-src &#39;none&#39;; img-src &#39;none&#39;; font-src &#39;none&#39;; "
        "media-src &#39;none&#39;; frame-src &#39;none&#39;; object-src &#39;none&#39;; "
        'base-uri &#39;none&#39;; form-action &#39;none&#39;">'
        f'<style>{css}</style></head><body class="{mode}">'
        f'<main aria-label="Transcript">{body}</main><script>{script}</script></body></html>'
    )
