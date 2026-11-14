# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Markdown bytes, safe names and D8 encode choice; no GTK or audio IO."""

import re
import unicodedata
from contextlib import nullcontext

from . import ux_formats as fmt
from .i18n import language, using_language
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


def _reading(value):
    """The shared reading rows and the resolver for names exactly as shown (D47)."""
    items = value["items"]
    names = {s["id"]: s["name"] for s in value["speakers"]}
    order = list(dict.fromkeys(item["speaker"] for item in sorted(items, key=lambda item: item["startMs"])))
    rank = {identifier: i for i, identifier in enumerate(order)}
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
        return names.get(identifier, text("common.speaker.unnamed", n=rank[identifier] + 1))

    return reading_turns(words, gaps=value.get("turnGaps")) if words else [], name, order


def _markdown(value):
    rows, name, order = _reading(value)
    lines = ["# " + escaped(value["title"]), ""]
    stamp = value.get("startMs")
    if stamp is None:
        stamp = value.get("recordedAtLocal")
    date_line = ""
    if stamp is not None:
        date_line = fmt.full_date_time(stamp, tz=value.get("tz"))
    if value.get("durationMs") is not None:
        date_line += (" · " if date_line else "") + fmt.meeting_duration(value["durationMs"])
    if date_line:
        lines += [date_line, ""]
    if order:
        lines += [
            text("export.markdown.speakersLine", names=", ".join(escaped(name(identifier)) for identifier in order)),
            "",
        ]
    lines += ["## " + text("export.markdown.transcriptHeading"), ""]
    for row in rows:
        over = speaker_list((name(identifier) for identifier in row.over), language=language())
        caption = " " + text("meeting.turn.over", names=escaped(over)) if over else ""
        lines += [
            f"**{escaped(name(row.speaker))}** · {fmt.position(row.start_ms)}{caption}",
            # Text is already stored or legacy-paired by the shared projection.
            escaped(row_text(row, name)[0]),
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


# L-11 (D99 §2, PARITY §2): text formats share the Markdown projection, so
# speaker names and turns are exactly what the viewer shows. Voiceprints and
# notes never appear; times are playback positions from the word timings.
CUE_MAX_MS = 7000  # PARITY §2: a cue covers at most 7 s ...
CUE_LINE = 42  # ... and at most two lines of this many characters.
CUE_PAUSE_MS = 1500  # A silence this long inside a turn starts a new cue.
CUE_MIN_MS = 1000  # Shortest cue on screen unless the speaker's next cue starts first.


def _plain(value):
    """One line of text: control characters (newlines, tabs, NUL) become spaces."""
    return " ".join("".join(" " if unicodedata.category(c)[0] == "C" else c for c in value).split())


def plain_text(value):
    """`.txt`: one block per speaker turn, `{name} · {hh:mm:ss}` then the words."""
    with using_language(value["uiLanguage"]) if "uiLanguage" in value else nullcontext():
        rows, name, _order = _reading(value)
        blocks = [
            f"{_plain(name(row.speaker))} · {fmt.position(row.start_ms)}\n{_plain(row_text(row, name)[0])}\n"
            for row in rows
        ]
    return "\n".join(blocks)


def _cue_lines(value, start=0):
    """One line when it fits, else two split at the most balanced space after `start` (never three)."""
    if len(value) <= CUE_LINE:
        return [value]
    spaces = [i for i in range(start, len(value)) if value[i] == " "]
    if not spaces:
        return [value]
    fitting = [i for i in spaces if i <= CUE_LINE and len(value) - i - 1 <= CUE_LINE]
    if fitting:
        at = min(fitting, key=lambda i: abs(len(value) - 1 - 2 * i))
    else:
        at = next((i for i in reversed(spaces) if i <= CUE_LINE), spaces[0])
    return [value[:at], value[at + 1 :]]


def _spread_tied(turn):
    """Give same-start cues of one turn consecutive, non-empty slices of their shared span.

    Imported timings can put every word of a long turn at the same start (or at zero
    duration); length splitting then yields several cues starting together, and each would
    be hidden by the next. The run's span, at least CUE_MIN_MS per cue and never past the
    turn's next later cue, is divided evenly so they play one after another.
    """
    index = 0
    while index < len(turn):
        start = turn[index]["start"]
        stop = index + 1
        while stop < len(turn) and turn[stop]["start"] == start:
            stop += 1
        count = stop - index
        if count > 1:
            span_end = max(max(d["end"] for d in turn[index:stop]), start + count * CUE_MIN_MS)
            if stop < len(turn) and turn[stop]["start"] > start:
                span_end = min(span_end, turn[stop]["start"])
            span = max(span_end - start, count)
            for offset, draft in enumerate(turn[index:stop]):
                draft["start"] = start + span * offset // count
                draft["end"] = start + span * (offset + 1) // count
        index = stop


def cues(value):
    """Subtitle cues: (speaker name, start ms, end ms, [line, line?], prefix length).

    Every reading row and every inline reply is a turn; a turn is cut into
    segments of at most CUE_MAX_MS and two CUE_LINE lines including the
    `{name}: ` prefix. Linear in the number of words apart from the final
    stable sort by start.
    """
    with using_language(value["uiLanguage"]) if "uiLanguage" in value else nullcontext():
        rows, name, _order = _reading(value)
        drafts = []

        def segment(speaker, atoms):
            label = _plain(name(speaker))
            prefix = len(label) + 2
            current = None
            previous_end = None
            turn = []
            for word, shown in atoms:
                shown = _plain(shown or "")
                if not shown:
                    continue
                if (
                    current is None
                    or word.end_ms - current["start"] > CUE_MAX_MS
                    or word.start_ms - previous_end >= CUE_PAUSE_MS
                    or current["length"] + 1 + len(shown) > 2 * CUE_LINE
                ):
                    current = dict(speaker=speaker, label=label, start=word.start_ms, end=word.end_ms, words=[])
                    current["length"] = prefix + len(shown)
                    turn.append(current)
                else:
                    current["length"] += 1 + len(shown)
                current["words"].append(shown)
                current["end"] = max(current["end"], word.end_ms)
                previous_end = word.end_ms
            _spread_tied(turn)
            drafts.extend(turn)

        for row in rows:
            segment(row.speaker, zip(row.words, row.items, strict=True))
            for _after, aside in row.asides:
                segment(aside.speaker, zip(aside.words, aside.items, strict=True))
    drafts.sort(key=lambda draft: draft["start"])
    # Per speaker: the nearest following cue start and the next start strictly after it, so a
    # cue is only ever capped by a strictly later start and keeps a positive duration.
    following = {}
    result = [None] * len(drafts)
    for index in range(len(drafts) - 1, -1, -1):
        draft = drafts[index]
        start = draft["start"]
        end = min(max(draft["end"], start + CUE_MIN_MS), start + CUE_MAX_MS)
        nearest, beyond = following.get(draft["speaker"], (None, None))
        cap = nearest if nearest is not None and nearest > start else beyond
        if cap is not None:
            end = min(end, cap)
        if nearest != start:
            following[draft["speaker"]] = (start, nearest)
        prefix = draft["label"] + ": "
        result[index] = (
            draft["label"],
            draft["start"],
            end,
            _cue_lines(prefix + " ".join(draft["words"]), len(prefix)),
            len(prefix),
        )
    return result


def _stamp(ms, separator):
    ms = max(0, ms)
    seconds, millis = divmod(ms, 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def srt(value):
    """`.srt`: numbered cues, each line set prefixed `{name}: `."""
    blocks = [
        f"{number}\n{_stamp(start, ',')} --> {_stamp(end, ',')}\n" + "".join(line + "\n" for line in lines)
        for number, (_label, start, end, lines, _prefix) in enumerate(cues(value), start=1)
    ]
    return "\n".join(blocks)


def vtt_escape(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def webvtt(value):
    """`.vtt`: `WEBVTT`, then the same cues as SRT with `<v {name}>` voice spans."""
    out = ["WEBVTT\n"]
    for label, start, end, lines, prefix in cues(value):
        body = "\n".join(lines)[prefix:]  # The name moves from the prefix into the voice span.
        out.append(f"\n{_stamp(start, '.')} --> {_stamp(end, '.')}\n<v {vtt_escape(label)}>{vtt_escape(body)}</v>\n")
    return "".join(out)


TEXT_FORMATS = {"markdown": (markdown, "md"), "txt": (plain_text, "txt"), "srt": (srt, "srt"), "vtt": (webvtt, "vtt")}


def has_completed_transcript(meeting):
    """D12/D99: only a completed transcript has text formats; queued, running, failed is audio-only."""
    return meeting.transcript == "done"


def audio_format(meeting):
    """D99 §2: an imported meeting with no Cassini file yet offers its original audio
    instead of a Cassini file while it is audio-only; everything else is a Cassini file."""
    if (
        meeting.origin == "received"
        and getattr(meeting, "plain_audio", False)
        and meeting.audio_file is not None
        and not has_completed_transcript(meeting)
    ):
        return "original"
    return "cassini"


def available_formats(meeting):
    """The single-meeting choices in dialog order (PARITY §2); text formats are hidden, not disabled."""
    if has_completed_transcript(meeting):
        return (audio_format(meeting), "markdown", "txt", "srt", "vtt")
    return (audio_format(meeting),)


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
        **(
            {"recordedAtLocal": meeting.recorded_at_local}
            if meeting.started_at_ms is None and meeting.recorded_at_local
            else {}
        ),
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
