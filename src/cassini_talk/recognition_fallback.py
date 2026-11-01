# SPDX-License-Identifier: GPL-3.0-or-later
"""D52 G10: a VAD-free windowed decode for a track whose VAD pass found too few words.

Per track, never the mix: attribution stays with the track's own audio. The
seam policy is ported from gocassini 5a5d77bb (stt.go dedupMergedFallbackWords),
the word energy gate from filterWordsByEnergy. Activity is energy inside the
decoded audio; padding and ownership masking are zeros, so they are inactive.
"""

import math
import unicodedata

import numpy as np

RATE = 16000
FRAME = 512
FEW_WORDS = 10
MIN_ACTIVE_SECONDS = 15
WINDOW = 15 * RATE
OVERLAP = RATE // 2
MIN_WINDOW_ACTIVE_SECONDS = 0.5
ACTIVE_DBFS = -50.0
FLOOR_MARGIN_DB = 12.0
FLOOR_PERCENTILE = 0.10
SILENT = 255  # frame code for digital silence (zero after mean removal)

# gocassini stt.go word energy gate.
WORD_PEAK = 0.001
WORD_RMS = 0.0001
WORD_ACTIVE = 0.0005
WORD_ACTIVE_MS = 5
WORD_PRE_MS = 100
WORD_POST_MS = 200

# gocassini stt.go seam alignment tolerances.
DUPLICATE_MS = 400
SINGLETON_MS = 200
SHIFT_MS = 100


class ActivityMeter:
    """Per-frame mean-removed level codes for one track, measured while planning."""

    def __init__(self):
        self.codes = bytearray()
        self.samples = 0
        self._tail = np.empty(0, dtype="<f4")

    def wrap(self, chunks):
        for block in chunks:
            self._feed(block)
            yield block

    def _feed(self, block):
        self.samples += len(block)
        data = np.concatenate((self._tail, np.asarray(block, dtype="<f4")))
        whole = len(data) // FRAME * FRAME
        self._tail = data[whole:]
        for frame in data[:whole].reshape(-1, FRAME).astype(np.float64):
            centred = frame - frame.mean()
            level = math.sqrt(float(np.dot(centred, centred)) / FRAME)
            self.codes.append(SILENT if level <= 0 else int(min(120, max(0, round(120 + 20 * math.log10(level))))))

    def threshold_db(self):
        """max(-50 dBFS, floor + 12 dB).

        Digital silence counts at the bottom of the floor percentile: a sparse
        per-person track is mostly silence, and a floor taken over its speech
        alone would rise above that speech.
        """
        if not self.codes:
            return None, None
        levels = sorted(0 if code == SILENT else code for code in self.codes)
        floor = max(-90.0, levels[int(FLOOR_PERCENTILE * (len(levels) - 1))] - 120.0)
        return floor, max(ACTIVE_DBFS, floor + FLOOR_MARGIN_DB)

    def active(self):
        _floor, threshold = self.threshold_db()
        if threshold is None:
            return [False] * len(self.codes)
        return [code != SILENT and code - 120 >= threshold for code in self.codes]

    def active_seconds(self, active=None):
        active = self.active() if active is None else active
        return sum(active) * FRAME / RATE


def window_bounds(total):
    """gocassini nonVADWindowBounds: 15 s windows overlapping by 0.5 s; the last is short."""
    if total <= 0:
        return []
    if total <= WINDOW:
        return [(0, total)]
    bounds = []
    for start in range(0, total, WINDOW - OVERLAP):
        end = start + WINDOW
        if end >= total:
            bounds.append((start, total))
            break
        bounds.append((start, end))
    return bounds


def active_windows(total, active):
    """Windows holding at least half a second of active frames."""
    needed = math.ceil(MIN_WINDOW_ACTIVE_SECONDS * RATE / FRAME)
    return [
        (start, end)
        for start, end in window_bounds(total)
        if sum(active[start // FRAME : math.ceil(end / FRAME)]) >= needed
    ]


def audio_windows(chunks, windows, heartbeat=None, *, clock=None):
    """Sequential, possibly overlapping windows; at most one window plus one block resident."""
    import time

    clock = clock or time.monotonic
    last_notice = clock()
    data, data_start = np.empty(0, dtype="<f4"), 0
    for index, (start, end) in enumerate(windows):
        while data_start + len(data) < end:
            block = next(chunks, None)
            if block is None:
                raise ValueError("Audio ended before its fallback window")
            data = np.concatenate((data, block))
            if heartbeat is not None and clock() - last_notice >= 1:
                heartbeat()
                last_notice = clock()
        yield data[start - data_start : end - data_start].copy()
        keep = windows[index + 1][0] if index + 1 < len(windows) else end
        data, data_start = data[max(0, keep - data_start) :], max(data_start, keep)


def energetic(words, samples, base_ms):
    """Keep words whose own audio (100 ms before, 200 ms after) is not near-silence.

    Unlike gocassini, the window's mean is removed first, so a DC offset is not energy.
    """
    centred = np.asarray(samples, dtype=np.float64) - float(np.mean(samples)) if len(samples) else samples
    audio_end = len(centred) * 1000 // RATE
    minimum_active = (RATE * WORD_ACTIVE_MS + 999) // 1000
    kept = []
    for word in words:
        start, end = word["startMs"] - base_ms, word["endMs"] - base_ms
        if end < 0 or end < start or start > audio_end:
            continue
        start = max(0, max(0, start) - WORD_PRE_MS)
        end = min(audio_end, end + WORD_POST_MS)
        first, last = start * RATE // 1000, min(len(centred), (end * RATE + 999) // 1000)
        if last <= first:
            continue
        part = np.abs(centred[first:last])
        if (
            part.max() >= WORD_PEAK
            and float(np.mean(part * part)) >= WORD_RMS * WORD_RMS
            and int(np.count_nonzero(part >= WORD_ACTIVE)) >= minimum_active
        ):
            kept.append(word)
    return kept


def normal(text):
    """gocassini normalizeOverlapWord, with NFC and Unicode casefolding."""
    value = unicodedata.normalize("NFC", text).replace("‘", "'").replace("’", "'").casefold()
    value = value.strip(' \t\n\r\f\v,!?;:"“”()[]{}<>').rstrip(".")
    return value if any(c.isalnum() for c in value) else ""


def _midpoint(word):
    start, end = word["startMs"], word["endMs"]
    return start if end <= start else start + (end - start) // 2


def _in_overlap(words, left, right):
    for word in words:
        if word["endMs"] < word["startMs"] or not normal(word["text"]):
            continue
        if word["endMs"] == word["startMs"]:
            if left <= word["startMs"] <= right:
                return True
        elif word["endMs"] > left and word["startMs"] < right:
            return True
    return False


def _references(words, left, right):
    return [
        (index, normal(word["text"]), word["startMs"])
        for index, word in enumerate(words)
        if word["endMs"] >= word["startMs"]
        and word["endMs"] >= left - DUPLICATE_MS
        and word["startMs"] <= right + DUPLICATE_MS
        and normal(word["text"])
    ]


def _align(previous, following, left, right):
    """Order-preserving alignment of equal normalized words starting within 400 ms."""
    a, b = _references(previous, left, right), _references(following, left, right)
    if not a or not b:
        return []
    # Cells are (matches, distance, step); more matches, then less distance, wins.
    cells = [[(0, 0, "")] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        cells[i][0] = (*cells[i - 1][0][:2], "l")
    for j in range(1, len(b) + 1):
        cells[0][j] = (*cells[0][j - 1][:2], "r")
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            best = (*cells[i - 1][j][:2], "l")
            right_cell = cells[i][j - 1]
            if right_cell[0] > best[0] or right_cell[0] == best[0] and right_cell[1] < best[1]:
                best = (*right_cell[:2], "r")
            distance = abs(a[i - 1][2] - b[j - 1][2])
            if a[i - 1][1] == b[j - 1][1] and distance <= DUPLICATE_MS:
                diagonal = cells[i - 1][j - 1]
                matched = (diagonal[0] + 1, diagonal[1] + distance, "m")
                if matched[0] > best[0] or matched[0] == best[0] and matched[1] <= best[1]:
                    best = matched
            cells[i][j] = best
    matches = []
    i, j = len(a), len(b)
    while i > 0 or j > 0:
        step = cells[i][j][2]
        if step == "m":
            matches.append((a[i - 1][0], b[j - 1][0]))
            i, j = i - 1, j - 1
        elif step == "l":
            i -= 1
        elif step == "r":
            j -= 1
        else:
            break
    return matches[::-1]


def _confident(matches, previous, following, right):
    keep = [False] * len(matches)
    for index, (old, new) in enumerate(matches):
        a, b = previous[old], following[new]
        if abs(a["startMs"] - b["startMs"]) <= SINGLETON_MS or a["startMs"] == a["endMs"] == right:
            keep[index] = True
    for index in range(len(matches) - 1):
        (old, new), (next_old, next_new) = matches[index], matches[index + 1]
        if next_old != old + 1 or next_new != new + 1:
            continue
        shift = following[new]["startMs"] - previous[old]["startMs"]
        next_shift = following[next_new]["startMs"] - previous[next_old]["startMs"]
        if abs(shift - next_shift) <= SHIFT_MS:
            keep[index] = keep[index + 1] = True
    return [match for match, kept in zip(matches, keep, strict=True) if kept]


def merge_seam(previous, following, window_start_ms, overlap_ms):
    """Merge the next window's words into the previous ones across one overlap."""
    if not previous or not following:
        return previous + following
    right = window_start_ms + overlap_ms
    matches = _confident(_align(previous, following, window_start_ms, right), previous, following, right)
    if not matches and _in_overlap(previous, window_start_ms, right) and _in_overlap(following, window_start_ms, right):
        # Two populated readings that cannot be aligned: one owner each side of the midpoint.
        cut = window_start_ms + overlap_ms // 2
        kept = list(previous)
        while kept and kept[-1]["startMs"] >= cut:
            kept.pop()
        return kept + [word for word in following if word["startMs"] >= cut]
    dropped_previous, dropped_following = set(), set()
    for old, new in matches:
        a, b = previous[old], following[new]
        a_duration, b_duration = a["endMs"] > a["startMs"], b["endMs"] > b["startMs"]
        if a_duration != b_duration:
            (dropped_previous if b_duration else dropped_following).add(old if b_duration else new)
            continue
        old_context = max(0, right - _midpoint(a))
        new_context = max(0, _midpoint(b) - window_start_ms)
        if new_context > old_context:
            dropped_previous.add(old)
        else:
            dropped_following.add(new)
    merged = [w for i, w in enumerate(previous) if i not in dropped_previous]
    merged += [w for i, w in enumerate(following) if i not in dropped_following]
    return sorted(merged, key=lambda word: word["startMs"])


def need(vad_words):
    """gocassini margin: the fallback must clearly beat the VAD pass to replace it."""
    return 1 if vad_words == 0 else vad_words + max(2, math.ceil(vad_words / 5))
