# SPDX-License-Identifier: GPL-3.0-or-later
"""D25 pure speaker rules, ported from Android7068c58; no engine imports."""

import json
import math
import re
import struct
import unicodedata
from collections import defaultdict, deque
from uuid import uuid4

REVISION = "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b"
DIMENSION = 512
AUTO, SUGGEST, MARGIN = 0.72, 0.50, 0.08
EDGE_TRIM_MS, MIN_SEGMENT_MS, MAX_WINDOW_MS = 200, 1500, 10000
MAX_SECONDS, MIN_SECONDS, MAX_COUNT_WEIGHT = 45, 4.0, 20


def f32(value):
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def normalised(vector):
    vector = [f32(float(x)) for x in vector]
    norm = f32(math.sqrt(sum(float(x) * x for x in vector)))
    return None if not math.isfinite(norm) or norm == 0 else [f32(x / norm) for x in vector]


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError("Speaker print dimensions differ")
    return f32(sum(float(x) * y for x, y in zip(a, b, strict=True)))


def stale(voice):
    return voice["model"] != REVISION or len(voice["mean"]) != DIMENSION


def name_key(name):
    collapsed = re.sub(r"\s+", " ", name.strip(), flags=re.ASCII)
    stripped = "".join(c for c in unicodedata.normalize("NFD", collapsed) if unicodedata.category(c) != "Mn")
    return stripped.upper().lower()  # Kotlin's locale-independent case folding.


def person_name(value, source_labels=()):
    """D35: generated source labels must never become remembered people."""
    value = name_key(value)
    return bool(
        value
        and value not in {name_key(label) for label in source_labels}
        and re.fullmatch(r"speaker \d+", value) is None
    )


def alphabetical(voice):
    return name_key(voice["name"]), voice["name"], voice["id"]


def name_candidates(typed, voices):
    typed = name_key(typed)
    if not typed:
        return []
    return [v for v in voices if typed in (name_key(v["name"]), name_key(v["name"]).split(" ", 1)[0])]


def pick_name(typed, voices, scores):
    return next(
        iter(sorted(name_candidates(typed, voices), key=lambda v: (-scores.get(v["id"], -math.inf), alphabetical(v)))),
        None,
    )


def ranked(print, voices, suggest=SUGGEST):
    current, old = [], []
    for voice in voices:
        if stale(voice):
            old.append(dict(voice=voice, score=None, stale=True, likely=False))
        else:
            score = cosine(print, voice["mean"]) if print is not None and len(print) == len(voice["mean"]) else None
            current.append(dict(voice=voice, score=score, stale=False, likely=False))
    current.sort(key=lambda v: (-v["score"] if v["score"] is not None else math.inf, alphabetical(v["voice"])))
    old.sort(key=lambda v: alphabetical(v["voice"]))
    result = current + old
    if result and result[0]["score"] is not None and result[0]["score"] >= f32(suggest):
        result[0]["likely"] = True
    return result


def match(speakers, voices, model=REVISION, *, auto=AUTO, suggest=SUGGEST, margin=MARGIN, excluded=None):
    excluded = excluded or {}
    scores = []
    for speaker, print in speakers.items():
        for voice in voices:
            if (
                voice["model"] == model
                and len(voice["mean"]) == len(print)
                and voice["id"] not in excluded.get(speaker, ())
            ):
                score = cosine(print, voice["mean"])
                if math.isfinite(score):
                    scores.append((speaker, voice["id"], score))
    result, taken = {}, set()
    for speaker, voice, score in sorted(scores, key=lambda item: -item[2]):
        if score < f32(suggest) or speaker in result or voice in taken:
            continue
        rivals = [value for s, v, value in scores if (s == speaker) != (v == voice)]
        automatic = score >= f32(auto) and (not rivals or f32(score - max(rivals)) >= f32(margin))
        result[speaker] = dict(voiceId=voice, score=score, state="auto" if automatic else "suggested")
        taken.add(voice)
    return result


def _clean(start, end, others):
    pieces = [(start, end)]
    for turn in others:
        result = []
        for s, e in pieces:
            a, b = turn["startMs"], turn["endMs"]
            if b <= s or a >= e:
                result.append((s, e))
            else:
                if a > s:
                    result.append((s, a))
                if b < e:
                    result.append((b, e))
        pieces = result
    return pieces


def select_windows(
    turns, words, sample_rate, total_samples, *, own_labels=None, min_seconds=MIN_SECONDS, max_seconds=MAX_SECONDS
):
    if sample_rate <= 0 or total_samples < 0:
        raise ValueError("Invalid print audio geometry")
    valid = [t for t in turns if t["endMs"] > t["startMs"] and t["speaker"] >= 0]
    spoken, by_native = defaultdict(list), defaultdict(list)
    for word in words:
        spoken[word["speaker"]].append(word)
    for turn in valid:
        by_native[turn["speaker"]].append(turn)
    own = {}
    for speaker, rows in spoken.items():
        scores = {
            n: sum(max(0, min(w["endMs"], t["endMs"]) - max(w["startMs"], t["startMs"])) for w in rows for t in ts)
            for n, ts in by_native.items()
        }
        positive = {key: value for key, value in scores.items() if value > 0}
        own[speaker] = {max(positive, key=positive.get)} if positive else set()
    if own_labels is not None:
        own.update(own_labels)
    runs = []
    for word in words:
        if word["endMs"] <= word["startMs"]:
            continue
        speaker = word["speaker"]
        if runs and runs[-1][0] == speaker:
            runs[-1] = speaker, runs[-1][1], max(runs[-1][2], word["endMs"])
        else:
            runs.append((speaker, word["startMs"], word["endMs"]))
    spans = defaultdict(list)
    for speaker, start, end in runs:
        spans[speaker].append((start, end))
    total_ms = total_samples * 1000 // sample_rate
    result = {}
    for speaker, intervals in spans.items():
        others = [t for t in valid if t["speaker"] not in own[speaker]]
        segments = []
        for start, end in intervals:
            for s, e in _clean(start, end, others):
                s, e = s + EDGE_TRIM_MS, e - EDGE_TRIM_MS
                if e - s < MIN_SEGMENT_MS:
                    continue
                parts = (e - s + MAX_WINDOW_MS - 1) // MAX_WINDOW_MS
                segments.extend((s + (e - s) * i // parts, s + (e - s) * (i + 1) // parts) for i in range(parts))
        thirds = [deque() for _ in range(3)]
        for span in sorted(segments, key=lambda x: (x[0] - x[1], x[0])):
            third = max(0, min(2, (sum(span) // 2) * 3 // total_ms)) if total_ms > 0 else 0
            thirds[third].append(span)
        budget, chosen = (max_seconds * 1000 if max_seconds is not None else sum(e - s for s, e in segments)), []
        while budget > 0 and any(thirds):
            for queue in thirds:
                if not queue:
                    continue
                start, end = queue.popleft()
                end = min(end, start + budget)
                if end - start >= MIN_SEGMENT_MS:
                    chosen.append((start, end))
                    budget -= end - start
                if budget < MIN_SEGMENT_MS:
                    budget = 0
                    break
        if sum(e - s for s, e in chosen) < min_seconds * 1000:
            continue
        windows = [
            (max(0, min(total_samples, s * sample_rate // 1000)), max(0, min(total_samples, e * sample_rate // 1000)))
            for s, e in sorted(chosen)
        ]
        windows = [(s, e) for s, e in windows if e > s]
        if windows:
            result[speaker] = windows
    return result


def enrol(previous, name, model, embedding, seconds, now):
    if previous is None or previous["model"] != model or len(previous["mean"]) != len(embedding):
        unit = normalised(embedding)
        if unit is None:
            raise ValueError("No speaker print to remember")
        return dict(
            id=previous["id"] if previous else "voice_" + str(uuid4()),
            name=name,
            model=model,
            dim=len(unit),
            mean=unit,
            count=1,
            seconds=seconds,
            createdAt=previous["createdAt"] if previous else now,
            updatedAt=now,
        )
    weight = min(previous["count"], MAX_COUNT_WEIGHT)
    mean = normalised([f32(f32(x * weight) + y) for x, y in zip(previous["mean"], embedding, strict=True)])
    if mean is None:
        raise ValueError("No speaker print to remember")
    return dict(
        previous,
        name=name,
        mean=mean,
        count=previous["count"] + 1,
        seconds=previous["seconds"] + seconds,
        updatedAt=now,
    )


def unenrol(voice, embedding):
    e = normalised(embedding)
    if e is None or len(e) != len(voice["mean"]):
        return None
    weight = min(voice["count"] - 1, MAX_COUNT_WEIGHT)
    dot = cosine(voice["mean"], e)
    length = f32(dot + math.sqrt(max(0, dot * dot - 1 + weight * weight)))
    mean = normalised([f32(f32(x * length) - y) for x, y in zip(voice["mean"], e, strict=True)])
    return dict(voice, mean=voice["mean"] if mean is None else mean, count=voice["count"] - 1)


class UnsupportedVersion(ValueError):
    def __init__(self, found):
        self.found = found
        super().__init__(f"Saved voices version {found} is unsupported")


def _invalid_number(token):
    raise ValueError(token)


def decode_people(text):
    value = json.loads(text, parse_constant=_invalid_number)
    version = value["version"]
    if type(version) is not int or version != 1:
        raise UnsupportedVersion(version)
    result = []
    for item in value["voices"]:
        voice = dict(item)
        if type(voice["count"]) is not int or voice["count"] <= 0 or voice["dim"] != len(voice["mean"]):
            raise ValueError("Invalid voice profile")
        voice["mean"] = [f32(v) for v in voice["mean"]]
        if any(not math.isfinite(v) for v in voice["mean"]):
            raise ValueError("Invalid voice profile")
        result.append(voice)
    return result


def encode_people(voices):
    return json.dumps(dict(version=1, voices=voices), ensure_ascii=False, allow_nan=False)
