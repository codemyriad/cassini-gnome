# SPDX-License-Identifier: GPL-3.0-or-later
"""D52/P8 half-open source placement; absence of events means unknown."""

import math
from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction

from .session_events import read_events


def windows(kind, events):
    me = kind in ("microphone", "local")
    selected, source, begin = True, None, 0
    result = []

    def allowed():
        return selected and (me or source is None or source == ("system" if kind == "system" else "browser"))

    def append(left, right):
        if result and result[-1][1] == left:
            result[-1] = (result[-1][0], right)
        else:
            result.append((left, right))

    for event in read_events(events):
        if event["type"] not in ("selection", "othersSource"):
            continue
        at = event["atMs"]
        if allowed() and at > begin:
            append(begin, at)
        begin = at
        if event["type"] == "selection":
            selected = event["me" if me else "others"]
        elif not me:
            source = event["source"]
    if allowed():
        append(begin, None)
    return None if result == [(0, None)] else tuple(result)


def union(*sets):
    """Merged half-open windows covering any of `sets`; a None end is open."""
    result = []
    for left, right in sorted((window for windows in sets for window in windows), key=lambda window: window[0]):
        if result and (result[-1][1] is None or left <= result[-1][1]):
            if result[-1][1] is not None and (right is None or right > result[-1][1]):
                result[-1] = (result[-1][0], right)
        else:
            result.append((left, right))
    return tuple(result)


class FrameMask:
    """Convert absolute half-open windows once, then mask bounded PCM blocks."""

    def __init__(self, intervals, rate, offset=0):
        self.ranges = None
        self.ends = ()
        if intervals is None:
            return
        if not isinstance(intervals, (list, tuple)) or len(intervals) > 1001:
            raise ValueError("Invalid source ownership windows")
        offset = Fraction(str(offset))
        self.ranges = []
        previous = 0
        for interval in intervals:
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise ValueError("Invalid source ownership window")
            left, right = interval
            if (
                type(left) not in (int, float)
                or not math.isfinite(left)
                or left < previous
                or right is not None
                and (type(right) not in (int, float) or not math.isfinite(right) or right <= left)
                or previous == math.inf
            ):
                raise ValueError("Invalid source ownership window")
            first = math.ceil((Fraction(str(left)) - offset) * rate / 1000)
            end = math.ceil((Fraction(str(right)) - offset) * rate / 1000) if right is not None else None
            self.ranges.append((first, end))
            previous = right if right is not None else math.inf
        self.ends = tuple(end if end is not None else math.inf for _begin, end in self.ranges)

    def sections(self, first, count):
        if self.ranges is None:
            return [(0, count)]
        result = []
        for index in range(bisect_right(self.ends, first), len(self.ranges)):
            begin, end = self.ranges[index]
            if begin >= first + count:
                break
            left = max(first, begin)
            right = first + count if end is None else min(first + count, end)
            if right > left:
                result.append((left - first, right - first))
        return result

    def pcm(self, data, first, width):
        count = len(data) // width
        sections = self.sections(first, count)
        if sections == [(0, count)]:
            return data
        output = bytearray(len(data))
        for left, right in sections:
            output[left * width : right * width] = data[left * width : right * width]
        return bytes(output)


@dataclass(frozen=True)
class Presence:
    """D52.2 half-open session-ms windows; unknown is neither present nor absent."""

    present: tuple
    unknown: tuple
    unpositioned_frames: int
    complete: bool


def _subtract(windows, left, right):
    result = []
    for a, b in windows:
        if right <= a or b <= left:
            result.append((a, b))
            continue
        if a < left:
            result.append((a, left))
        if right < b:
            result.append((right, b))
    return result


def _owned(windows, ownership):
    if ownership is None:
        return tuple((a, b) for a, b in windows if a < b)
    result = []
    for a, b in windows:
        for left, right in ownership:
            low = max(a, Fraction(str(left)))
            high = b if right is None else min(b, Fraction(str(right)))
            if low < high:
                result.append((low, high))
    return tuple(result)


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return Fraction(str(value))


def presence(track, ownership=None):
    """Span extents minus stored gap ranges, intersected with ownership windows.

    Never reads PCM: zero samples are not absence. A droppedFrames count has
    no position and is only reported. Without a ``gaps`` list (older writers),
    after an overflow marker, or from a malformed entry on, the time is unknown.
    Works on session.json tracks and on the published file's x["codemyriad.io"]
    tracks, whose offsets live only on their spans. Returns None when no span
    has a known start offset.
    """
    from .source_spans import bounds

    # Span rows inherit the logical track's fields; a span offset wins. A span
    # without an offset cannot be placed (display=False never assumes zero).
    logical = {key: value for key, value in track.items() if key != "spans"}
    rows = [dict(logical, **span) for span in track.get("spans") or [{}] if isinstance(span, dict)]
    placed = sorted(extent for row in rows if (extent := bounds(row)) is not None)
    if not placed:
        return None
    # A track rotated across files is one logical extent; empty spans add nothing.
    extents = []
    for a, b in placed:
        if a >= b:
            continue
        if extents and a <= extents[-1][1]:
            extents[-1] = (extents[-1][0], max(extents[-1][1], b))
        else:
            extents.append((a, b))
    gaps = track.get("gaps")
    dropped = track.get("droppedFrames")
    dropped = dropped if type(dropped) is int and dropped > 0 else 0
    if not isinstance(gaps, list):
        return Presence((), _owned(extents, ownership), dropped, False)
    present, unknown, complete = list(extents), [], True
    for gap in gaps:
        at = _time(gap.get("atMs")) if isinstance(gap, dict) else None
        duration = _time(gap.get("durationMs")) if isinstance(gap, dict) else None
        if at is not None and duration and gap.get("kind") != "overflow":
            present = _subtract(present, at, at + duration)
            continue
        # Overflow, or an entry this reader cannot place: unknown from its
        # start, or the whole track when even that is unusable.
        at = min((a for a, _ in extents), default=Fraction(0)) if at is None else at
        unknown = [(max(a, at), b) for a, b in present if b > at]
        present = [(a, min(b, at)) for a, b in present if a < at]
        complete = False
        break
    return Presence(_owned(present, ownership), _owned(unknown, ownership), dropped, complete)


# D52.2: absorbs floating-point noise in stored times before taking the next sample.
EPSILON = Fraction(1, 1_000_000)


def samples(windows, rate=16000):
    """Half-open ms windows as [ceil(a*rate/1000 - eps), ceil(b*rate/1000 - eps)) sample ranges."""
    return [[math.ceil(Fraction(edge) * rate / 1000 - EPSILON) for edge in window] for window in windows]


def gap_ranges(gaps):
    """D63 #2: one track's stored gap ranges as 16 kHz session [G0, G1) samples.

    A prefix counts. An empty gap is no range. From an overflow marker (or an
    entry this reader cannot place) on, the time is unknown, so nothing there is
    a gap range; without a ``gaps`` list there are none.
    """
    result = []
    for gap in gaps if isinstance(gaps, list) else ():
        at = _time(gap.get("atMs")) if isinstance(gap, dict) else None
        duration = _time(gap.get("durationMs")) if isinstance(gap, dict) else None
        if at is None or duration is None or gap.get("kind") == "overflow":
            break
        result.extend((left, right) for left, right in samples([(at, at + duration)]) if left < right)
    return result


def speaker_gap_ranges(tracks, *, silent=(), merges=None, speakers=()):
    """D63 #2: {speaker id: sorted gap ranges of that speaker's own tracks}.

    `tracks` are stored track rows (session.json, or x["codemyriad.io"] with
    offsets on the spans). Speaker ids and R64 overlap lanes are the ones the
    write-down gives (transcription.prepare_tracks); a D56 silent track has no
    words and adds no ranges. `merges` maps a merged-away speaker to the one
    its words now carry, which then splits on both speakers' ranges. Every
    track and lane id has an entry, an empty one too, so a source id that ends
    in `~1` never takes another source's ranges as a voice (own_gap_ranges).
    A voice in `speakers` whose group is a merge target keeps its group's own
    ranges, not the merged speaker's.
    """
    from .source_spans import separate

    rows = []
    for track in tracks:
        identity = track.get("sourceTrackId") or track.get("id")
        if track.get("kind") in ("microphone", "local"):
            speaker = "me"
        elif track.get("kind") == "system":
            speaker = "others"
        else:
            speaker = f"track-{identity}"
        rows.append(dict(track, id=track.get("id"), captureTrackId=track.get("id"), speaker=speaker))
    # A row this reader cannot place gives no ranges; its source's other rows keep theirs.
    audible = [row for row in rows if _placeable(row) and row.get("frames") and row["id"] not in silent]
    reserved = [row["speaker"] for row in rows]
    sources = {}
    for row in audible:
        sources.setdefault(row["speaker"], []).append(row)
    result = {speaker: [] for speaker in reserved}
    for group in sources.values():
        # R64 lanes are per source, so one at a time gives the same ids; a
        # source whose lane id is over the limit loses only its own breaks.
        try:
            separated, _diagnostics = separate(group, reserved=reserved)
        except ValueError:
            continue
        for row in separated:
            result.setdefault(row["speaker"], []).extend(gap_ranges(row.get("gaps")))
    # Merges read the unmerged map, so their order changes nothing. A
    # `<group>~n` target keeps its group's ranges as well.
    merged = {}
    for source, target in (merges or {}).items():
        merged.setdefault(target, [*own_gap_ranges(result, target)]).extend(own_gap_ranges(result, source))
    for speaker in speakers:
        if isinstance(speaker, str) and speaker not in result and _voice_group(speaker) in merged:
            merged.setdefault(speaker, own_gap_ranges(result, speaker))
    result.update(merged)
    return {speaker: _union(ranges) for speaker, ranges in result.items()}


def _count(value, low):
    return type(value) is int and low <= value <= 2**63


def _placeable(row):
    """Whether source_spans.bounds and separate can read a stored row; a foreign file's may not."""
    from .source_spans import bounds

    spans = row.get("spans")
    if (
        not isinstance(row.get("id"), str)
        or not isinstance(row.get("sourceTrackId") or "", str)
        or not _count(row.get("frames"), 0)
        or spans is not None
        and not isinstance(spans, (list, tuple))
    ):
        return False
    for span in spans or [row]:
        if not isinstance(span, dict) or not _count(span.get("sampleRate"), 1) or not _count(span.get("frames"), 0):
            return False
        at = span.get("offsetMs", span.get("startOffsetMs"))
        if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
            return False
    # R64 sorts by UTF-8 ids and extents: collapsed clock anchors divide by
    # zero, a huge offset goes NaN, a lone surrogate does not encode.
    try:
        row["id"].encode("utf-8")
        row["speaker"].encode("utf-8")
        bounds(row)
    except (ArithmeticError, ValueError):
        return False
    return True


def _union(ranges):
    # Sorted ranges that do not overlap; turn_break bisects them. Joining
    # overlapping ones changes no break (touching ones stay apart: two equal
    # midpoints on the shared edge are in neither).
    result = []
    for left, right in sorted(ranges):
        if result and left < result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], right))
        else:
            result.append((left, right))
    return tuple(result)


def own_gap_ranges(ranges, speaker):
    """The gap ranges of `speaker`; a D24 `<group>~n` voice with no entry of its own has its group's."""
    if speaker in ranges:
        return ranges[speaker]
    group = _voice_group(speaker)
    return () if group is None else ranges.get(group, ())


def _voice_group(speaker):
    group, separator, ordinal = speaker.rpartition("~")
    return group if separator and ordinal.isdigit() else None


def turn_break(ranges, first, second):
    """D63 #2: whether a gap range lies between two words' midpoints.

    Words are (startMs, endMs) in session ms; a midpoint is 8 × (start + end)
    in 16 kHz session samples, so it is exact. `ranges` are sorted and never
    overlap (speaker_gap_ranges), so the first one ending after the lower
    midpoint decides.
    """
    low, high = sorted((8 * (first[0] + first[1]), 8 * (second[0] + second[1])))
    index = bisect_right(ranges, low, key=lambda value: value[1])
    return index < len(ranges) and ranges[index][0] < high
