# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex #181: an empty gap (durationMs 0) is no range, so a later real gap still counts everywhere.

One remote 5 s track: an empty gap at 1000 ms, a real gap at 2000-3000 ms
(digital zero, as a writer pads it) and ownership ending at 4000 ms. Each
reader must keep the gap and the unowned time silent or absent after the
empty gap: presence, the scanner, the write-down's G10 ranges, the published
mix and its x track map, export, playback and the diarization rejoin.
"""

import json
import shutil
import wave

import pytest

np = pytest.importorskip("numpy")

RATE = 48000
GAPS = [
    dict(atMs=1000, durationMs=0, frames=0, kind="jump"),
    dict(atMs=2000, durationMs=1000, frames=48000, kind="jump"),
]
CASE = dict(track=dict(startOffsetMs=0, sampleRate=RATE, frames=5 * RATE, gaps=GAPS), ownership=[[0, 4000]])
# Owned presence in 16 kHz session samples; without ownership it runs to 5000 ms.
OWNED = [[0, 32000], [48000, 64000]]
PRESENT = [[0, 32000], [48000, 80000]]
SILENT_MS = [(2000, 3000), (4000, 5000)]
SOUNDING_MS = [(0, 2000), (3000, 4000)]
EDGE_MS = 10  # beyond the 97-tap FIR's 1 ms context


@pytest.fixture(autouse=True)
def release(tmp_path):
    yield
    shutil.rmtree(tmp_path.absolute(), ignore_errors=True)


def tone():
    """The stored PCM: a -15 dBFS 440 Hz tone, digital zero inside the real gap."""
    t = np.arange(5 * RATE) / RATE
    pcm = np.round(0.18 * 32767 * np.sin(2 * np.pi * 440 * t)).astype("<i2")
    pcm[2 * RATE : 3 * RATE] = 0
    return pcm


def masked(pcm):
    """What every reader must give: the tone only in owned present time."""
    result = pcm.copy()
    for left, right in SILENT_MS:
        result[left * RATE // 1000 : right * RATE // 1000] = 0
    return result


@pytest.fixture
def meeting(tmp_path):
    from test_d63_vectors import stored_session

    from cassini_talk.library import LibraryScanner

    directory = stored_session(tmp_path / "library", CASE)
    with wave.open(str(directory / "track-t.wav"), "wb") as stream:
        stream.setparams((1, 2, RATE, 0, "NONE", ""))
        stream.writeframes(tone().tobytes())
    (meeting,) = LibraryScanner(tmp_path / "library").scan().meetings
    return meeting


def silent_and_sounding(values, rate, label):
    for left, right in SILENT_MS:
        part = values[(left + EDGE_MS) * rate // 1000 : (right - EDGE_MS) * rate // 1000]
        assert len(part) and not np.any(part), f"{label} sounds at {left}..{right}"
    for left, right in SOUNDING_MS:
        part = values[(left + EDGE_MS) * rate // 1000 : (right - EDGE_MS) * rate // 1000]
        assert np.max(np.abs(part)) > 0.1 * np.max(np.abs(values)), f"{label} quiet at {left}..{right}"


def test_presence_and_the_scanner_keep_the_later_gap(meeting):
    from cassini_talk.source_ownership import presence, samples
    from cassini_talk.transcription import session_presence

    (track,) = meeting.tracks
    assert track.gaps == GAPS
    assert track.ownership_windows == ((0, 4000),)
    found = presence(CASE["track"])
    assert (samples(found.present), found.unknown, found.complete) == (PRESENT, (), True)
    assert samples(presence(CASE["track"], track.ownership_windows).present) == OWNED
    assert session_presence(track) == OWNED


def test_the_write_down_measures_and_windows_only_present_time(meeting):
    from cassini_talk import recognition_fallback as fallback
    from cassini_talk.speech_pipeline import audio_chunks
    from cassini_talk.transcription import prepare_tracks

    (job,), _speakers = prepare_tracks(meeting)
    assert job["sessionPresence"] == OWNED
    assert fallback.track_ranges(job["sessionPresence"], job["offsetMs"]) == OWNED
    meter = fallback.ActivityMeter(fallback.track_ranges(job["sessionPresence"], job["offsetMs"]))
    for _block in meter.wrap(audio_chunks(job)):
        pass
    windows = fallback.active_windows(meter.samples, meter.active(), meter.presence)
    assert windows, "the tone is active"
    for left, right in windows:
        assert any(a <= left and right <= b for a, b in OWNED), f"window {left}..{right} leaves presence"
    speech = np.concatenate(list(audio_chunks(job)))
    silent_and_sounding(speech, 16000, "speech input")


def test_the_published_mix_and_its_track_map_keep_the_gap_and_ownership(meeting):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from cassini_talk.library_v2 import mixdown
    from cassini_talk.source_ownership import presence, samples

    session = json.loads((meeting.directory / "session.json").read_text())
    timeline = mixdown.session_spans(meeting.directory, session)
    published = np.concatenate(list(mixdown.mixed(timeline.spans, mixdown.total_samples(timeline.spans))))
    wanted = masked(tone()).astype(np.float32) / 32768
    assert np.array_equal(published, wanted[: len(published)]) and len(published) == len(wanted)
    (entry,) = timeline.tracks
    assert entry["gaps"] == GAPS
    assert samples(presence(entry).present) == PRESENT
    assert samples(presence(entry, meeting.tracks[0].ownership_windows).present) == OWNED


def test_export_and_playback_mask_after_the_empty_gap(meeting):
    gi = pytest.importorskip("gi")
    gi.require_version("Gst", "1.0")
    from test_folder_player import capture

    from cassini_talk.export_audio import mixed_pcm, track_pcm

    wanted = masked(tone())
    exported = np.concatenate(list(track_pcm(meeting.tracks[0])))
    assert np.array_equal(exported, (wanted.astype(np.float32) / 32768)[: len(exported)])
    assert np.array_equal(np.concatenate(list(mixed_pcm(meeting.tracks))), exported)
    played, _pts, _duration = capture(meeting.tracks)
    played = np.frombuffer(played.tobytes(), dtype="<i2")
    silent_and_sounding(played, RATE, "playback")
    assert np.array_equal(played[: len(wanted)], wanted[: len(played)])


def test_the_rejoin_is_silent_in_the_gap_and_unowned_time(meeting, tmp_path):
    from cassini_talk.diarization_audio import rejoin
    from cassini_talk.transcription import prepare_tracks

    jobs, _speakers = prepare_tracks(meeting)
    with rejoin(jobs, scratch=tmp_path / "scratch") as (stream, frames, _digest):
        joined = np.frombuffer(stream.read(), dtype="<f4")
    assert frames == 5 * 16000
    silent_and_sounding(joined, 16000, "rejoin")
