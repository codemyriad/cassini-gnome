# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional D24 step 3b inside either existing recognition worker."""

import json
import math
import time

from .diarization import group_tracks
from .nemotron_model import MODEL_TAG
from .received import regular_input

PARAMS = dict(minDurationOn=0.3, minDurationOff=0.5)
FORMAT = "cassini.speaker-turns.v1"


def valid_group(group):
    return (
        isinstance(group, dict)
        and isinstance(group.get("id"), str)
        and type(group.get("durationMs")) is int
        and group["durationMs"] >= 0
        and isinstance(group.get("inputSHA256"), str)
        and len(group["inputSHA256"]) == 64
        and isinstance(group.get("turns"), list)
        and len(group["turns"]) <= 100000
        and all(
            isinstance(t, dict)
            and all(type(t.get(k)) is int for k in ("speaker", "startMs", "endMs"))
            and t["speaker"] >= 0
            and 0 <= t["startMs"] < t["endMs"] <= group["durationMs"]
            for t in group["turns"]
        )
    )


def read_cache(path):
    if not path:
        return {}
    try:
        with regular_input(path) as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            return {}
        value = json.loads(raw)
        if value.get("format") != FORMAT or value.get("model") != MODEL_TAG or value.get("params") != PARAMS:
            return {}
        groups = value["groups"]
        if not isinstance(groups, list) or len(groups) > 64 or not all(valid_group(g) for g in groups):
            return {}
        return {g["id"]: g for g in groups}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def run(tracks, options, notify):
    """Failure keeps D11. Results are streamed in bounded messages, never words."""
    from .diarization_audio import rejoin

    by_id = {t["id"]: t for t in tracks}
    cached = read_cache(options.get("storedTurnsPath"))
    model = None
    diagnostics, completed = [], []
    measured_seconds = measured_frames = 0
    factor = options.get("realtimeFactor", 0.05)
    if type(factor) not in (int, float) or not math.isfinite(factor) or factor <= 0:
        factor = 0.05
    try:
        for group in group_tracks(tracks, origin=options.get("origin", "own")):
            if not group["eligible"]:
                continue
            identifier = group["id"]
            try:

                def heartbeat(group=group, identifier=identifier):
                    notify(
                        "diarizing",
                        groupId=identifier,
                        etaMinutes=max(1, math.ceil(group["durationMs"] * factor / 60000)),
                    )

                with rejoin(
                    [by_id[t] for t in group["tracks"]], scratch=options.get("scratch"), heartbeat=heartbeat
                ) as (stream, frames, sha):
                    duration = frames * 1000 // 16000
                    old = cached.get(identifier)
                    if old and old["inputSHA256"] == sha and old["durationMs"] == duration:
                        turns, reused = old["turns"], True
                    elif frames < 3200:
                        turns, reused = [], False
                    else:
                        if model is None:
                            from .nemotron import Nemotron

                            model = Nemotron(options["modelPath"], options["runtimePath"])
                        import numpy as np

                        # Mapping stays owned by this block, and all C pointers
                        # are released before closing it. No full PCM copy.
                        samples = np.memmap(stream, dtype="<f4", mode="r", shape=(frames,))
                        last = time.monotonic()

                        def progress(_done, _total):
                            nonlocal last
                            if time.monotonic() - last >= 1:
                                heartbeat()
                                last = time.monotonic()

                        try:
                            result = model.infer(samples, progress)
                        finally:
                            samples._mmap.close()
                        measured_seconds += result["inferenceSeconds"]
                        measured_frames += frames
                        turns, reused = result["turns"], False
                value = dict(id=identifier, durationMs=duration, inputSHA256=sha, turns=turns)
                if not valid_group(value):
                    raise ValueError("Invalid diarization group")
                # The existing transport limits each reply to1MiB. Long turns
                # use separate messages, with an explicit final batch marker.
                for first in range(0, max(1, len(turns)), 512):
                    notify(
                        "diarized",
                        groupId=identifier,
                        durationMs=duration,
                        inputSHA256=sha,
                        turns=turns[first : first + 512],
                        first=first == 0,
                        final=first + 512 >= len(turns),
                        reused=reused,
                        etaMinutes=0,
                    )
                completed.append(identifier)
            except Exception as error:
                diagnostics.append(dict(code="diarization-kept-d11", group=identifier, message=str(error)))
    finally:
        if model is not None:
            model.close()
    return dict(
        model=MODEL_TAG,
        params=PARAMS,
        groupIds=completed,
        realtimeFactor=measured_seconds * 16000 / measured_frames if measured_frames else None,
        diagnostics=diagnostics,
    )


def estimate(tracks, options):
    if not options:
        return 0
    factor = options.get("realtimeFactor", 0.05)
    if type(factor) not in (int, float) or not math.isfinite(factor) or factor <= 0:
        factor = 0.05
    return (
        sum(g["durationMs"] for g in group_tracks(tracks, origin=options.get("origin", "own")) if g["eligible"])
        * factor
        / 1000
    )


def complete(summary, tracks, options, notify):
    if not options:
        return summary
    # If a native diarizer crashes after recognition, the parent still has
    # every word and this marker. It can finish the transcript with D11 names.
    notify("recognised", summary=summary, etaMinutes=1)
    try:
        summary["diarization"] = run(tracks, options, notify)
    except Exception as error:
        summary.setdefault("diagnostics", []).append(dict(code="diarization-kept-d11", message=str(error)))
    return summary
