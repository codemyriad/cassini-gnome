# SPDX-License-Identifier: GPL-3.0-or-later
"""D25 extraction inside the speech child, after final D24 attribution."""

import contextlib
import json
from collections import deque
from pathlib import Path

from .diarization import group_tracks, voice_ordinals
from .library import _sidecar_words
from .meeting_store import read_metadata
from .received import regular_input
from .speaker_merges import resolve_merges
from .voices_logic import MAX_SECONDS, MIN_SEGMENT_MS, select_windows
from .worker_diarization import read_cache


def read_words(path):
    with regular_input(path) as stream:
        raw = stream.read((120 << 20) + 1)
    if len(raw) > 120 << 20:
        raise ValueError("Voice transcript exceeds limit")
    value = json.loads(raw)
    if value.get("format") != "cassini.words.v1":
        raise ValueError("Unexpected voice transcript format")
    return [dict(speaker=w.speaker, startMs=w.start_ms, endMs=w.end_ms) for w in _sidecar_words(value)]


def group_words(group, words, turns, merges):
    ordinals = voice_ordinals(turns)
    native_ids = {f"{group['id']}~{ordinal}": native for native, ordinal in ordinals.items()}
    selected = [w for w in words if w["speaker"] == group["id"] or w["speaker"] in native_ids]
    labels = {}
    for word in selected:
        raw = word["speaker"]
        if raw in native_ids:
            labels.setdefault(merges.get(raw, raw), set()).add(native_ids[raw])
    return [dict(w, speaker=merges.get(w["speaker"], w["speaker"])) for w in selected], labels


def combined_windows(windows, total_frames):
    """One speaker may span merged groups; apply its 45 s budget once."""
    thirds = [deque() for _ in range(3)]
    for stream, start, end in sorted(windows, key=lambda w: (w[1] - w[2], w[1])):
        third = min(2, (start + end) * 3 // (2 * total_frames)) if total_frames else 0
        thirds[third].append((stream, start, end))
    budget, chosen = MAX_SECONDS * 16000, []
    while any(thirds) and budget >= MIN_SEGMENT_MS * 16:
        for queue in thirds:
            if not queue:
                continue
            stream, start, end = queue.popleft()
            end = min(end, start + budget)
            if end - start >= MIN_SEGMENT_MS * 16:
                chosen.append((stream, start, end))
                budget -= end - start
    return sorted(chosen, key=lambda w: w[1])


def run(tracks, options, notify):
    """Only bounded per-speaker prints cross the private worker pipe."""
    from .campplus import SpeakerExtractor
    from .diarization_audio import rejoin

    directory = Path(options["directory"])
    words = read_words(directory / "transcript.json")
    metadata = read_metadata(directory)
    merges = resolve_merges(
        options.get("speakerMerges", metadata.get("speakerMerges", [])), {w["speaker"] for w in words}
    )
    cached = read_cache(directory / "speaker-turns.json")
    tracks_by_id = {t["id"]: t for t in tracks}
    plans = {}
    total_frames = 0
    with contextlib.ExitStack() as stack:
        model = stack.enter_context(contextlib.closing(SpeakerExtractor(options["modelPath"], options["runtimePath"])))
        for group in group_tracks(tracks, origin=options.get("origin", "own")):
            turns = cached.get(group["id"], {}).get("turns", [])
            selected, labels = group_words(group, words, turns, merges)
            selected = [w for w in selected if w["speaker"] != "me"]
            if not selected:
                continue
            stream, frames, _sha = stack.enter_context(
                rejoin(
                    [tracks_by_id[t] for t in group["tracks"]],
                    scratch=options.get("scratch"),
                    heartbeat=lambda: notify("voice-progress"),
                )
            )
            total_frames = max(total_frames, frames)
            for speaker, windows in select_windows(
                turns, selected, 16000, frames, own_labels=labels, min_seconds=0, max_seconds=None
            ).items():
                plans.setdefault(speaker, []).extend((stream, start, end) for start, end in windows)
        for speaker, windows in plans.items():
            budget, chunks, chosen = MAX_SECONDS * 16000, [], []
            for stream, start, end in combined_windows(windows, total_frames):
                end = min(end, start + budget)
                if end - start < MIN_SEGMENT_MS * 16:
                    continue
                stream.seek(start * 4)
                raw = stream.read((end - start) * 4)
                if len(raw) != (end - start) * 4:
                    raise ValueError("Voice window audio was truncated")
                import numpy as np

                chunks.append(np.frombuffer(raw, dtype="<f4"))
                chosen.append(dict(startMs=start // 16, endMs=end // 16))
                budget -= end - start
                if budget < MIN_SEGMENT_MS * 16:
                    break
            result = model.embed(chunks)
            if result is None and sum(len(chunk) for chunk in chunks) >= 4 * 16000:
                raise ValueError("Selected speech produced no usable speaker print")
            if result is not None:
                notify(
                    "voice-print",
                    speaker=speaker,
                    embedding=result["embedding"],
                    seconds=result["seconds"],
                    windows=chosen,
                    inputSHA256=result["inputSHA256"],
                )
            notify("voice-progress")
    notify("voices-complete")


def request(message, notify):
    try:
        tracks = message["tracks"]
        if not isinstance(tracks, list) or not 1 <= len(tracks) <= 64:
            raise ValueError("Invalid voice track count")
        run(tracks, message["options"], notify)
    except Exception as error:
        notify("voices-error", message=str(error))
