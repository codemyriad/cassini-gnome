# SPDX-License-Identifier: GPL-3.0-or-later
"""D24 word assignment, ported from pinned gocassini e5746ec (S-11).

No model, NumPy or native library is imported here. Stable voice identity comes
from first turns; display numbering comes from the resulting word order.
"""

import hashlib
import json
import logging
import math
import struct
from collections import Counter

LOG = logging.getLogger(__name__)


def turn_key(turn):
    return turn["startMs"], turn["endMs"], turn["speaker"]


def usable(turn):
    return turn["speaker"] >= 0 and 0 <= turn["startMs"] < turn["endMs"]


def normalize_turns(raw, duration_ms):
    result = []
    for turn in raw:
        start, end = float(turn["start"]), float(turn["end"])
        if turn["speaker"] < 0 or not math.isfinite(start) or not math.isfinite(end):
            continue
        # JSON vectors also exercise the float32 -> double C API boundary.
        start, end = (struct.unpack("f", struct.pack("f", value))[0] for value in (start, end))
        start, end = (max(0, min(duration_ms, math.trunc(value * 1000))) for value in (start, end))
        if end > start:
            result.append(dict(startMs=start, endMs=end, speaker=turn["speaker"]))
    return sorted(result, key=turn_key)


def union_turns(turns):
    speakers = {}
    for turn in turns:
        if usable(turn):
            speakers.setdefault(turn["speaker"], []).append(turn)
    result = []
    for rows in speakers.values():
        merged = []
        for turn in sorted(rows, key=turn_key):
            if merged and turn["startMs"] <= merged[-1]["endMs"]:
                merged[-1]["endMs"] = max(merged[-1]["endMs"], turn["endMs"])
            else:
                merged.append(dict(turn))
        result.extend(merged)
    return sorted(result, key=turn_key)


def voice_ordinals(turns):
    result = {}
    for turn in sorted(turns, key=turn_key):
        if usable(turn) and turn["speaker"] not in result:
            result[turn["speaker"]] = len(result) + 1
    return result


def turns_sha256(turns):
    """G12: hash the usable turns as `[startMs, endMs, ordinal]`, never native labels."""
    order = voice_ordinals(turns)
    rows = [[t["startMs"], t["endMs"], order[t["speaker"]]] for t in sorted(turns, key=turn_key) if usable(t)]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("ascii")).hexdigest()


def voice_source(input_sha256, turns):
    """G12 (provisional, R74): what a `<group>~n` voice and its name are bound to."""
    return dict(inputSHA256=input_sha256, turnsSHA256=turns_sha256(turns))


def assign_words(words, turns):
    union = union_turns(turns)
    if not union:
        return None
    result = []
    for word in words:
        sums = {}
        for turn in union:
            overlap = min(word["endMs"], turn["endMs"]) - max(word["startMs"], turn["startMs"])
            if overlap > 0:
                speaker = turn["speaker"]
                sums[speaker] = sums.get(speaker, 0) + overlap
        if sums:
            result.append(max(sums, key=sums.get))  # First encountered wins a tie.
        else:
            nearest = min(union, key=lambda t: max(t["startMs"] - word["endMs"], word["startMs"] - t["endMs"], 0))
            result.append(nearest["speaker"])
    return result


def candidate_ids(group_id, words, turns):
    native = assign_words(words, turns)
    order = voice_ordinals(turns)
    return native, None if native is None else [f"{group_id}~{order[s]}" for s in native]


def apply_group(group_id, words, turns):
    """Keep D11 identity when fewer than two voices actually receive words."""
    native, candidates = candidate_ids(group_id, words, turns)
    seen = set(candidates or ())
    conclusive = len(seen) >= 2
    effective = candidates if conclusive else [group_id] * len(words)
    voices = [f"{group_id}~{n}" for n in voice_ordinals(turns).values() if f"{group_id}~{n}" in seen]
    return dict(
        nativeSpeakers=native,
        candidateWordSpeakerIds=candidates,
        wordSpeakerIds=effective,
        voiceIds=voices if conclusive else ([group_id] if words else []),
        conclusive=conclusive,
    )


def speaking_order(words, ids=None):
    ids = [word["speaker"] for word in words] if ids is None else ids
    ordered = sorted(enumerate(words), key=lambda item: (item[1]["startMs"], item[1].get("_order", item[0])))
    return list(dict.fromkeys(ids[index] for index, _word in ordered))


def apply_transcript(words, groups):
    """One final linear-size rewrite; timing, text and attribution stay intact."""
    output = [dict(word) for word in words]
    new_speakers = {}
    for group in groups:
        positions = [i for i, word in enumerate(output) if word["speaker"] == group["id"]]
        chosen = apply_group(group["id"], [output[i] for i in positions], group["turns"])
        if not chosen["conclusive"]:
            continue
        for index, speaker in zip(positions, chosen["wordSpeakerIds"], strict=True):
            output[index]["speaker"] = speaker
        source = voice_source(group["inputSHA256"], group["turns"])
        for speaker in chosen["voiceIds"]:
            new_speakers[speaker] = dict(
                id=speaker, name=None, provenance="unnamed", colourIndex=0, voiceSource=dict(source)
            )
    for rank, speaker in enumerate(speaking_order(output)):
        if speaker in new_speakers:
            new_speakers[speaker]["colourIndex"] = rank
    return output, new_speakers


def split_segments(segments, group_id, turns):
    """The S-11 pure Go split helper; D24's inconclusive rule is applied above."""
    result = dict(
        segments=segments,
        subSpeakerIds=[],
        wordsPerSpeaker=[],
        speakerCount=len({t["speaker"] for t in turns}),
        turnCount=len(turns),
    )
    parent_words = sorted(
        [w for s in segments if s["speakerId"] == group_id for w in s.get("words", [])], key=lambda w: w["startMs"]
    )
    if not parent_words:
        return result
    _native, ids = candidate_ids(group_id, parent_words, turns)
    if ids is None:
        return result
    counts = Counter(ids)
    voices = [f"{group_id}~{n}" for n in voice_ordinals(turns).values() if f"{group_id}~{n}" in counts]
    passthrough = [s for s in segments if s["speakerId"] != group_id or not s.get("words")]
    wordless = [s for s in passthrough if not s.get("words")]
    attributed = [(s["speakerId"], w) for s in passthrough for w in s.get("words", [])]
    attributed += [
        (voice, word) for voice in voices for word, found in zip(parent_words, ids, strict=True) if found == voice
    ]
    attributed.sort(key=lambda item: item[1]["startMs"])
    merged = []
    for speaker, word in attributed:
        if (
            not merged
            or merged[-1]["speakerId"] != speaker
            or len(merged[-1]["words"]) >= 60
            or word["startMs"] - merged[-1]["endMs"] > 1500
        ):
            merged.append(dict(speakerId=speaker, startMs=word["startMs"], endMs=word["endMs"], text="", words=[]))
        current = merged[-1]
        current["words"].append(dict(word))
        current["endMs"] = max(current["endMs"], word["endMs"])
        current["text"] = " ".join(w["text"] for w in current["words"])
    result.update(
        segments=sorted(merged + wordless, key=lambda s: s["startMs"]),
        subSpeakerIds=voices,
        wordsPerSpeaker=[counts[v] for v in voices],
    )
    return result


def group_tracks(tracks, *, origin="own"):
    """Input may carry the already validated D11 `speaker` from prepare_tracks."""
    from .source_spans import bounds, separate

    groups, kinds = {}, {}
    planned = []
    for track in tracks:
        kind = track.get("kind", "original" if origin == "received" else "remote")
        identity = track.get("sourceTrackId") or track["id"]
        fallback = "me" if kind in ("microphone", "local") else "others" if kind == "system" else "track-" + identity
        speaker = track.get("speaker", "track-original" if origin == "received" else fallback)
        planned.append(dict(track, speaker=speaker, kind=kind))
    separated, diagnostics = separate(planned)
    for diagnostic in diagnostics:
        LOG.warning("Diarization diagnostic: %s", diagnostic)
    for track in separated:
        speaker, kind = track["speaker"], track["kind"]
        group = groups.setdefault(speaker, dict(id=speaker, tracks=[], eligible=False, durationMs=0))
        group["tracks"].append(track["id"])
        kinds.setdefault(speaker, set()).add(kind)
        if "frames" in track:
            _start, end = bounds(track, display=True)
            group["durationMs"] = max(group["durationMs"], math.floor(end))
    for speaker, group in groups.items():
        group["eligible"] = (
            origin == "received"
            or speaker == "others"
            or speaker == "me"
            and len(groups) == 1
            or bool(kinds[speaker] & {"tab", "media", "application"})
        )
    return list(groups.values())
