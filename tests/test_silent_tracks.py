# SPDX-License-Identifier: GPL-3.0-or-later
"""D56 (R75 Q1): tracks with fewer than 32 non-zero 32 ms frames are left out of the writing-down.

The boundary tests write tiny PCM16 WAVs with exact sample values and need no NumPy.
The end-to-end tests run the real speech pipeline, diarization worker and voice step
in this process; only Silero, the recognizer, the diarizer and the speaker
extractor are faked.
"""

import array
import hashlib
import json
import random
import struct
import sys
import threading
import types

import pytest

from cassini_talk.library import CaptureTrack, LibraryScanner
from cassini_talk.recording_session import RecordingSession
from cassini_talk.silent_tracks import frame_samples, nonzero_frames, silent


def wav(path, rate, channels, samples):
    """PCM16 WAV of interleaved integer `samples`."""
    data = array.array("h", samples)
    if sys.byteorder != "little":
        data.byteswap()
    raw = data.tobytes()
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        *(b"RIFF", 36 + len(raw), b"WAVE", b"fmt ", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16),
        *(b"data", len(raw)),
    )
    path.write_bytes(header + raw)
    return len(samples) // channels


def track(path, rate, channels, samples, **values):
    frames = wav(path, rate, channels, samples)
    return CaptureTrack(path, frames, rate, 0, None, channels, **values)


def marked(rate, frames, nonzero, *, at=-1, value=1, size=None):
    """`frames` 32 ms frames; frames 0..nonzero-1 hold one sample `value` (at index `at` of the frame)."""
    size = size or frame_samples(rate)
    samples = [0] * (frames * size)
    for index in range(nonzero):
        samples[index * size + at % size] = value
    return samples


@pytest.mark.parametrize(("rate", "size"), [(48000, 1536), (16000, 512)])
@pytest.mark.parametrize(("nonzero", "expected"), [(31, True), (32, False)])
def test_fewer_than_32_nonzero_frames_is_silent(tmp_path, rate, size, nonzero, expected):
    assert frame_samples(rate) == size
    # One non-zero sample, the last of its frame, is enough; the frame grid starts at sample 0.
    assert silent(track(tmp_path / "t.wav", rate, 1, marked(rate, 64, nonzero))) is expected
    assert silent(track(tmp_path / "f.wav", rate, 1, marked(rate, 64, nonzero, at=0, value=-1))) is expected


def test_frame_geometry_is_32_ms_at_the_track_rate(tmp_path):
    # One non-zero sample every 512 samples, 32 times: 32 frames at 16 kHz, only 11 at 48 kHz.
    samples = marked(16000, 40, 32, at=0, size=512)
    assert not silent(track(tmp_path / "16k.wav", 16000, 1, samples))
    assert silent(track(tmp_path / "48k.wav", 48000, 1, samples))
    # 44.1 kHz frames are floor(1411.2) = 1411 samples (D56 geometry, rounded down): a sample at the
    # start of each of 32 such frames touches only 31 frames of 1412.
    assert frame_samples(44100) == 1411
    assert not silent(track(tmp_path / "44k.wav", 44100, 1, marked(44100, 40, 32, at=0, size=1411)))


def test_constant_dc_is_present_and_exact_zeros_are_not(tmp_path):
    # The ActivityMeter removes the mean and calls DC silent; the literal rule does not.
    for value in (7, -1):
        assert not silent(track(tmp_path / f"dc{value}.wav", 16000, 1, [value] * (32 * 512)))
    assert silent(track(tmp_path / "zero.wav", 48000, 1, [0] * (48000 * 3)))
    assert silent(track(tmp_path / "empty.wav", 16000, 1, []))


def test_any_channel_counts_and_frames_span_every_channel(tmp_path):
    right_only = [s for v in marked(48000, 40, 32) for s in (0, v)]
    assert not silent(track(tmp_path / "right.wav", 48000, 2, right_only))
    # Opposite channels average to zero; each sample is still non-zero.
    opposite = [s for v in marked(48000, 40, 32, value=1000) for s in (v, -v)]
    assert not silent(track(tmp_path / "opposite.wav", 48000, 2, opposite))
    # 16 fully non-zero stereo frames are 16 frames, not 32 half-frames.
    full = [1] * (16 * 1536 * 2) + [0] * (24 * 1536 * 2)
    assert silent(track(tmp_path / "half.wav", 48000, 2, full))


def test_a_trailing_partial_frame_never_counts(tmp_path):
    # 31 full non-zero frames, then 1535 non-zero samples: one short of a frame.
    samples = [1] * (31 * 1536 + 1535)
    assert silent(track(tmp_path / "partial.wav", 48000, 1, samples))
    assert not silent(track(tmp_path / "whole.wav", 48000, 1, samples + [1]))


def test_rotation_files_of_one_entry_are_framed_each_from_their_own_start_and_summed(tmp_path):
    def entry(second):
        # The first file ends with a partial frame; the second file's grid restarts at its first sample.
        first = track(tmp_path / "a.wav", 16000, 1, [1] * (16 * 512 + 100))
        other = track(tmp_path / "b.wav", 16000, 1, second)
        return CaptureTrack(first.path, first.frames + other.frames, 16000, 0, None, spans=(first, other))

    assert not silent(entry([1] * (16 * 512)))
    assert silent(entry([1] * (15 * 512 + 511)))


def test_counting_does_not_depend_on_how_the_bytes_are_split():
    generator = random.Random(56)
    frame = 512 * 2 * 2
    data = bytearray(frame * 40 + 77)
    for index in generator.sample(range(len(data)), 20):
        data[index] = generator.randrange(1, 256)
    expected = sum(any(data[i : i + frame]) for i in range(0, frame * 40, frame))
    for _ in range(20):
        cuts = sorted(generator.sample(range(1, len(data)), 15))
        blocks = [bytes(data[a:b]) for a, b in zip([0, *cuts], [*cuts, len(data)], strict=True)]
        assert nonzero_frames(blocks, frame, limit=1000) == expected
    # The count stops at the limit: a track with sound is read only until it has 32 frames.
    assert nonzero_frames(iter([bytes([1]) * frame * 100]), frame) == 32


def test_an_unreadable_track_is_kept(tmp_path):
    assert not silent(CaptureTrack(tmp_path / "missing.wav", 100, 16000, 0, None))
    (tmp_path / "bad.wav").write_bytes(b"RIFF" + bytes(60))
    assert not silent(CaptureTrack(tmp_path / "bad.wav", 100, 16000, 0, None))


def test_compacted_opus_is_framed_at_its_decoded_48_khz(tmp_path):
    pytest.importorskip("gi")
    from test_track_source import compact

    session = RecordingSession(tmp_path, "Compacted")
    for identifier in ("zeros", "tone"):
        session.add_track(identifier, identifier, "remote", 48000, 1)
    feed(session, "zeros", [0] * 48000 * 2, 0, 48000)
    feed(session, "tone", [3000, -3000] * 48000, 0, 48000)
    session.finish()
    for identifier in ("zeros", "tone"):
        compact(session.directory, identifier)
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    by_id = {t.id: t for t in meeting.tracks}
    assert all(t.codec == "opus" for t in by_id.values())
    assert silent(by_id["zeros"]) and not silent(by_id["tone"])


# End to end ---------------------------------------------------------------------------------------

RATE = 16000


def feed(session, identifier, samples, offset, rate=RATE):
    """Append in protocol-sized chunks with consistent timestamps."""
    data = array.array("h", samples)
    if sys.byteorder != "little":
        data.byteswap()
    raw = data.tobytes()
    size = 4800 * 2
    for first in range(0, len(raw), size):
        session.append(identifier, raw[first : first + size], offset + first // 2 * 1000 / rate)


def bursts(seconds):
    """A loud 300 Hz tone on even seconds, digital silence on odd ones."""
    import math

    return [
        round(8000 * math.sin(2 * math.pi * 300 * i / RATE)) if (i // RATE) % 2 == 0 else 0
        for i in range(seconds * RATE)
    ]


def dc_frames(seconds, at_seconds, frames=31, value=3277):
    """`seconds` of zeros with `frames` 32 ms frames of DC (0.1) from `at_seconds`."""
    samples = [0] * (seconds * RATE)
    first = at_seconds * RATE
    samples[first : first + frames * 512] = [value] * (frames * 512)
    return samples


def meeting(root):
    """Me and a tab source (two spans) speak; a remote participant and the tab's second span are D56 silent.

    r1 and tab-b each hold 31 frames of DC, less than 1 s, so leaving them out of
    the published mix would change it.
    """
    session = RecordingSession(root, source="gnome")
    session.add_track("mic", "Microphone", "microphone", RATE, 1)
    session.add_track("r1", "Remote", "remote", RATE, 1, source_track_id="r1")
    session.add_track("tab-a", "Tab", "tab", RATE, 1, source_track_id="tab")
    session.add_track("tab-b", "Tab", "tab", RATE, 1, source_track_id="tab")
    feed(session, "mic", bursts(3), 0)
    feed(session, "r1", dc_frames(6, 4), 0)
    feed(session, "tab-a", bursts(3), 0)
    feed(session, "tab-b", dc_frames(2, 0), 3000)
    session.finish()
    return session


def published_mix(directory):
    """The Library v2 mixdown's PCM, before encoding, exactly as finalize builds it."""
    import numpy as np

    from cassini_talk.library_v2 import mixdown

    timeline = mixdown.session_spans(directory, json.loads((directory / "session.json").read_text()))
    return np.concatenate(list(mixdown.mixed(timeline.spans, mixdown.total_samples(timeline.spans))))


def capture_hashes(directory):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.name == "session.json" or p.suffix == ".wav"
    }


class Diarizer:
    """Records the rejoined group audio the native diarizer would read."""

    inputs = []

    def __init__(self, *_args):
        pass

    def infer(self, samples, _progress):
        Diarizer.inputs.append(len(samples))
        return dict(turns=[], inferenceSeconds=0.0)

    def close(self):
        pass


def in_process_child(monkeypatch, decoded, rejoined, sent):
    """A speech client running the real child code here: recognition, then diarization."""
    from test_recognition_fallback import RunModel, detector

    from cassini_talk import diarization_audio, worker_diarization
    from cassini_talk import speech_pipeline as pipeline

    monkeypatch.setattr(pipeline, "SpeechDetector", detector(1.0))
    monkeypatch.setitem(sys.modules, "cassini_talk.nemotron", types.SimpleNamespace(Nemotron=Diarizer))
    Diarizer.inputs = []
    original_chunks, original_rejoin = pipeline.resampled_chunks, diarization_audio.rejoin

    def chunks(path, *args, **kwargs):
        decoded.add(str(path).rsplit("/", 1)[-1])
        return original_chunks(path, *args, **kwargs)

    def rejoin(tracks, **kwargs):
        rejoined.append([t["captureTrackId"] for t in tracks])
        return original_rejoin(tracks, **kwargs)

    monkeypatch.setattr(pipeline, "resampled_chunks", chunks)
    monkeypatch.setattr(diarization_audio, "rejoin", rejoin)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            sent.append(json.loads(json.dumps(tracks)))  # Exactly what crosses the worker pipe.

            def emit(kind, **value):
                notify(dict(type=kind, **value))

            summary = pipeline.process_tracks(RunModel(), tracks, None, emit)
            return dict(type="complete", **worker_diarization.complete(summary, tracks, self.diarization, emit))

    return Client


def write_down(root, directory, client, *, diarization=True):
    from test_diarization_queue import Models

    from cassini_talk.transcription import TranscriptionQueue

    done = threading.Event()
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _directory: done.set(),
        bundle=root,
        root=root,
        client_factory=client,
        diarization_models=Models() if diarization else None,
    )
    try:
        assert queue.enqueue(directory)
        assert done.wait(60)
    finally:
        queue.close()


def test_a_silent_track_is_left_out_of_recognition_and_separation_but_stays_in_the_mix(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    from cassini_talk.library_schema import valid
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.nemotron_model import MODEL_ID, REVISION

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    session = meeting(tmp_path)
    directory = session.directory
    files = {t["id"]: t["file"] for t in session.manifest["tracks"]}
    mix, before = published_mix(directory), capture_hashes(directory)
    decoded, rejoined, sent = set(), [], []
    write_down(tmp_path, directory, in_process_child(monkeypatch, decoded, rejoined, sent))

    metadata = read_metadata(directory)
    assert metadata["transcript"]["status"] == "done"
    writing = metadata["recordingDetails"]["writingDown"]
    assert writing["silentTracks"] == ["r1", "tab-b"]
    assert valid(metadata)
    # The child gets every track, silent ones flagged, so the layout is unchanged.
    assert {t["captureTrackId"]: t.get("silent", False) for t in sent[0]} == {
        "mic": False,
        "r1": True,
        "tab-a": False,
        "tab-b": True,
    }
    # No recognition: the silent files were never decoded, in any pass.
    assert decoded == {files["mic"], files["tab-a"]}
    body = json.loads((directory / "transcript.json").read_bytes())
    assert {w["speaker"] for w in body["items"]} == {"me", "track-tab"}
    # The tab group's diarization mix is tab-a alone: 3 s, not tab-b's 5 s end.
    assert rejoined == [["tab-a"]] and Diarizer.inputs == [3 * RATE]
    # The silent participant keeps its speaker row, and does not make Me separable.
    assert "track-r1" in {s["id"] for s in metadata["speakers"]}
    assert writing["speakerSeparation"] == dict(
        status="completed", modelId=MODEL_ID, modelRevision=REVISION, speakersFound=0, reused=False
    )
    # The published mixdown keeps both silent tracks' sound, and the capture is untouched.
    after = published_mix(directory)
    assert np.array_equal(mix, after) and len(after) == 6 * 48000  # r1 sets the mix's end.
    for start, end in ((3.1, 3.9), (4.1, 4.9)):  # tab-b's DC, then r1's: each alone in the mix.
        assert np.mean(after[int(start * 48000) : int(end * 48000)]) == pytest.approx(0.1, abs=1e-3)
    assert capture_hashes(directory) == before


def test_silent_tracks_is_replaced_on_each_attempt_and_omitted_when_empty(tmp_path):
    from cassini_talk.meeting_store import read_metadata, update_metadata
    from cassini_talk.transcription import REFERENCE

    session = meeting(tmp_path)
    directory = session.directory
    files = {t["id"]: directory / t["file"] for t in session.manifest["tracks"]}
    flagged = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            flagged.append([t["captureTrackId"] for t in tracks if t.get("silent")])
            return dict(segments=0, diagnostics=[])

    def make_audible(identifier):
        """Same length, so the scan is unchanged: a tone over the whole file."""
        path = files[identifier]
        size = path.stat().st_size - 44
        with path.open("r+b") as stream:
            stream.seek(44)
            stream.write(struct.pack("<2h", 3000, -3000) * (size // 4))
        update_metadata(directory, transcript=dict(status="failed", reference=REFERENCE))

    write_down(tmp_path, directory, Client, diarization=False)
    assert read_metadata(directory)["recordingDetails"]["writingDown"]["silentTracks"] == ["r1", "tab-b"]
    make_audible("tab-b")
    write_down(tmp_path, directory, Client, diarization=False)
    # Replaced, never merged with the previous attempt's list.
    assert read_metadata(directory)["recordingDetails"]["writingDown"]["silentTracks"] == ["r1"]
    make_audible("r1")
    write_down(tmp_path, directory, Client, diarization=False)
    writing = read_metadata(directory)["recordingDetails"]["writingDown"]
    assert "silentTracks" not in writing and writing["wallDurationMs"] >= 0
    assert flagged == [["r1", "tab-b"], ["r1"], []]


def test_the_voice_step_never_rejoins_a_silent_track(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from test_resource_budget import fake_extractor, voices

    from cassini_talk import diarization_audio
    from cassini_talk.silent_tracks import silent as measure
    from cassini_talk.transcription import prepare_tracks

    session = meeting(tmp_path)
    items = [
        dict(speaker=speaker, text="word", startMs=start, endMs=start + 900)
        for speaker in ("me", "track-tab")
        for start in (0, 2000)
    ]
    (session.directory / "transcript.json").write_text(json.dumps(dict(format="cassini.words.v1", items=items)))
    (meeting_value,) = LibraryScanner(tmp_path).scan().meetings
    tracks, _speakers = prepare_tracks(meeting_value, silent=measure)
    fake_extractor(monkeypatch)
    rejoined = []
    original = diarization_audio.rejoin

    def rejoin(group, **kwargs):
        rejoined.append([t["captureTrackId"] for t in group])
        return original(group, **kwargs)

    monkeypatch.setattr(diarization_audio, "rejoin", rejoin)
    voices(session.directory, tracks, merges=[("me", "track-tab")])
    assert all(ids in (["mic"], ["tab-a"]) for ids in rejoined)
    assert ["tab-a"] in rejoined and ["mic"] in rejoined


def layout(*kinds):
    """Track jobs as prepare_tracks makes them: (id, kind, silent)."""
    speakers = {"microphone": "me", "system": "others"}
    return [
        dict(id=f"{i}.wav", captureTrackId=i, kind=k, speaker=speakers.get(k, "track-" + i), offsetMs=0)
        | dict(frames=16000, sampleRate=16000, **({"silent": True} if quiet else {}))
        for i, k, quiet in kinds
    ]


def test_eligibility_is_decided_on_every_track_and_an_all_silent_group_is_absent():
    from cassini_talk.capture_facts import separation_unavailable
    from cassini_talk.diarization import group_tracks

    # A silent participant never makes the microphone the only group.
    tracks = layout(("mic", "microphone", False), ("r1", "remote", True))
    assert group_tracks(tracks) == [dict(id="me", tracks=["mic.wav"], eligible=False, durationMs=1000)]
    assert separation_unavailable(None, tracks, "own")["reason"] == "notApplicable"
    # A silent system sound is an absent group: nothing is left to separate.
    tracks = layout(("mic", "microphone", False), ("room", "system", True))
    assert [g["id"] for g in group_tracks(tracks)] == ["me"]
    assert separation_unavailable(None, tracks, "own")["reason"] == "notApplicable"
    # With sound, the same layouts are what they were.
    assert separation_unavailable(None, layout(("mic", "microphone", False), ("room", "system", False)), "own")[
        "reason"
    ] == ("unavailable")
    assert group_tracks(layout(("mic", "microphone", False)))[0]["eligible"]
    assert group_tracks(layout(("mic", "microphone", True))) == []


def test_a_silent_span_never_pushes_a_live_span_of_its_source_onto_an_overlap_lane(tmp_path):
    from cassini_talk.diarization import group_tracks
    from cassini_talk.silent_tracks import silent as measure
    from cassini_talk.transcription import prepare_tracks

    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("early", "Tab", "tab", RATE, 1, source_track_id="tab")
    session.add_track("late", "Tab", "tab", RATE, 1, source_track_id="tab")
    feed(session, "early", [0] * (3 * RATE), 0)  # Silent, and first: it would take the base lane.
    feed(session, "late", bursts(3), 1000)
    session.finish()
    (meeting_value,) = LibraryScanner(tmp_path).scan().meetings
    diagnostics = []
    tracks, speakers = prepare_tracks(meeting_value, diagnostics=diagnostics, silent=measure)
    assert [(t["captureTrackId"], t["speaker"], t.get("silent", False)) for t in tracks] == [
        ("early", "track-tab", True),
        ("late", "track-tab", False),
    ]
    assert diagnostics == [] and list(speakers) == ["track-tab"]
    late = next(t["id"] for t in tracks if t["captureTrackId"] == "late")
    assert [(g["id"], g["tracks"]) for g in group_tracks(tracks)] == [("track-tab", [late])]


def test_a_silent_track_is_not_placed_so_its_unknown_offset_is_not_assumed_zero():
    from cassini_talk.capture_facts import offsets_assumed_zero, silent_tracks

    quiet = dict(id="q.wav", captureTrackId="q", offsetMs=None, silent=True)
    heard = dict(id="h.wav", captureTrackId="h", offsetMs=0)
    assert not offsets_assumed_zero([quiet, heard])
    assert offsets_assumed_zero([dict(quiet, silent=False), heard])
    assert silent_tracks([quiet, heard]) == ["q"] and silent_tracks([heard]) == []


@pytest.mark.parametrize("listed", [["tab-a"], None])
def test_a_voice_job_leaves_out_what_the_file_lists_and_never_measures(tmp_path, monkeypatch, listed):
    """D47: meeting.json, not the audio, says which tracks are silent outside the writing-down.

    The file lists tab-a, which has sound, and not r1 or tab-b, which are silent;
    without the key, no track is silent.
    """
    from test_voice_queue import Models

    from cassini_talk import voice_models
    from cassini_talk.meeting_store import update_metadata
    from cassini_talk.transcription import REFERENCE, TranscriptionQueue
    from cassini_talk.voice_store import VoiceStore

    directory = meeting(tmp_path).directory
    writing = dict(wallDurationMs=1, resumed=False, speakerSeparation=dict(status="skipped", reason="notApplicable"))
    if listed is not None:
        writing["silentTracks"] = listed
    update_metadata(
        directory, transcript=dict(status="done", reference=REFERENCE), recordingDetails=dict(writingDown=writing)
    )
    items = [dict(speaker="track-tab", text="Speech", startMs=0, endMs=900)]
    (directory / "transcript.json").write_text(json.dumps(dict(format="cassini.words.v1", language="en", items=items)))
    store = VoiceStore(tmp_path / "private")
    store.refresh()
    sent = []

    class Ready(Models):
        def snapshot(self):
            return dict(ready=True)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def close(self):
            pass

        def voiceprints(self, tracks, *_args):
            sent.append({t["captureTrackId"]: t.get("silent", False) for t in tracks})
            return {}

    monkeypatch.setattr(voice_models, "VoiceJobClient", Client)
    done = threading.Event()
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        root=tmp_path,
        diarization_models=Ready(),
        voice_models=Ready(),
        voice_store=store,
    )
    try:
        assert queue.backfill_voices(directory, lambda *_args: done.set())
        assert done.wait(30)
    finally:
        queue.close()
    assert sent == [{"mic": False, "r1": False, "tab-a": listed is not None, "tab-b": False}]


class Gate(set):
    """The queue's cancelled-meeting set; `added` fires when Trash registers a meeting."""

    def __init__(self):
        super().__init__()
        self.added = threading.Event()

    def add(self, value):
        super().add(value)
        self.added.set()


@pytest.mark.parametrize("stop", ["trash", "quit"])
def test_cancelling_stops_the_measurement_of_a_long_silent_track_mid_file(tmp_path, monkeypatch, stop):
    """The fake reader parks on its first audio block until the job is cancelled, then lets it go on."""
    import os

    from cassini_talk import silent_tracks
    from cassini_talk.meeting_store import read_metadata
    from cassini_talk.transcription import TranscriptionQueue

    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("quiet", "Remote", "remote", RATE, 1, source_track_id="quiet")
    feed(session, "quiet", [0] * (120 * RATE), 0)  # 2 min of zeros: 15 blocks of 256 frames.
    session.finish()
    directory = session.directory.absolute()
    reading, cancelled = threading.Event(), threading.Event()
    reads = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            reads.append(size)
            if len(reads) == 2:  # The header, then the first audio block.
                reading.set()
                assert cancelled.wait(30)
            return self.stream.read(size)

    fake_os = types.SimpleNamespace(fdopen=lambda fd, mode: Reader(os.fdopen(fd, mode)), fstat=os.fstat)
    monkeypatch.setattr(silent_tracks, "os", fake_os)
    recognised, finished = [], threading.Event()

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            recognised.append(tracks)
            return dict(segments=0, diagnostics=[])

    queue = TranscriptionQueue(
        lambda _: None, lambda _directory: finished.set(), bundle=tmp_path, root=tmp_path, client_factory=Client
    )
    queue._cancelled_meetings = gate = Gate()
    trashed = threading.Event()

    def trash():
        with queue.cancel_meeting(directory):
            trashed.set()

    try:
        assert queue.enqueue(directory)
        assert reading.wait(30)
        if stop == "trash":
            threading.Thread(target=trash, daemon=True).start()
            assert gate.added.wait(30)
        else:
            queue.close()
        cancelled.set()
        assert finished.wait(30)
        if stop == "trash":
            assert trashed.wait(30)
    finally:
        cancelled.set()
        queue.close()
    # The job stopped at the next block: header and one block read of 16, nothing recognised or written.
    assert len(reads) == 2 and not recognised
    assert read_metadata(directory)["transcript"]["status"] != "done"
