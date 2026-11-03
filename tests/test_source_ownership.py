# SPDX-License-Identifier: GPL-3.0-or-later
"""Literal shared seams through real PCM readers, not the fixture oracle."""

import array
import json
import shutil
from pathlib import Path

import pytest

from cassini_talk.source_ownership import FrameMask, windows

CASES = json.loads((Path(__file__).parents[1] / "browser-protocol/conformance/multistream/ownership.json").read_text())[
    "cases"
]
RESUME = json.loads(
    (Path(__file__).parents[1] / "browser-protocol/conformance/control/resume-capture-vectors.json").read_text()
)


@pytest.fixture(autouse=True)
def release(tmp_path):
    """Later suite tests need their own free space on a small test tmpfs: drop these WAVs."""
    yield
    shutil.rmtree(tmp_path.absolute(), ignore_errors=True)


def resume_seam(name):
    """R67/D52.4: a resume seam in ownership.json's shape, so it takes the same path.

    The resume vectors list the others' owners only; the native microphone
    keeps recording across the seam and owns every instant of its own track.
    """
    seam = RESUME[name]
    return dict(
        name=name,
        events=seam["events"],
        tracks=seam["tracks"],
        expected=seam["expectedWindows"],
        ownershipAt=[
            dict(
                atMs=row["atMs"],
                ids=sorted(["microphone", *row["others"]], key=[t["id"] for t in seam["tracks"]].index),
            )
            for row in seam["ownershipAt"]
        ],
    )


@pytest.mark.parametrize(
    "case",
    [*CASES, resume_seam("seam"), resume_seam("lostSeam")],
    ids=lambda case: case["name"],
)
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


def meeting_fixture(root, case, rate=48000):
    import numpy as np

    from cassini_talk.library import LibraryScanner
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(root, source="chromium")
    for track in case["tracks"]:
        session.add_track(track["id"], track["id"], track["kind"], rate, 1)
        for offset in range(track["startMs"], track["endMs"], 1000):
            count = min(1000, track["endMs"] - offset) * rate // 1000
            session.append(track["id"], np.full(count, 8192, dtype="<i2").tobytes(), offset)
    session.finish()
    session.manifest["events"] = case["events"]
    session.checkpoint()
    return LibraryScanner(root).scan().meetings[0]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_readers_mask_actual_overlapping_wavs_for_export_speech_diarization_and_publication(tmp_path, case):
    np = pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from cassini_talk.diarization_audio import rejoin
    from cassini_talk.export_audio import mixed_pcm, track_pcm
    from cassini_talk.library_v2 import mixdown
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
    # The published Library mix (finalize) masks each span as export does.
    session = json.loads((meeting.directory / "session.json").read_text())
    timeline = mixdown.session_spans(meeting.directory, session)
    published = np.concatenate(list(mixdown.mixed(timeline.spans, mixdown.total_samples(timeline.spans))))
    assert np.array_equal(published, mixed[: len(published)]) and not np.any(mixed[len(published) :])
    assert np.max(published) == 0.25, "the published mix must never sum an overlap"
    with rejoin(jobs, scratch=tmp_path / "scratch") as (stream, frames, _sha):
        joined = np.frombuffer(stream.read(), dtype="<f4")
        assert frames == len(expected_speech)
        for at in (3000, 6000):
            assert np.allclose(
                joined[at * 16 - 16 : at * 16 + 16], expected_speech[at * 16 - 16 : at * 16 + 16], atol=0.0001
            )
    assert all(path.read_bytes() == data for path, data in before.items())


def published_and_exported(meeting):
    np = pytest.importorskip("numpy")
    from cassini_talk.export_audio import mixed_pcm
    from cassini_talk.exporting import assumed_zero
    from cassini_talk.library_v2 import mixdown

    session = json.loads((meeting.directory / "session.json").read_text())
    timeline = mixdown.session_spans(meeting.directory, session)
    published = np.concatenate(list(mixdown.mixed(timeline.spans, mixdown.total_samples(timeline.spans))))
    return published, np.concatenate(list(mixed_pcm(assumed_zero(meeting.tracks)))), timeline


def test_published_mix_masks_a_16_khz_span_before_resampling_as_export_does(tmp_path):
    np = pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")

    meeting = meeting_fixture(tmp_path, CASES[1], rate=16000)
    published, exported, _timeline = published_and_exported(meeting)
    # The resampler sees the same cut at every ownership edge, so the mixes agree exactly.
    common = min(len(published), len(exported))
    assert np.array_equal(published[:common], exported[:common])
    assert not np.any(published[common:]) and not np.any(exported[common:])


def rotated_with_unplaced_span(root, frames):
    """CASES[1] with "after" rotated into a second file whose offset GNOME never set."""
    import wave

    import numpy as np

    from cassini_talk.library import LibraryScanner

    meeting = meeting_fixture(root, CASES[1])
    manifest = meeting.directory / "session.json"
    value = json.loads(manifest.read_text())
    (after,) = (track for track in value["tracks"] if track["id"] == "after")
    with wave.open(str(meeting.directory / "track-after-2.wav"), "wb") as second:
        second.setnchannels(1)
        second.setsampwidth(2)
        second.setframerate(48000)
        second.writeframes(np.full(frames, 8192, dtype="<i2").tobytes())
    first = {key: after[key] for key in ("file", "frames", "sampleRate", "channels", "startOffsetMs") if key in after}
    after["spans"] = [first, dict(first, file="track-after-2.wav", frames=frames, startOffsetMs=None)]
    manifest.write_text(json.dumps(value))
    return LibraryScanner(root).scan().meetings[0]


@pytest.mark.parametrize("frames", [0, 48000], ids=["empty", "audio"])
def test_an_unplaced_span_never_unmasks_its_placed_sibling_in_any_reader(tmp_path, frames):
    np = pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from test_folder_player import capture

    from cassini_talk.diarization_audio import rejoin
    from cassini_talk.folder_player import FolderPlayer
    from cassini_talk.speech_pipeline import audio_chunks
    from cassini_talk.transcription import prepare_tracks

    meeting = rotated_with_unplaced_span(tmp_path, frames)
    (after,) = (track for track in meeting.tracks if track.id == "after")
    # The placed span sounds from 5000 ms but owns only [6000, 10000): the system track owns [3000, 6000).
    owned = ((0, 3000), (6000, None))
    placed = [span for span in after.spans or [after] if span.start_offset_ms is not None]
    assert [span.ownership_windows for span in placed] == [owned]
    # An unplaced span with audio is mixed at its assumed zero (D19) and never clipped there.
    assert [span.ownership_windows for span in after.spans if span.start_offset_ms is None] == [None] * bool(frames)
    expected = np.full(10000 * 48, 0.25, dtype="<f4")
    expected[:frames] += 0.25
    published, exported, timeline = published_and_exported(meeting)
    assert [(span.offset_assumed, span.ownership) for span in timeline.spans if span.track == "after"] == [
        (False, owned)
    ] + [(True, None)] * bool(frames)
    assert np.array_equal(exported, expected)
    assert np.array_equal(published, expected)
    # Playback needs every offset; the empty span no longer blocks it.
    assert FolderPlayer.can_play(meeting) is not frames
    if not frames:
        samples, _pts, _duration = capture(meeting.tracks)
        assert max(samples) <= 8193 and len(samples) >= 10000 * 48
    jobs, _speakers = prepare_tracks(meeting)
    (job,) = (job for job in jobs if job.get("captureTrackId") == "after")
    speech = np.concatenate(list(audio_chunks(job)))
    speech = np.pad(speech, (int((job["offsetMs"] or 0) * 16), 0))
    assert not np.any(speech[5000 * 16 : 6000 * 16])
    assert np.allclose(speech[6100 * 16 : 9900 * 16], 0.25, atol=0.0001)
    assert np.allclose(speech[100 * 16 : 900 * 16], 0.25 if frames else 0, atol=0.0001)
    with rejoin(jobs, scratch=tmp_path / "scratch") as (stream, _frames, _sha):
        joined = np.frombuffer(stream.read(), dtype="<f4")
    assert np.allclose(joined[100 * 16 : 900 * 16], 0.5 if frames else 0.25, atol=0.0001)
    # Masked before its FIR, the rotated span starts at 6000 ms with the FIR's step response.
    assert np.allclose(joined[1100 * 16 : 5990 * 16], 0.25, atol=0.0001)
    assert np.allclose(joined[6010 * 16 : 9900 * 16], 0.25, atol=0.0001)
    assert np.max(joined[1100 * 16 : 9900 * 16]) < 0.3, "a seam is never doubled"


def anchored_meeting(root, anchors, edge, owned_first):
    """One 16 kHz remote track, silent for 16000 source frames then loud, with clock anchors.

    The remote side owns [0, edge) when `owned_first`, else [edge, end).
    """
    import numpy as np

    from cassini_talk.library import LibraryScanner
    from cassini_talk.recording_session import RecordingSession

    session = RecordingSession(root, source="chromium")
    session.add_track("remote", "remote", "remote", 16000, 1)
    session.append("remote", np.zeros(16000, dtype="<i2").tobytes(), 0)
    session.append("remote", np.full(16000, 8192, dtype="<i2").tobytes(), 1000)
    session.finish()
    start = dict(atMs=0, type="selection", me=True, others=True, reason="start")
    if owned_first:
        changes = [dict(source="browser", reason="start", atMs=0), dict(source="system", reason="lost", atMs=edge)]
    else:
        changes = [
            dict(source="system", reason="missing", atMs=0),
            dict(source="browser", reason="callConnected", atMs=edge),
        ]
    session.manifest["events"] = [start, *(dict(change, type="othersSource") for change in changes)]
    session.manifest["tracks"][0]["clockAnchors"] = anchors
    session.checkpoint()
    return LibraryScanner(root).scan().meetings[0]


@pytest.mark.parametrize(
    "anchors,edge",
    [([[0, 0], [32000, 2000000000]], 1000), ([[0, 0], [16000, 1005000000], [32000, 2000000000]], 1005)],
    ids=["nominal", "warped"],
)
def test_published_mix_masks_a_clock_mapped_span_before_resampling(tmp_path, anchors, edge):
    np = pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from dataclasses import replace

    from cassini_talk.library_v2 import mixdown

    # Source frame 16000, the first loud one, sounds exactly at the ownership edge.
    meeting = anchored_meeting(tmp_path / "silent-owned", anchors, edge, owned_first=True)
    published, exported, timeline = published_and_exported(meeting)
    assert [span.ownership for span in timeline.spans] == [((0, edge),)]
    assert np.max(np.abs(published)) == 0, "unowned audio must not ring through the resampler into owned time"
    # Export interpolates the native samples only after masking them through the same clock map.
    assert not np.any(exported)
    # Owned audio itself is unchanged.
    meeting = anchored_meeting(tmp_path / "loud-owned", anchors, edge, owned_first=False)
    published, exported, timeline = published_and_exported(meeting)
    spans = [replace(span, ownership=None) for span in timeline.spans]
    whole = np.concatenate(list(mixdown.mixed(spans, mixdown.total_samples(spans))))
    assert not np.any(published[: edge * 48]) and np.max(np.abs(whole[: edge * 48])) > 0.001
    assert np.array_equal(published[edge * 48 :], whole[edge * 48 :])
    assert np.max(published) > 0.2
    if edge == 1000:
        common = min(len(published), len(exported))
        assert np.array_equal(published[:common], exported[:common])
        assert not np.any(published[common:]) and not np.any(exported[common:])


def rotated_16_khz(root, owned_first, first_ms):
    """One 16 kHz remote track rotated into two files: 2000 ms at `first_ms`, then 1000 loud ms at 3000 ms.

    The remote side owns [0, 1000) when `owned_first`, else [1000, end). The
    first file is silent where it is owned and loud where it is not, so any
    sound in [0, 2000) ms is a leak; the second file is owned only in the latter case.
    """
    import wave

    import numpy as np

    from cassini_talk.library import LibraryScanner
    from cassini_talk.recording_session import RecordingSession

    loud, silent = np.full(16000, 8192, dtype="<i2"), np.zeros(16000, dtype="<i2")
    session = RecordingSession(root, source="chromium")
    session.add_track("remote", "remote", "remote", 16000, 1)
    session.append("remote", np.concatenate((silent, loud) if owned_first else (loud, silent)).tobytes(), 0)
    session.finish()
    start = dict(atMs=0, type="selection", me=True, others=True, reason="start")
    if owned_first:
        changes = [dict(source="browser", reason="start", atMs=0), dict(source="system", reason="lost", atMs=1000)]
    else:
        changes = [
            dict(source="system", reason="missing", atMs=0),
            dict(source="browser", reason="callConnected", atMs=1000),
        ]
    session.manifest["events"] = [start, *(dict(change, type="othersSource") for change in changes)]
    session.checkpoint()
    meeting = LibraryScanner(root).scan().meetings[0]
    manifest = meeting.directory / "session.json"
    value = json.loads(manifest.read_text())
    (track,) = value["tracks"]
    with wave.open(str(meeting.directory / "track-remote-2.wav"), "wb") as second:
        second.setnchannels(1)
        second.setsampwidth(2)
        second.setframerate(16000)
        second.writeframes(loud.tobytes())
    first = {key: track[key] for key in ("file", "frames", "sampleRate", "channels") if key in track}
    track["spans"] = [
        dict(first, startOffsetMs=first_ms),
        dict(first, file="track-remote-2.wav", frames=16000, startOffsetMs=3000),
    ]
    manifest.write_text(json.dumps(value))
    return LibraryScanner(root).scan().meetings[0]


@pytest.mark.parametrize(
    "owned_first,first_ms", [(True, 0), (False, 0.0209), (False, 0.0417)], ids=["right", "left-1", "left-2"]
)
def test_rotated_16_khz_spans_mask_native_samples_before_interpolating(tmp_path, owned_first, first_ms):
    np = pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from test_folder_player import capture

    meeting = rotated_16_khz(tmp_path, owned_first, first_ms)
    (track,) = meeting.tracks
    owned = ((0, 1000),) if owned_first else ((1000, None),)
    assert [span.ownership_windows for span in track.spans] == [owned, owned]
    published, exported, _timeline = published_and_exported(meeting)
    played, _pts, _duration = capture(meeting.tracks)
    played = np.asarray(played, dtype=np.float64) / 32768
    for name, mix in (("published", published), ("exported", exported), ("played", played)):
        # The owned sample next to the edge used to take a share of the first unowned native sample.
        assert not np.any(mix[: 2000 * 48]), f"{name}: unowned audio leaks into owned time"
        if owned_first:
            assert not np.any(mix), name
        else:
            assert np.allclose(mix[3010 * 48 : 3990 * 48], 0.25, atol=0.0001), f"{name}: owned audio is kept"


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


def test_compacted_opus_ownership_is_exact_after_decoding_and_resampling(tmp_path):
    np = pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from cassini_talk.export_audio import encode_blocks, track_pcm
    from cassini_talk.library import CaptureTrack
    from cassini_talk.speech_pipeline import audio_chunks

    (tmp_path / "tracks").mkdir()
    path = tmp_path / "tracks" / "voice.opus"
    signal = np.sin(np.arange(48000 * 2) * (2 * np.pi * 440 / 48000)).astype("<f4") * 0.25
    encode_blocks(
        (array.array("f", block) for block in (signal[:48000], signal[48000:])),
        path,
        dict(bitrateBps=64000, cutoffHz=20000),
    )
    track = CaptureTrack(
        path,
        96000,
        48000,
        1000,
        True,
        kind="remote",
        codec="opus",
        decoded_frames=96000,
        ownership_windows=((1500.01, 2000.01),),
    )
    before = path.read_bytes()
    exported = np.concatenate(list(track_pcm(track)))
    assert np.all(exported[:72001] == 0) and np.all(exported[96001:] == 0)
    assert np.max(np.abs(exported[75000:90000])) > 0.1
    job = dict(
        id="voice",
        path=str(path),
        codec="opus",
        frames=96000,
        sampleRate=48000,
        offsetMs=1000,
        ownershipWindows=[[1500.01, 2000.01]],
    )
    speech = np.concatenate(list(audio_chunks(job)))
    assert np.all(speech[:8001] == 0) and np.all(speech[16001:] == 0)
    assert np.max(np.abs(speech[9000:15000])) > 0.1
    assert path.read_bytes() == before
