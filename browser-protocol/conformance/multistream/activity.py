"""Independent D63/R71 active-audio and fallback oracle (G10/M13). Never production code.

measure(): R71 active frames of one stored span, on the stored 48 kHz PCM16. Frames are
1536 samples (32 ms, the same span as 512 samples at 16 kHz), track-relative, and a
trailing partial frame never counts. A frame is inside when it lies wholly inside one
present-or-unknown range (gaps.presence with ownership, adjacent ranges merged). Its
level is the mean-removed RMS in dBFS; digital silence has no level. The floor is the
10th percentile (index int(0.1 * (n - 1))) of inside frames above -90 dBFS; with fewer
than FLOOR_MIN_FRAMES of them, -50 dBFS applies alone. A frame is loud when it is inside
and its level is at least max(-50 dBFS, floor + 12 dB) + shift_db. Only runs of at least
MIN_RUN_FRAMES loud frames are active. shift_db moves the threshold, for tolerance bands.

windows(): the fallback windows of one span, laid separately inside each merged presence
range (15 s, 0.5 s overlap, the last one short), kept when they hold at least
MIN_WINDOW_FRAMES active frames, counting partial edge frames.

decide(): D63 #1 on abstract per-track facts. The 15 s and 10-word tests pool every
non-silent track of one speaker id; the gocassini margin stays per track; provenance
keeps one entry per track of a firing speaker, with personActiveSeconds added.
Stdlib only, so /usr/bin/python3 on macOS runs it.
"""
import importlib.util
import math
from fractions import Fraction
from pathlib import Path

_spec = importlib.util.spec_from_file_location('gaps', Path(__file__).resolve().parents[1] / 'gaps/gaps.py')
gaps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gaps)

FRAME = 1536  # 32 ms at 48 kHz
HOLD = 3  # 48 kHz samples per 16 kHz sample
FRAME_16K = FRAME // HOLD
FRAME_MS = 32
FULL_SCALE_DB = 20 * math.log10(32768)
ACTIVE_DBFS = -50.0
FLOOR_MARGIN_DB = 12.0
PERCENTILE = 0.10
AUDIBLE_DBFS = -90.0
FLOOR_MIN_FRAMES = 10000 // FRAME_MS  # 312: "under 10 s audible", counted in whole frames
MIN_RUN_FRAMES = 160 // FRAME_MS  # 5
WINDOW = 15 * 16000  # 16 kHz samples
OVERLAP = 16000 // 2
MIN_WINDOW_FRAMES = -(-500 // FRAME_MS)  # 16
MIN_ACTIVE_MS = 15000
FEW_WORDS = 10


def level(frame):
    total = sum(frame)
    energy = len(frame) * sum(x * x for x in frame) - total * total
    return None if energy <= 0 else 10 * math.log10(energy / len(frame) ** 2) - FULL_SCALE_DB


def levels(pcm):
    return [level(pcm[k * FRAME:(k + 1) * FRAME]) for k in range(len(pcm) // FRAME)]


def ranges16(track, ownership=None):
    """Merged present-or-unknown ranges, track-relative 16 kHz samples, clipped to the span."""
    found = gaps.presence(track, ownership)
    offset = Fraction(str(track['startOffsetMs'])) * 16
    total = track['frames'] * 16000 // track['sampleRate']
    merged = []
    for a, b in sorted(found['present'] + found['unknown']):
        a, b = max(0, math.ceil(a - offset)), min(total, math.floor(b - offset))
        if a >= b:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def inside(track, ownership, count):
    flags = [False] * count
    for a, b in ranges16(track, ownership):
        first, last = -(-a // FRAME_16K), min(count, b // FRAME_16K)
        flags[first:last] = [True] * max(0, last - first)
    return flags


def threshold(values, flags, shift_db=0.0):
    audible = sorted(v for v, i in zip(values, flags) if i and v is not None and v > AUDIBLE_DBFS)
    if len(audible) < FLOOR_MIN_FRAMES:
        return None, ACTIVE_DBFS + shift_db
    floor = audible[int(PERCENTILE * (len(audible) - 1))]
    return floor, max(ACTIVE_DBFS, floor + FLOOR_MARGIN_DB) + shift_db


def runs(loud):
    active, run = [False] * len(loud), 0
    for k, value in enumerate(list(loud) + [False]):
        if value:
            run += 1
            continue
        if run >= MIN_RUN_FRAMES:
            active[k - run:k] = [True] * run
        run = 0
    return active


def measure(track, pcm, ownership=None, shift_db=0.0, values=None):
    """Active frames of one stored span (session.json track facts and its PCM)."""
    values = levels(pcm) if values is None else values
    flags = inside(track, ownership, len(values))
    floor, limit = threshold(values, flags, shift_db)
    active = runs([i and v is not None and v >= limit for v, i in zip(values, flags)])
    return dict(floor=floor, threshold=limit, active=active, frames=sum(active))


def window_bounds(total):
    if total <= 0:
        return []
    if total <= WINDOW:
        return [(0, total)]
    bounds = []
    for start in range(0, total, WINDOW - OVERLAP):
        if start + WINDOW >= total:
            bounds.append((start, total))
            break
        bounds.append((start, start + WINDOW))
    return bounds


def _ms(value):
    return int(value) if value.denominator == 1 else float(value)


def windows(track, active, ownership=None):
    """Fallback windows as half-open session milliseconds."""
    start = Fraction(str(track['startOffsetMs']))
    result = []
    for left, right in ranges16(track, ownership):
        for a, b in window_bounds(right - left):
            a, b = left + a, left + b
            if sum(active[a // FRAME_16K:-(-b // FRAME_16K)]) >= MIN_WINDOW_FRAMES:
                result.append([_ms(start + Fraction(a, 16)), _ms(start + Fraction(b, 16))])
    return result


def person(frames):
    """A speaker's pooled active frames: the sum over its tracks (D63 #1)."""
    return sum(frames)


def fires(active_frames, vad_words):
    """The pooled R71 gate for one speaker: frames and words summed over its tracks."""
    return vad_words < FEW_WORDS and active_frames * FRAME_MS >= MIN_ACTIVE_MS


def need(vad_words):
    return 1 if vad_words == 0 else vad_words + max(2, math.ceil(vad_words / 5))


def decide(tracks):
    """tracks: [{trackId, speaker, activeFrames, vadWords, fallbackWords, silent?}] in track order.

    Returns {speaker: fires} and the provenance entries, one per non-silent track of a
    firing speaker, in track order.
    """
    counted = [t for t in tracks if t.get('silent') is not True]
    speakers = {}
    for t in counted:
        speakers.setdefault(t['speaker'], []).append(t)
    pooled = {key: (person([t['activeFrames'] for t in group]), sum(t['vadWords'] for t in group))
              for key, group in speakers.items()}
    verdict = {speaker: fires(*value) for speaker, value in pooled.items()}
    entries = [dict(trackId=t['trackId'], vadWords=t['vadWords'], fallbackWords=t['fallbackWords'],
                    activeSeconds=t['activeFrames'] * FRAME_MS / 1000,
                    personActiveSeconds=pooled[t['speaker']][0] * FRAME_MS / 1000,
                    used=t['fallbackWords'] >= need(t['vadWords']))
               for t in counted if verdict[t['speaker']]]
    return verdict, entries
