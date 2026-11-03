"""Independent D52 noise-floor oracle (G9); apps must measure with their own meters.

span(): the 10th percentile of audible 32 ms frame levels inside presence, where
presence is gaps.presence() with ownership, measured on the stored 48 kHz PCM16.
A frame counts only when it lies wholly inside one present range. Its level is
the mean-removed RMS in dBFS; digital silence and frames at or below -90 dBFS are
not audible, so zero padding never lowers a floor. person(): the minimum over
that person's span floors, so a clean span is never hidden by a noisy one.
Never production code, the same rule as ownership.py.
"""
import importlib.util
import math
from pathlib import Path

_spec = importlib.util.spec_from_file_location('gaps', Path(__file__).resolve().parents[1] / 'gaps/gaps.py')
gaps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gaps)

FRAME = 1536  # 32 ms at 48 kHz, the same span as 512 frames at 16 kHz
PERCENTILE = 0.10
AUDIBLE_DBFS = -90.0
FULL_SCALE_DB = 20 * math.log10(32768)


def levels(pcm, ranges):
    """dBFS per frame (track-relative, FRAME-aligned) wholly inside one [first, last) frame range."""
    result = []
    for first, last in ranges:
        for start in range(-(-first // FRAME) * FRAME, last - FRAME + 1, FRAME):
            frame = pcm[start:start + FRAME]
            total = sum(frame)
            energy = FRAME * sum(x * x for x in frame) - total * total
            if energy > 0:
                result.append(10 * math.log10(energy / FRAME ** 2) - FULL_SCALE_DB)
    return result


def span(track, pcm, ownership=None):
    """Floor of one stored span (session.json track facts), or None without audible frames."""
    offset = round(track['startOffsetMs'] * 48)
    ranges = [(3 * a - offset, 3 * b - offset) for a, b in gaps.presence(track, ownership)['present']]
    audible = sorted(level for level in levels(pcm, ranges) if level > AUDIBLE_DBFS)
    return audible[int(PERCENTILE * (len(audible) - 1))] if audible else None


def person(floors):
    """Pooled per-person floor over that person's span floors."""
    known = [floor for floor in floors if floor is not None]
    return min(known) if known else None
