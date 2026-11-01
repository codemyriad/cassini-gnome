# SPDX-License-Identifier: GPL-3.0-or-later
"""Literal shared seams through real PCM readers, not the fixture oracle."""

import array
import json
from pathlib import Path

import pytest

from cassini_talk.source_ownership import FrameMask, windows

CASES = json.loads((Path(__file__).parents[1] / "browser-protocol/conformance/multistream/ownership.json").read_text())[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_literal_half_open_ownership(case):
    for track in case["tracks"]:
        intervals = windows(track["kind"], case["events"])
        selected = FrameMask(intervals, 1000).sections(track["startMs"], track["endMs"] - track["startMs"])
        assert [[left + track["startMs"], right + track["startMs"]] for left, right in selected] == case["expected"][
            track["id"]
        ]
    for point in case["ownershipAt"]:
        owners = []
        for track in case["tracks"]:
            intervals = windows(track["kind"], case["events"])
            if track["startMs"] <= point["atMs"] < track["endMs"] and (
                intervals is None
                or any(left <= point["atMs"] and (right is None or point["atMs"] < right) for left, right in intervals)
            ):
                owners.append(track["id"])
        assert owners == point["ids"]


def test_fractional_boundaries_use_sample_instants_and_stereo_channels_together():
    mask = FrameMask(((1000 / 3, 2000 / 3),), 48000)
    # An exact decimal fractional-ms boundary is converted once, with ceil on both ends.
    sections = mask.sections(0, 48000)
    assert sections == [(16000, 32000)]
    samples = array.array("h", [123, -456] * 48000)
    data = samples.tobytes()
    result = array.array("h", mask.pcm(data, 0, 4))
    assert result[:32000] == array.array("h", [0] * 32000)
    assert result[32000:64000] == samples[32000:64000]
    assert not any(result[64000:]) and samples.tobytes() == data
    assert FrameMask((), 48000).pcm(data[:16], 0, 4) == bytes(16)
    assert FrameMask(None, 48000).pcm(data, 0, 4) is data


@pytest.mark.parametrize("kind", ["microphone", "local"])
def test_browser_mic_is_not_masked_by_others_source_changes(kind):
    assert windows(kind, CASES[0]["events"]) is None
    events = [dict(type="selection", atMs=100, me=False, others=True, reason="user")]
    assert windows(kind, events) == ((0, 100),)
    assert windows("application", CASES[1]["events"]) == ((0, 3000), (6000, None))
    assert windows("remote", []) is None


def meeting_fixture(root, case):
    import numpy as np

    from cassini_talk.library import LibraryScanner
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(root, source="chromium")
    for track in case["tracks"]:
        session.add_track(track["id"], track["id"], track["kind"], 48000, 1)
        for offset in range(track["startMs"], track["endMs"], 1000):
            count = min(1000, track["endMs"] - offset) * 48
            session.append(track["id"], np.full(count, 8192, dtype="<i2").tobytes(), offset)
    session.finish()
    session.manifest["events"] = case["events"]
    session.checkpoint()
    return LibraryScanner(root).scan().meetings[0]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_readers_mask_actual_overlapping_wavs_for_export_speech_and_diarization(tmp_path, case):
    np = pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from cassini_talk.diarization_audio import rejoin
    from cassini_talk.export_audio import mixed_pcm, track_pcm
    from cassini_talk.speech_pipeline import audio_chunks, audio_segments
    from cassini_talk.transcription import prepare_tracks

    meeting = meeting_fixture(tmp_path, case)
    before = {track.path: track.path.read_bytes() for track in meeting.tracks}
    jobs, _speakers = prepare_tracks(meeting)
    expected_mix = np.zeros(10000 * 48, dtype="<f4")
    expected_speech = np.zeros(10000 * 16, dtype="<f4")
    for track, job in zip(meeting.tracks, jobs, strict=True):
        expected = np.zeros(10000 * 48, dtype="<f4")
        for left, right in case["expected"][track.id]:
            expected[left * 48 : right * 48] = 0.25
            expected_mix[left * 48 : right * 48] += 0.25
            expected_speech[left * 16 : right * 16] += 0.25
        actual = np.concatenate(list(track_pcm(track)))
        assert np.array_equal(actual, expected[: len(actual)])
        speech = np.concatenate(list(audio_chunks(job)))
        start = int(job["offsetMs"] * 16)
        full = np.pad(speech, (start, len(expected_speech) - start - len(speech)))
        wanted = expected[::3]
        # FIR edge context can change the first/last samples of a physical WAV;
        # outside ownership is exact zero, including both seam sample instants.
        assert np.all(full[wanted == 0] == 0)
        segment = dict(startSample=0, endSample=len(speech))
        assert np.array_equal(next(audio_segments(job, [segment])), speech)
    mixed = np.concatenate(list(mixed_pcm(meeting.tracks)))
    assert np.array_equal(mixed, expected_mix)
    assert np.max(mixed) == 0.25, "two physically overlapping sources must never be summed"
    with rejoin(jobs, scratch=tmp_path / "scratch") as (stream, frames, _sha):
        joined = np.frombuffer(stream.read(), dtype="<f4")
        assert frames == len(expected_speech)
        for at in (3000, 6000):
            assert np.allclose(
                joined[at * 16 - 16 : at * 16 + 16], expected_speech[at * 16 - 16 : at * 16 + 16], atol=0.0001
            )
    assert all(path.read_bytes() == data for path, data in before.items())


def test_unknown_offset_is_reported_and_never_used_as_alignment_zero(tmp_path):
    pytest.importorskip("numpy")
    from cassini_talk.library import LibraryScanner
    from cassini_talk.transcription import prepare_tracks

    meeting = meeting_fixture(tmp_path, CASES[0])
    manifest = meeting.directory / "session.json"
    value = json.loads(manifest.read_text())
    value["tracks"][0]["startOffsetMs"] = None
    manifest.write_text(json.dumps(value))
    meeting = LibraryScanner(tmp_path).scan().meetings[0]
    assert any(d.code == "ownership-start-offset-unknown" for d in meeting.diagnostics)
    jobs, _speakers = prepare_tracks(meeting)
    assert jobs[0]["offsetMs"] is None and "ownershipWindows" not in jobs[0]


def test_real_folder_seek_obeys_the_same_half_open_source_ownership(tmp_path):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from test_folder_player import capture

    meeting = meeting_fixture(tmp_path, CASES[0])
    samples, _pts, _duration = capture(meeting.tracks, seek=5900)
    # A seam cannot add the two .25 sources, including after an actual seek.
    assert max(samples) <= 8193
    assert len(samples) > 4 * 48000


@pytest.mark.parametrize("bad", [[(0, None), (1, 2)], [(2, 1)], [(True, 3)], [(0, float("nan"))], [(2, 3), (1, 2)]])
def test_worker_masks_reject_malformed_or_unordered_windows(bad):
    with pytest.raises(ValueError, match="ownership"):
        FrameMask(bad, 16000)
