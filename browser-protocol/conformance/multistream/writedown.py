"""Independent D63 #2 and #3 oracle: word keep/drop at write-down and turn breaks. Never production code.

A word is [startMs, endMs] in integer session milliseconds. Its midpoint, in 16 kHz
session samples, is M = 8 * (startMs + endMs): exact, so no rounding is involved.

keep() (D63 #3): a word is written only when M lies in one of its track's
present-or-unknown ranges [a, b) from gaps.presence with that track's ownership.
A track without `gaps` is unknown and keeps its words inside its span extent and
ownership; time after an overflow marker is unknown too. The extent starts at the D55
placement, frame(startOffsetMs, 16000), not at the raw offset (R88). Compare in session
samples: subtracting a fractional startOffsetMs before rounding moves the edges. This is a write-time rule on every path, not a display filter.

breaks() and turns() (D63 #2): the gap ranges of a speaker are the non-overflow
gap ranges of that speaker's tracks, as 16 kHz samples [G0, G1) with the presence
conversion. Two consecutive words of one speaker never share a turn when a gap
range lies between their midpoints: G0 < G1, G0 < max(M1, M2) and min(M1, M2) < G1.
An empty gap (durationMs 0) is no range: it splits no turn and drops no word. Pauses
inside presence, rejoins and non-owned time are not gap ranges, so they still join.
"""
from fractions import Fraction
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('gaps', Path(__file__).resolve().parents[1] / 'gaps/gaps.py')
gaps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gaps)


def midpoint(word):
    start, end = word
    assert type(start) is int and type(end) is int and 0 <= start <= end, word
    return 8 * (start + end)


def keep(track, word, ownership=None):
    """track: stored session.json facts (startOffsetMs, sampleRate, frames, optional gaps)."""
    found = gaps.presence(track, ownership)
    m = midpoint(word)
    return any(a <= m < b for a, b in found['present'] + found['unknown'])


def breaks(tracks):
    """16 kHz [G0, G1) gap ranges of one speaker's stored tracks."""
    result = []
    for track in tracks:
        for gap in track.get('gaps') or []:
            if gap['kind'] == 'overflow':
                break
            at = Fraction(str(gap['atMs']))
            result.append(gaps._samples(at, at + Fraction(str(gap['durationMs']))))
    return sorted(result)


def split(ranges, first, second):
    m1, m2 = midpoint(first), midpoint(second)
    low, high = min(m1, m2), max(m1, m2)
    return any(g0 < g1 and g0 < high and low < g1 for g0, g1 in ranges)


def turns(ranges, words):
    """Turn sizes for consecutive words of one speaker, in file order."""
    sizes = []
    for index, word in enumerate(words):
        if sizes and not split(ranges, words[index - 1], word):
            sizes[-1] += 1
        else:
            sizes.append(1)
    return sizes
