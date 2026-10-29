# SPDX-License-Identifier: GPL-3.0-or-later
"""S-9 production adapter; expected files remain independently authored."""

import json
import math
from pathlib import Path

from cassini_viewer import voices_logic as logic

ROOT = Path(__file__).resolve().parent / "ux/voices"


def vector(value):
    if value is None:
        return None
    if isinstance(value, list):
        return [logic.f32(float(x)) for x in value]
    if "normalise" in value:
        return logic.normalised(vector(value["normalise"]))
    result = [0.0] * value["dim"]
    if "axis" in value:
        result[value["axis"]] = 1.0
    else:
        score = logic.f32(value["near"])
        result[0] = score
        result[value["other"]] = logic.f32(math.sqrt(logic.f32(1 - logic.f32(score * score))))
    return result


def voices(data):
    return [dict(v, mean=vector(v["mean"])) for v in data["voices"]]


def run(op, data):
    if op == "normalise":
        return logic.normalised(vector(data["vector"]))
    if op == "cosine":
        return logic.cosine(vector(data["a"]), vector(data["b"]))
    if op == "select":
        return logic.select_windows(data["turns"], data["words"], data["sampleRate"], data["totalSamples"])
    if op == "dimension":
        return logic.DIMENSION
    if op == "stale":
        return logic.stale(dict(model=data["model"], mean=[0.0] * data["dimension"]))
    if op == "normaliseName":
        return logic.name_key(data["name"])
    if op == "nameCandidates":
        return [v["id"] for v in logic.name_candidates(data["typed"], voices(data))]
    if op == "namePick":
        result = logic.pick_name(data["typed"], voices(data), data["scores"])
        return result["id"] if result else None
    if op == "rank":
        return [
            dict(id=v["voice"]["id"], score=v["score"], stale=v["stale"], likely=v["likely"])
            for v in logic.ranked(vector(data["print"]), voices(data), data["suggest"])
        ]
    if op == "match":
        result = logic.match(
            {k: vector(v) for k, v in data["speakers"].items()},
            voices(data),
            data["model"],
            **data["thresholds"],
            excluded=data["excluded"],
        )
        return {key: dict(voiceId=v["voiceId"], state=v["state"]) for key, v in result.items()}
    if op == "decodeVersion":
        try:
            logic.decode_people(json.dumps(data))
        except logic.UnsupportedVersion as error:
            return dict(unsupportedVersion=error.found)
        raise AssertionError("An unknown version was accepted")
    if op == "voiceRoundtrip":
        v = logic.enrol(None, data["name"], data["model"], vector(data["embedding"]), data["seconds"], data["now"])
        value = logic.encode_people([v])
        (v,) = logic.decode_people(value)
        assert v.pop("id").startswith("voice_")
        return dict(v, idPrefix="voice_", version=json.loads(value)["version"])
    if op == "enrolUnenrol":
        first = logic.enrol(None, "Anna", "m", vector(data["first"]), 1, 0)
        second = logic.enrol(first, "Anna", "m", vector(data["second"]), 1, 0)
        v = logic.unenrol(second, vector(data["undo"]))
        return None if v is None else {k: v[k] for k in ["mean", "count", "seconds"]}
    if op == "selectResolved":
        aliases = {e["from"]: e["into"] for e in data["speakerMerges"]}

        def resolved(speaker):
            seen = set()
            while speaker in aliases:
                assert speaker not in seen
                seen.add(speaker)
                speaker = aliases[speaker]
            return speaker

        own = {}
        for raw, labels in data["nativeSpeakerIds"].items():
            own.setdefault(resolved(raw), set()).update(labels)
        return logic.select_windows(
            data["turns"],
            [dict(w, speaker=resolved(w["speaker"])) for w in data["words"]],
            data["sampleRate"],
            data["totalSamples"],
            own_labels=own,
        )
    if op == "meetingKeys":
        keys = [Path(p).name for p in data["folders"]]
        return dict(keys=keys, files=[k + ".json" for k in keys], distinctMeetingCount=len(set(keys)))
    raise AssertionError(op)
