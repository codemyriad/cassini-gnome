# SPDX-License-Identifier: GPL-3.0-or-later
"""S-11 hand-authored shared vectors, byte pins, ABI and long derived IDs."""

import hashlib
import json
import struct
import wave
from pathlib import Path

import pytest

from cassini_viewer import diarization as d
from cassini_viewer.library_schema import valid
from cassini_viewer.meeting_store import read_metadata
from cassini_viewer.recording_session import RecordingSession
from cassini_viewer.strings import text

HERE = Path(__file__).parent / "ux/diarization"
CASES = json.loads((HERE / "assign-vectors.json").read_bytes())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_shared_assignment_case(case, tmp_path):
    operation = case["operation"]
    if operation in ("assign", "applyGroup"):
        native, candidates = d.candidate_ids(case["groupId"], case["words"], case["turns"])
        actual = dict(nativeSpeakers=native, candidateWordSpeakerIds=candidates)
        if case["expected"].get("repeatWithReversedTurns"):
            assert d.candidate_ids(case["groupId"], case["words"], list(reversed(case["turns"]))) == (
                native,
                candidates,
            )
            actual["repeatWithReversedTurns"] = True
        if operation == "applyGroup":
            actual = d.apply_group(case["groupId"], case["words"], case["turns"])
            if "displayOrder" in case["expected"]:
                order = d.speaking_order(case["words"], actual["wordSpeakerIds"])
                actual.update(
                    displayOrder=order,
                    displayNames={s: text("common.speaker.unnamed", n=i + 1) for i, s in enumerate(order)},
                )
    elif operation == "normalize":
        actual = d.normalize_turns(case["rawTurns"], case["durationMs"])
    elif operation == "split":
        actual = d.split_segments(case["segments"], case["groupId"], case["turns"])
    elif operation == "groupTracks":
        actual = d.group_tracks(case["tracks"], origin=case["origin"])
    elif operation == "rejoin":
        np = pytest.importorskip("numpy")
        from cassini_viewer.diarization_audio import rejoin

        tracks = []
        for index, track in enumerate(case["tracks"]):
            path = tmp_path / f"track-{index}.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, track["sampleRate"], 0, "NONE", ""))
                audio.writeframes(struct.pack("<" + "h" * len(track["pcm"]), *track["pcm"]))
            tracks.append(dict(track, path=str(path)))
        with rejoin(tracks, scratch=tmp_path) as (stream, frames, _sha):
            actual = dict(
                sampleRate=16000,
                samples=np.fromfile(stream, dtype="<f4").tolist(),
                durationMs=frames * 1000 // 16000,
                invokeModel=frames >= 3200,
                turns=[],
            )
    else:
        pytest.fail("Unimplemented shared operation: " + operation)
    assert actual == case["expected"]


def test_shared_snapshot_bytes_and_frozen_voice_assignment():
    for line in (HERE / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest
    golden = json.loads((HERE / "golden.json").read_bytes())
    expected = golden["expected"]
    actual = d.apply_group("others", expected["words"], expected["turns"])
    assert actual["wordSpeakerIds"] == [word["speakerId"] for word in expected["words"]]
    assert actual["voiceIds"] == expected["voiceIds"]


def test_approved_long_group_ids_keep_the_required_voice_suffix(tmp_path):
    session = RecordingSession(tmp_path)
    try:
        metadata = read_metadata(session.directory)
        group = "track-" + "x" * 194
        words = [dict(startMs=10, endMs=90, text="one"), dict(startMs=110, endMs=190, text="two")]
        turns = [dict(startMs=0, endMs=100, speaker=0), dict(startMs=100, endMs=200, speaker=1)]
        result = d.apply_group(group, words, turns)
        assert result["voiceIds"] == [group + "~1", group + "~2"]
        metadata["speakers"] = [
            dict(id=s, name=None, provenance="unnamed", colourIndex=i) for i, s in enumerate(result["voiceIds"])
        ]
        assert valid(metadata)
        metadata["speakers"][0]["id"] = "x" * 256
        assert valid(metadata)
        metadata["speakers"][0]["id"] += "x"
        assert not valid(metadata)
    finally:
        session.finish()
