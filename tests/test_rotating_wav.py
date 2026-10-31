# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime checks precede the real publication imports.
"""Small injected RIFF limits exercise the production rotation without 4 GB buffers."""

import json
import wave
from contextlib import closing
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("WAV publication needs GStreamer", allow_module_level=True)

from cassini_talk.export_audio import track_pcm
from cassini_talk.library import LibraryScanner
from cassini_talk.library_v2 import finalize
from cassini_talk.recording_details import recording_observation
from cassini_talk.recording_session import RecordingSession, WAVWriter, load_session
from cassini_talk.speech_pipeline import audio_chunks
from cassini_talk.transcription import prepare_tracks


def capture(tmp_path, monkeypatch, limit=3200):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    session = RecordingSession(None, source="gnome", writer_factory=lambda p, r, c: WAVWriter(p, r, c, max_bytes=limit))
    session._native_clock_origin_ns = 1_000_000_000
    session.add_track("mic", "Me", "microphone", 8000, 1)
    return root, session


def stored_pcm(session):
    (track,) = load_session(session.directory / "session.json")["tracks"]
    rows = track.get("spans", [track])
    result = b""
    for row in rows:
        path = session.directory / row["file"]
        assert path.stat().st_size <= session.writers["mic"].max_bytes + 44
        with wave.open(str(path)) as stream:
            assert stream.getnframes() == row["frames"]
            result += stream.readframes(stream.getnframes())
    return result


def test_rotation_keeps_every_frame_one_speaker_and_all_input_paths(tmp_path, monkeypatch):
    root, session = capture(tmp_path, monkeypatch)
    blocks = [np.full(800, index * 1000, dtype="<i2").tobytes() for index in range(1, 7)]
    for index, block in enumerate(blocks):
        session.append_native("mic", block, index * 100, host_ns=1_000_000_000 + index * 100_000_000)
    session.finish()
    assert stored_pcm(session) == b"".join(blocks)
    (logical,) = session.manifest["tracks"]
    assert logical["id"] == "mic" and logical["frames"] == 4800 and len(logical["spans"]) == 3
    assert [span["startOffsetMs"] for span in logical["spans"]] == [0, 200, 400]
    (meeting,) = LibraryScanner(root).scan().meetings
    assert len(meeting.tracks) == 1 and len(meeting.tracks[0].spans) == 3
    assert meeting.tracks[0].display_end_ms == 600
    jobs, speakers = prepare_tracks(meeting)
    assert len(jobs) == len(speakers) == 1 and len(jobs[0]["spans"]) == 3
    with closing(audio_chunks(jobs[0])) as stream:
        spoken = np.concatenate(list(stream))
    assert len(spoken) == 9600
    with closing(track_pcm(meeting.tracks[0])) as stream:
        playback = np.concatenate([np.asarray(block) for block in stream])
    assert len(playback) == 28800
    assert np.min(playback[6000:8000]) > 0
    published = finalize.finalize(root, session.manifest["id"])
    assert published is not None and (root / published.name).is_file()
    assert stored_pcm(session) == b"".join(blocks)


def test_materialized_gap_crosses_rotation_without_loss_or_double_insertion(tmp_path, monkeypatch):
    root, session = capture(tmp_path, monkeypatch, limit=16000)
    first, last = b"\x10\0" * 800, b"\x20\0" * 800
    session.append_native("mic", first, 0, host_ns=1_000_000_000)
    session.append_native("mic", last, 1100, gap_frames=8000, host_ns=2_100_000_000)
    session.finish()
    assert stored_pcm(session) == first + bytes(16000) + last
    spans = session.manifest["tracks"][0]["spans"]
    assert sum(span.get("gapFrames", 0) for span in spans) == 8000
    assert all(span.get("gapMaterialized") is True for span in spans if span.get("gapFrames"))
    (meeting,) = LibraryScanner(root).scan().meetings
    assert len(meeting.tracks) == 1 and meeting.duration_ms == 1200
    (job,), _speakers = prepare_tracks(meeting)
    with closing(audio_chunks(job)) as stream:
        spoken = np.concatenate(list(stream))
    assert len(spoken) == 19200 and not np.any(spoken[2000:17000])


def test_new_span_reference_is_durable_before_any_new_pcm(tmp_path, monkeypatch):
    _root, session = capture(tmp_path, monkeypatch, limit=1600)
    block = b"\x10\0" * 800
    session.append_native("mic", block, 0, host_ns=1_000_000_000)
    create = session.writer_factory
    observed = []

    def factory(path, rate, channels):
        writer = create(path, rate, channels)
        append = writer.append

        def checked(data):
            header = json.loads((session.directory / "session.json").read_text())
            rows = header["tracks"][0]["spans"]
            assert rows[-1]["file"] == path.name and rows[-1]["frames"] == 0
            assert path.stat().st_size == 44
            observed.append(True)
            return append(data)

        writer.append = checked
        return writer

    session.writer_factory = factory
    session.append_native("mic", block, 100, host_ns=1_100_000_000)
    session.finish()
    assert observed == [True] and stored_pcm(session) == block * 2


def test_live_observation_uses_the_active_span_and_does_not_count_rotations_as_voices(tmp_path, monkeypatch):
    root, session = capture(tmp_path, monkeypatch, limit=1600)
    tone = (np.sin(np.arange(800) * 2 * np.pi * 440 / 8000) * 5000).astype("<i2").tobytes()
    session.append_native("mic", tone, 0)
    session.append_native("mic", tone, 100)
    session.checkpoint()
    (meeting,) = LibraryScanner(root).scan(capture_activity={session.manifest["id"]: "recording"}).meetings
    assert not meeting.tracks[0].ended
    assert [span.ended for span in meeting.tracks[0].spans] == [True, False]
    observed = recording_observation(meeting, freshness_seconds=None)
    assert observed["levels"][0] > 0 and observed["voices"] == 0
    session.finish()


def _produce_rotation_boundary(child, phase):
    session = RecordingSession(None, source="gnome", writer_factory=lambda p, r, c: WAVWriter(p, r, c, max_bytes=1600))
    session.add_track("mic", "Me", "microphone", 8000, 1)
    session.append_native("mic", b"\x10\0" * 800, 0)
    session.checkpoint()
    child.send(str(session.directory))

    def barrier():
        child.send(phase)
        child.recv()  # The parent kills this process after the explicit event.

    if phase == "oldClosed":
        close = session.writers["mic"].current.close

        def closed():
            close()
            barrier()

        session.writers["mic"].current.close = closed
    elif phase == "referencePublished":
        checkpoint = session.checkpoint

        def checkpointed():
            checkpoint()
            if session.manifest["tracks"][0].get("spans"):
                barrier()

        session.checkpoint = checkpointed
    else:
        create = session.writer_factory

        def factory(path, rate, channels):
            writer = create(path, rate, channels)
            append = writer.append

            def appended(data):
                append(data)
                barrier()

            writer.append = appended
            return writer

        session.writer_factory = factory
    session.append_native("mic", b"\x20\0" * 800, 100)


@pytest.mark.parametrize("phase", ["oldClosed", "referencePublished", "newPCM"])
def test_sigkill_at_rotation_boundaries_keeps_the_original_and_referenced_pcm(tmp_path, monkeypatch, phase):
    import multiprocessing
    import os
    import signal

    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()

    process = context.Process(target=_produce_rotation_boundary, args=(child, phase))
    process.start()
    child.close()
    try:
        assert parent.poll(5), "Rotation producer did not create its first durable WAV"
        directory = Path(parent.recv())
        assert parent.poll(5), f"Rotation did not reach {phase} before its deadline"
        assert parent.recv() == phase
        os.kill(process.pid, signal.SIGKILL)
        process.join(5)
        assert not process.is_alive() and process.exitcode == -signal.SIGKILL
        manifest = load_session(directory / "session.json")
        (logical,) = manifest["tracks"]
        rows = logical.get("spans", [logical])
        observed = b""
        for row in rows:
            with wave.open(str(directory / row["file"])) as stream:
                observed += stream.readframes(stream.getnframes())
        expected = b"\x10\0" * 800 + (b"\x20\0" * 800 if phase == "newPCM" else b"")
        assert observed == expected
        (meeting,) = LibraryScanner(root).scan().meetings
        assert meeting.interrupted_at_ms is not None and len(meeting.tracks) == 1
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()


def test_real_riff_ceiling_rotates_sparse_pcm_before_the_header_can_overflow(tmp_path, monkeypatch):
    import struct

    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    session = RecordingSession(None, source="gnome")
    session.add_track("mic", "Me", "microphone", 48000, 1)
    writer = session.writers["mic"]
    # Only the WAV header and the new packet allocate pages. Exercise the real
    # 32-bit boundary, rather than allocating four gigabytes of test samples.
    size = (writer.max_bytes // 2) * 2 - 32
    writer.current.size = size
    writer.current.file.truncate(44 + size)
    writer.current._header()
    session.manifest["tracks"][0].update(frames=size // 2, startOffsetMs=0)
    session.checkpoint()
    packet = b"\x10\0" * 800
    session.append_native("mic", packet, size // 2 * 1000 / 48000)
    session.finish()
    (logical,) = load_session(session.directory / "session.json")["tracks"]
    assert len(logical["spans"]) == 2 and logical["frames"] == size // 2 + 800
    first, second = [session.directory / row["file"] for row in logical["spans"]]
    assert first.stat().st_size == 44 + size < 2**32
    with first.open("rb") as stream:
        header = stream.read(44)
    assert struct.unpack_from("<I", header, 4)[0] == size + 36
    assert second.read_bytes()[44:] == packet
