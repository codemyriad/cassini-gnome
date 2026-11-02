# SPDX-License-Identifier: GPL-3.0-or-later
"""D25 extraction inside the speech child, after final D24 attribution."""

import contextlib
import json
import os
from collections import deque
from pathlib import Path

from .diarization import group_tracks, voice_ordinals, voice_source
from .library import LibraryScanner, _sidecar_words
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


def historical_words(directory):
    # Use the validated reader for both embedded Received transcripts and
    # sidecars. Long transcripts never cross the 512 KiB worker request pipe.
    meeting = next(
        m
        for m in LibraryScanner.for_meeting(directory).scan(changed_directories={directory}).meetings
        if m.directory == directory
    )
    if meeting.transcript != "done" or not meeting.metadata_editable:
        raise ValueError("Historical meeting is no longer ready for voices")
    return [dict(speaker=w.speaker, startMs=w.start_ms, endMs=w.end_ms) for w in meeting.words]


def group_words(group, words, turns, merges, *, all_words=False, excluded_groups=(), labelled=None):
    """`labelled` limits ordinal labels to saved ids made from these turns (G12)."""
    ordinals = voice_ordinals(turns)
    native_ids = {f"{group['id']}~{ordinal}": native for native, ordinal in ordinals.items()}
    selected = [
        w
        for w in words
        if (all_words or w["speaker"] == group["id"] or w["speaker"] in native_ids)
        and not any(w["speaker"] == other or w["speaker"].startswith(other + "~") for other in excluded_groups)
    ]
    labels = {}
    for word in selected:
        raw = word["speaker"]
        if raw in native_ids and (labelled is None or raw in labelled):
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


def chosen_windows(windows, total_frames):
    """The (group, start, end) windows one speaker's print reads, in order."""
    budget, chosen = MAX_SECONDS * 16000, []
    for group, start, end in combined_windows(windows, total_frames):
        end = min(end, start + budget)
        if end - start < MIN_SEGMENT_MS * 16:
            continue
        chosen.append((group, start, end))
        budget -= end - start
        if budget < MIN_SEGMENT_MS * 16:
            break
    return chosen


def read_window(stream, start, end):
    import numpy as np

    stream.seek(start * 4)
    raw = stream.read((end - start) * 4)
    if len(raw) != (end - start) * 4:
        raise ValueError("Voice window audio was truncated")
    return np.frombuffer(raw, dtype="<f4")


def run(tracks, options, notify, *, statvfs=os.statvfs):
    """Only bounded per-speaker prints cross the private worker pipe.

    G14: at most one group's rejoin file exists at a time. The first pass
    rejoins each group, picks its windows and closes the file. A speaker's
    45 s budget needs every group's windows, so the second pass rejoins again
    only the groups that hold chosen windows, and reads just those.
    """
    from .campplus import SpeakerExtractor
    from .diarization_audio import live_recordings, rejoin, shortfall

    directory = Path(options["directory"])
    background = options.get("prepareMissing", False)
    if background:
        os.nice(10)
    words = historical_words(directory) if background else read_words(directory / "transcript.json")
    metadata = read_metadata(directory)
    merges = resolve_merges(
        options.get("speakerMerges", metadata.get("speakerMerges", [])), {w["speaker"] for w in words}
    )
    cached = read_cache(directory / "speaker-turns.json")
    tracks_by_id = {t["id"]: t for t in tracks}
    live = live_recordings(options)
    plans, digests = {}, {}
    total_frames = 0
    with contextlib.ExitStack() as stack:
        model = stack.enter_context(contextlib.closing(SpeakerExtractor(options["modelPath"], options["runtimePath"])))
        diarizer = None
        groups = group_tracks(tracks, origin=options.get("origin", "own"))

        def group_audio(index):
            """The group's rejoin, or None after a low-disk diagnostic."""
            group = groups[index]
            if short := shortfall(
                group["durationMs"], scratch=options.get("scratch"), statvfs=statvfs, recordings=live
            ):
                notify("voice-progress", diagnostic=dict(code="voices-skipped-low-disk", group=group["id"], **short))
                return None
            return rejoin(
                [tracks_by_id[t] for t in group["tracks"]],
                scratch=options.get("scratch"),
                heartbeat=lambda: notify("voice-progress"),
            )

        group_file = stack.enter_context(contextlib.ExitStack())
        for index, group in enumerate(groups):
            group_file.close()  # G14: the previous group's audio is gone first.
            if group["id"] == "me" and "me" not in merges and not any(w["speaker"].startswith("me~") for w in words):
                continue
            if (audio := group_audio(index)) is None:
                continue
            stream, frames, sha = group_file.enter_context(audio)
            digests[index] = sha
            previous = cached.get(group["id"], {})
            turns, turns_input = previous.get("turns", []), previous.get("inputSHA256")
            if (
                background
                and group["eligible"]
                and (previous.get("inputSHA256") != sha or previous.get("durationMs") != frames // 16)
            ):
                # Old meetings may predate Tell speakers apart as well. Obtain
                # overlap evidence privately; do not reassign their saved words.
                if diarizer is None:
                    from .nemotron import Nemotron

                    config = options["diarization"]
                    diarizer = stack.enter_context(
                        contextlib.closing(Nemotron(config["modelPath"], config["runtimePath"]))
                    )
                import numpy as np

                samples = np.memmap(stream, dtype="<f4", mode="r", shape=(frames,))
                try:
                    turns = diarizer.infer(samples, lambda *_: notify("voice-progress"))["turns"]
                finally:
                    samples._mmap.close()
                turns_input = sha
            # G12: saved `<group>~n` ids name ordinals of the turns they were
            # stamped with. Other turns' ordinals may belong to another voice.
            stamp = voice_source(turns_input, turns)
            labelled = {s["id"] for s in metadata.get("speakers", []) if s.get("voiceSource") == stamp}
            selected, labels = group_words(
                group,
                words,
                turns,
                merges,
                excluded_groups=tuple(g["id"] for g in groups if g["id"] != group["id"]) if background else (),
                labelled=labelled,
                all_words=background
                and (
                    options.get("origin") == "received"
                    or group["id"] != "me"
                    and sum(g["id"] != "me" for g in groups) == 1
                ),
            )
            selected = [w for w in selected if w["speaker"] != "me"]
            if not selected:
                continue
            total_frames = max(total_frames, frames)
            for speaker, windows in select_windows(
                turns, selected, 16000, frames, own_labels=labels, min_seconds=0, max_seconds=None
            ).items():
                plans.setdefault(speaker, []).extend((index, start, end) for start, end in windows)
        group_file.close()
        chosen = {speaker: chosen_windows(windows, total_frames) for speaker, windows in plans.items()}
        speakers, audio = deque(chosen), {}

        def embed_ready(read):
            """Prints keep plan order; each is made once its groups up to `read` are read."""
            while speakers and all(index <= read for index, _start, _end in chosen[speakers[0]]):
                speaker = speakers.popleft()
                chunks = [audio.pop((speaker, n), None) for n in range(len(chosen[speaker]))]
                if any(chunk is None for chunk in chunks):
                    notify("voice-progress")  # A low-disk skip lost some of its audio.
                    continue
                result = model.embed(chunks)
                if result is None and sum(len(chunk) for chunk in chunks) >= 4 * 16000:
                    raise ValueError("Selected speech produced no usable speaker print")
                if result is not None:
                    notify(
                        "voice-print",
                        speaker=speaker,
                        embedding=result["embedding"],
                        seconds=result["seconds"],
                        windows=[dict(startMs=start // 16, endMs=end // 16) for _, start, end in chosen[speaker]],
                        inputSHA256=result["inputSHA256"],
                    )
                notify("voice-progress")

        embed_ready(-1)
        for index in sorted({index for windows in chosen.values() for index, _start, _end in windows}):
            if (rebuilt := group_audio(index)) is not None:
                with rebuilt as (stream, _frames, sha):
                    if sha != digests[index]:
                        raise ValueError("Voice audio changed during extraction")
                    for speaker in speakers:
                        for n, (at, start, end) in enumerate(chosen[speaker]):
                            if at == index:
                                audio[speaker, n] = read_window(stream, start, end)
            embed_ready(index)
    notify("voices-complete")


def request(message, notify):
    try:
        tracks = message["tracks"]
        if not isinstance(tracks, list) or not 1 <= len(tracks) <= 64:
            raise ValueError("Invalid voice track count")
        run(tracks, message["options"], notify)
    except Exception as error:
        notify("voices-error", message=str(error))
