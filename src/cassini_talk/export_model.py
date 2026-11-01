# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Markdown bytes, safe names and D8 encode choice; no GTK or audio IO."""

import re
import unicodedata
from contextlib import nullcontext

from . import ux_formats as fmt
from .i18n import using_language
from .strings import text
from .transcript import turn_text

BANDS = ((4000, 12000, 16000), (6000, 12000, 20000), (8000, 16000, 32000), (12000, 20000, 48000), (20000, 24000, 64000))


def encode_choice(sources):
    widths = [
        s["sampleRate"] // 2
        if s.get("codec") != "opus" and type(s.get("sampleRate")) is int and s["sampleRate"] > 0
        else 20000
        for s in sources
    ]
    bandwidth = max(widths, default=20000)
    cutoff, floor, ceiling = next((band for band in BANDS if band[0] >= bandwidth), BANDS[-1])
    active = [s for s in sources if not s.get("empty")]
    speech = (
        max(s["speechBitrateBps"] for s in active)
        if active and all(type(s.get("speechBitrateBps")) is int and s["speechBitrateBps"] > 0 for s in active)
        else None
    )
    bitrate = max(floor, min(ceiling, (speech * 4 + 2999) // 3000 * 1000)) if speech else ceiling
    return dict(
        policy="match-source",
        bitrateBps=bitrate,
        cutoffHz=cutoff,
        application="audio",
        sourceBandwidthHz=bandwidth,
        sourceSpeechBitrateBps=speech,
    )


def file_stem(title):
    safe = "".join("-" if c in "/\\" or unicodedata.category(c) == "Cc" else c for c in title).strip()[:120]
    safe = safe or "Untitled meeting"
    while len(safe.encode("utf-8")) > 230:
        safe = safe[:-1]
    return safe


def escaped(value):
    value = " ".join(value.split())
    value = re.sub(r"([\\`*_\[\]<>])", r"\\\1", value)
    value = re.sub(r"^([#\-+])", r"\\\1", value)
    return re.sub(r"^(\d+)\.", r"\1\\.", value)


def markdown(value):
    """S-5: value is the shared, already-resolved Library model projection."""
    with using_language(value["uiLanguage"]) if "uiLanguage" in value else nullcontext():
        return _markdown(value)


def _markdown(value):
    items = sorted(value["items"], key=lambda item: item["startMs"])
    names = {s["id"]: s["name"] for s in value["speakers"]}
    order = list(dict.fromkeys(item["speaker"] for item in items))
    lines = ["# " + escaped(value["title"]), ""]
    stamp = value.get("startMs")
    date_line = ""
    if stamp is not None:
        date_line = fmt.full_date_time(stamp, tz=value.get("tz"))
    if value.get("durationMs") is not None:
        date_line += (" · " if date_line else "") + fmt.meeting_duration(value["durationMs"])
    if date_line:
        lines += [date_line, ""]
    if order:
        lines += [
            text(
                "export.markdown.speakersLine",
                names=", ".join(
                    escaped(names.get(identifier, text("common.speaker.unnamed", n=i + 1)))
                    for i, identifier in enumerate(order)
                ),
            ),
            "",
        ]
    lines += ["## " + text("export.markdown.transcriptHeading"), ""]
    turns = []
    for item in items:
        if turns and turns[-1][0] == item["speaker"]:
            turns[-1][2].append(item["text"])
        else:
            turns.append([item["speaker"], item["startMs"], [item["text"]]])
    for identifier, start, texts in turns:
        name = names.get(identifier, text("common.speaker.unnamed", n=order.index(identifier) + 1))
        lines += [
            f"**{escaped(name)}** · {fmt.position(start)}",
            escaped(" ".join(texts) if value.get("verbatim") else turn_text(texts)),
            "",
        ]
    if value.get("notes"):
        lines += ["## " + text("export.markdown.notesHeading"), ""]
        lines += [
            f"- {fmt.position(note['atMs'])} {escaped(note['text'])}"
            for note in sorted(value["notes"], key=lambda n: n["atMs"])
        ]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def model_projection(meeting, *, tz=None):
    return dict(
        title=meeting.title,
        startMs=meeting.started_at_ms,
        tz=tz,
        durationMs=meeting.duration_ms,
        speakers=[dict(id=s.id, name=s.name) for s in meeting.speakers],
        items=[dict(speaker=w.speaker, startMs=w.start_ms, endMs=w.end_ms, text=w.text) for w in meeting.words]
        if meeting.transcript == "done"
        else [],
        notes=list(meeting.notes),
        **({"verbatim": True} if meeting.words and all(w.text_is_stored for w in meeting.words) else {}),
    )
