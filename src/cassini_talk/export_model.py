# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Markdown bytes, safe names and D8 encode choice; no GTK or audio IO."""

import re
import unicodedata
from contextlib import nullcontext

from . import ux_formats as fmt
from .i18n import using_language
from .meeting_text import reading_turns
from .portable import Word
from .strings import text
from .transcript import row_text, speaker_list

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
    items = value["items"]
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
    words = [
        Word(
            item["speaker"],
            item["startMs"],
            item.get("endMs", item["startMs"]),
            item["text"],
            item.get("lowConfidenceSpeaker", False),
            text_is_stored=value.get("verbatim", False),
            ends_bounded_by_audio=value.get("endsBoundedByAudio", False),
        )
        for item in items
    ]

    def name(identifier):
        return names.get(identifier, text("common.speaker.unnamed", n=order.index(identifier) + 1))

    for row in reading_turns(words, gaps=value.get("turnGaps")):
        over = speaker_list(name(identifier) for identifier in row.over)
        caption = f" (over {escaped(over)})" if over else ""
        lines += [
            f"**{escaped(name(row.speaker))}** · {fmt.position(row.start_ms)}{caption}",
            # Text is already stored or legacy-paired by the shared projection.
            re.sub(r"([\\`*_{}\[\]<>#|~])", r"\\\1", row_text(row, name)[0])
            if value.get("verbatim")
            else escaped(row_text(row, name)[0]),
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
    items = (
        [
            dict(
                speaker=w.speaker,
                startMs=w.start_ms,
                endMs=w.end_ms,
                text=w.text,
                lowConfidenceSpeaker=w.low_confidence,
            )
            for w in meeting.words
        ]
        if meeting.transcript == "done"
        else []
    )
    exported = {item["speaker"] for item in items}
    return dict(
        title=meeting.title,
        startMs=meeting.started_at_ms,
        tz=tz,
        durationMs=meeting.duration_ms,
        # D65: speakers listed are only those in the exported words.
        speakers=[dict(id=s.id, name=s.name) for s in meeting.speakers if s.id in exported],
        items=items,
        notes=list(meeting.notes),
        endsBoundedByAudio=bool(meeting.words) and all(w.ends_bounded_by_audio for w in meeting.words),
        turnGaps={speaker: [list(r) for r in ranges] for speaker, ranges in meeting.turn_gaps.items()},
        **({"verbatim": True} if meeting.words and all(w.text_is_stored for w in meeting.words) else {}),
    )
