# SPDX-License-Identifier: GPL-3.0-or-later
"""D38 cross-track echo classification; never changes words or capture audio."""

import unicodedata
from collections import defaultdict, deque

MICROPHONE_KINDS = frozenset(("microphone", "local"))


def normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    while value and unicodedata.category(value[0]).startswith("P"):
        value = value[1:]
    while value and unicodedata.category(value[-1]).startswith("P"):
        value = value[:-1]
    return value


def echo_indices(words, tracks, level=None):
    """Return input indexes. A level callback reads dBFS at meeting-relative times.

    Frozen vectors carry word RMS and the other-track comparison-window RMS.
    Real jobs provide the same measurements from unchanged PCM. Only direct
    matches anchor the second pass: no recursive expansion into own speech.
    """
    microphones = {track["id"] for track in tracks if track["kind"] in MICROPHONE_KINDS}
    others = {track["id"] for track in tracks} - microphones
    if not microphones or not others:
        return set()
    mine, theirs, per_mic = defaultdict(list), defaultdict(list), defaultdict(list)
    for index, word in enumerate(words):
        key = normalized(word["text"])
        if word["track"] in microphones:
            mine[key].append(index)
            per_mic[word["track"]].append(index)
        elif word["track"] in others:
            theirs[key].append(index)

    def own_rms(index):
        word = words[index]
        return level(word["track"], word["startMs"], word["endMs"]) if level else word["rmsDbfs"]

    direct = set()
    for key, indexes in mine.items():
        candidates = iter(sorted(theirs[key], key=lambda i: (words[i]["startMs"], i)))
        candidate = next(candidates, None)
        loudest = deque()
        for index in sorted(indexes, key=lambda i: (words[i]["startMs"], i)):
            start = words[index]["startMs"]
            while candidate is not None and words[candidate]["startMs"] <= start:
                value = own_rms(candidate)
                while loudest and loudest[-1][1] <= value:
                    loudest.pop()
                loudest.append((words[candidate]["startMs"], value))
                candidate = next(candidates, None)
            while loudest and loudest[0][0] < start - 600:
                loudest.popleft()
            if loudest and own_rms(index) <= loudest[0][1] - 6:
                direct.add(index)
    sandwich = set()
    for indexes in per_mic.values():
        indexes.sort(key=lambda i: (words[i]["startMs"], i))
        following, after = {}, None
        for index in reversed(indexes):
            following[index] = after
            if index in direct:
                after = index
        before = None
        for index in indexes:
            if index in direct:
                before = index
                continue
            after = following[index]
            if before is None or after is None or words[after]["startMs"] - words[before]["endMs"] >= 1000:
                continue
            word = words[index]
            for track in others:
                comparison = (
                    level(track, word["startMs"] - 600, word["endMs"])
                    if level
                    else word.get("otherWindowRmsDbfs", {}).get(track)
                )
                if comparison is not None and own_rms(index) <= comparison - 6:
                    sandwich.add(index)
                    break
    return direct | sandwich


class WaveLevels:
    """Seekable native PCM RMS, at most one bounded block resident per query.

    Offsets are meeting-relative, as are recognition word times. WAVs, FIR and
    recogniser input are untouched. This runs only in the recognition child.
    """

    def __init__(self, tracks, heartbeat=lambda: None):
        from contextlib import ExitStack
        from functools import lru_cache

        self.stack = ExitStack()
        self.tracks = tracks
        self.sources = {}
        self.heartbeat = heartbeat
        self.level = lru_cache(maxsize=8192)(self._level)

    def __enter__(self):
        import wave

        from .received import regular_input

        try:
            for track in self.tracks:
                stream = self.stack.enter_context(regular_input(track["path"]))
                source = self.stack.enter_context(wave.open(stream, "rb"))
                if source.getsampwidth() != 2 or source.getnchannels() not in (1, 2) or source.getcomptype() != "NONE":
                    raise ValueError("Echo measurement needs PCM16 mono or stereo")
                self.sources[track["id"]] = (source, track.get("offsetMs") or 0)
        except BaseException:
            self.stack.close()
            raise
        return self

    def _level(self, track, start, end):
        import math

        import numpy as np

        source, offset = self.sources[track]
        rate, channels, total = source.getframerate(), source.getnchannels(), source.getnframes()
        first = min(total, max(0, math.floor((start - offset) * rate / 1000)))
        last = min(total, max(0, math.floor((end - offset) * rate / 1000)))
        remaining, squares, count = max(0, last - first), 0.0, 0
        source.setpos(first)
        while remaining:
            self.heartbeat()
            raw = source.readframes(min(16384, remaining))
            values = np.frombuffer(raw, dtype="<i2").astype(np.float64).reshape(-1, channels).mean(axis=1)
            if not len(values):
                raise ValueError("Echo measurement encountered truncated PCM")
            squares += float(np.dot(values, values))
            count += len(values)
            remaining -= len(values)
        return 20 * math.log10(max(1e-9, math.sqrt(squares / count) if count else 0) / 32768)

    def __exit__(self, *args):
        self.level.cache_clear()
        self.stack.close()


def notify_echo(words, tracks, notify):
    """Bounded additive replies indexed in original recognition progress order."""
    import time

    microphones = [track for track in tracks if track.get("kind") in MICROPHONE_KINDS]
    if not words or not microphones or len(microphones) == len(tracks):
        return
    last_notice = time.monotonic()

    def heartbeat():
        nonlocal last_notice
        if time.monotonic() - last_notice >= 1:
            notify("planning", phase="echo")
            last_notice = time.monotonic()

    with WaveLevels(tracks, heartbeat) as levels:
        flagged = sorted(echo_indices(words, tracks, levels.level))
    for first in range(0, len(flagged), 2048):
        notify("echo", indexes=flagged[first : first + 2048])
