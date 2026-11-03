# SPDX-License-Identifier: GPL-3.0-or-later
"""D63 #1 and #3 shared vectors (protocol conformance/multistream) on GNOME's own functions.

fallback.json feeds the pure decision the child runs. writedown.json's stored
tracks are written as real sessions, scanned and prepared as the queue does,
and their words go through the child's VAD path; only inference is faked.
"""

import json
import math
import wave
from pathlib import Path

import pytest

pytest.importorskip("numpy")  # Core/Whistle-light profiles need no PCM worker dependencies.

ROOT = Path(__file__).parents[1] / "browser-protocol/conformance/multistream"
FALLBACK = json.loads((ROOT / "fallback.json").read_text())
WRITEDOWN = json.loads((ROOT / "writedown.json").read_text())


@pytest.mark.parametrize("case", FALLBACK["cases"], ids=[case["name"] for case in FALLBACK["cases"]])
def test_fallback_decisions_follow_the_shared_cases(case):
    from cassini_talk import recognition_fallback as fallback

    verdict, entries = fallback.decide(case["tracks"])
    assert verdict == case["expected"]["fires"]
    assert entries == case["expected"]["entries"]


def test_the_gate_compares_whole_frames():
    from cassini_talk import recognition_fallback as fallback

    assert fallback.holds(469) and not fallback.holds(468)
    assert fallback.fires(469, fallback.FEW_WORDS - 1) and not fallback.fires(469, fallback.FEW_WORDS)


def stored_session(root, case):
    """One remote track with the case's stored facts, its ownership as selection events."""
    track = dict(case["track"])
    start, rate, frames = track["startOffsetMs"], track["sampleRate"], track["frames"]
    end = start + frames * 1000 / rate
    directory = root / "Recording-05200000-0000-4000-8000-0000000000d3"
    directory.mkdir(parents=True)
    with wave.open(str(directory / "track-t.wav"), "wb") as stream:
        stream.setparams((1, 2, rate, frames, "NONE", ""))
        stream.writeframes((b"\x00\x10\x00\xf0") * (frames // 2) + b"\x00\x10" * (frames % 2))
    ownership = case.get("ownership")
    events = [
        dict(type="selection", atMs=0, me=True, others=ownership is None or ownership[0][0] == 0, reason="start"),
        dict(type="othersSource", atMs=0, source="browser", reason="start"),
    ]
    for left, right in ownership or []:
        if left > 0:
            events.append(dict(type="selection", atMs=left, me=True, others=True, reason="user"))
        events.append(dict(type="selection", atMs=right, me=True, others=False, reason="user"))
    events.append(dict(type="end", atMs=math.ceil(end), reason="user"))
    track.update(
        id="t",
        kind="remote",
        label="Guest",
        labelSource="browser",
        channels=1,
        file="track-t.wav",
        endOffsetMs=end,
        ended=True,
        sourceTrackId="x",
    )
    session = dict(
        version=1,
        id=directory.name.removeprefix("Recording-"),
        createdAt="2026-11-03T10:00:00Z",
        title="Write-down",
        source="chromium",
        state="completed",
        tracks=[track],
        events=events,
    )
    (directory / "session.json").write_text(json.dumps(session))
    return directory


def written(tmp_path, monkeypatch, case):
    """The words of `case` the child sends for writing, recognised in the track's first VAD segment."""
    from corpus_support import Mute, label, scripted
    from test_recognition_fallback import detector

    from cassini_talk import speech_pipeline as pipeline
    from cassini_talk.library import LibraryScanner
    from cassini_talk.transcription import prepare_tracks

    stored_session(tmp_path, case)
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    (job,), _speakers = prepare_tracks(meeting)
    monkeypatch.setattr(pipeline, "SpeechDetector", detector(1.0))
    scripted(monkeypatch, {job["id"]: [word["ms"] for word in case["words"]]})
    events = []
    pipeline.process_tracks(Mute(), [job], None, lambda kind, **values: events.append(dict(type=kind, **values)))
    texts = {word["text"] for event in events if event["type"] == "progress" for word in event["words"]}
    return {tuple(word["ms"]): label(job["id"], word["ms"]) in texts for word in case["words"]}


@pytest.mark.parametrize("case", WRITEDOWN["keep"], ids=[case["name"] for case in WRITEDOWN["keep"]])
def test_written_words_follow_the_shared_keep_cases(tmp_path, monkeypatch, case):
    pytest.importorskip("numpy")
    assert written(tmp_path, monkeypatch, case) == {tuple(word["ms"]): word["keep"] for word in case["words"]}
