# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1125 unit, paired integration, negative-control, symlink/lifecycle, G14 budget, and scaling tests."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from durable import Finished

np = pytest.importorskip("numpy")

from fake_follow_worker import DeterministicDetector, DeterministicNemotron, DeterministicRecognizer  # noqa: E402
from test_session_follower import (  # noqa: E402
    FakeBundle,
    FakeDiarizationModels,
    FakeSpeechModels,
    SubprocessTestClient,
    _assert_lanes_byte_identical,
    _run_paired_lane,
    cached_segment_count,
    make_speech_pcm16,
    wait_on_queue,
)

from cassini_talk import (  # noqa: E402
    diarization_audio,
    nemotron_model,
    session_follower,
    speech_pipeline,
    worker_diarization,
)
from cassini_talk.diarization import turns_sha256, voice_source  # noqa: E402
from cassini_talk.library import LibraryScanner  # noqa: E402
from cassini_talk.library_publication import capture_parent  # noqa: E402
from cassini_talk.parakeet_model import MODEL_ID as PARAKEET_MODEL_ID  # noqa: E402
from cassini_talk.recording_session import RecordingSession, WAVWriter  # noqa: E402
from cassini_talk.rotating_wav import RotatingWAV  # noqa: E402
from cassini_talk.session_follower import SessionFollower  # noqa: E402
from cassini_talk.transcription import (  # noqa: E402
    RECOGNITION_CACHE_FILE,
    TranscriptionMode,
    TranscriptionQueue,
    prepare_tracks,
)


def append_pcm_chunks(session, track_id: str, pcm: bytes, *, rate: int, channels: int, start_ms: float = 0.0) -> None:
    """Append PCM16 bytes in <= 0.5 s wire-sized chunks respecting RecordingSession.MAX_CHUNK."""
    frame_bytes = channels * 2
    step = (rate // 2) * frame_bytes
    for offset in range(0, len(pcm), step):
        chunk = pcm[offset : offset + step]
        session.append(track_id, chunk, start_ms + (offset // frame_bytes) * 1000.0 / rate)


class _TempFileMeter:
    """Counts tempfile.TemporaryFile creations, peak concurrency, and live open bytes."""

    def __init__(self, monkeypatch):
        self.created = 0
        self.max_open = 0
        self.open_files: dict[int, object] = {}
        real_temp = tempfile.TemporaryFile

        def tracked(*args, **kwargs):
            f = real_temp(*args, **kwargs)
            self.created += 1
            ident = id(f)
            self.open_files[ident] = f
            if len(self.open_files) > self.max_open:
                self.max_open = len(self.open_files)
            real_close = f.close

            def tracked_close():
                self.open_files.pop(ident, None)
                return real_close()

            f.close = tracked_close
            return f

        monkeypatch.setattr(tempfile, "TemporaryFile", tracked)

    def reset_max(self) -> None:
        self.max_open = len(self.open_files)

    def open_bytes(self) -> int:
        total = 0
        for f in list(self.open_files.values()):
            with contextlib.suppress(OSError, ValueError):
                total += os.fstat(f.fileno()).st_size
        return total


# ---------------------------------------------------------------------------
# 1. record_only and after_stop perform ZERO staging and ZERO warming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [TranscriptionMode.RECORD_ONLY, TranscriptionMode.AFTER_STOP])
@pytest.mark.parametrize("engine", ["whistle", "parakeet"])
def test_record_only_and_after_stop_do_zero_diarization_staging_or_warming(tmp_path, mode, engine):
    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    scratch = bundle / "scratch"
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    selected = PARAKEET_MODEL_ID if engine == "parakeet" else "whistle"
    models = FakeSpeechModels(selected, tracker)
    diar_requests = []

    class ObservedDiarizationModels(FakeDiarizationModels):
        def request(self, directory, *, origin="own"):
            diar_requests.append(str(directory))
            return super().request(directory, origin=origin)

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        models=models,
        diarization_models=ObservedDiarizationModels(),
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=mode,
    )
    session = RecordingSession(capture_parent(lib), "Zero Prestage Mode", source="gnome")
    try:
        session.add_track("system", None, "system", 48000, 2, filename="track-system.wav")
        append_pcm_chunks(
            session,
            "system",
            make_speech_pcm16(48000, 2, [(1.2, 410.0, 0.3), (0.9, 0.0, 0.0), (1.2, 580.0, 0.3)]),
            rate=48000,
            channels=2,
        )
        session.checkpoint()
        assert not queue.follow_recording(session.directory)
        queue.sync_recordings([session.directory])
        assert not queue.notify_session_finished(session.directory)
        assert tracker["pids"] == []
        assert diar_requests == []
        assert not scratch.exists()
        assert not (session.directory / RECOGNITION_CACHE_FILE).exists()
    finally:
        session.finish()
        queue.close()


# ---------------------------------------------------------------------------
# 2. Multi-minute anchored prefix + <60 s tail + warm Nemotron paired test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["whistle", "parakeet"])
def test_paired_multi_minute_anchored_prefix_and_tail_byte_identical_turns_and_warm_nemotron(tmp_path, engine):
    bundle = FakeBundle.create(tmp_path)
    drift_ppm = 100
    # 142 s stereo 48 kHz recording:
    # - Two permanently fixed 60 s knots ([0..60 s], [60..120 s])
    # - < 60 s tail ([120..142 s])
    # - Alternating speaker 0 (<495 Hz: 390/420 Hz) and speaker 1 (>=495 Hz: 560/620 Hz)
    spec = [
        (1.2, 410.0, 0.3),  # Speaker 0 in minute 1
        (57.3, 0.0, 0.0),
        (2.4, 580.0, 0.3),  # Speaker 1 spanning the 60 s knot!
        (1.0, 0.0, 0.0),
        (1.2, 420.0, 0.3),  # Speaker 0 in minute 2
        (58.0, 0.0, 0.0),
        (1.3, 610.0, 0.3),  # Speaker 1 in the <60 s post-knot tail (t=121.1..122.4 s)
        (1.0, 0.0, 0.0),
        (1.2, 390.0, 0.3),  # Speaker 0 in the <60 s post-knot tail
        (17.4, 0.0, 0.0),
    ]
    pcm = make_speech_pcm16(48000, 2, spec)

    def record_meeting(session, queue, _tracker, live, _sig):
        session.add_track("microphone", None, "microphone", 48000, 1, filename="track-microphone.wav")
        session.add_track("system", None, "system", 48000, 2, filename="track-system.wav")
        mic_pcm = make_speech_pcm16(48000, 1, [(1.0, 440.0, 0.25), (2.0, 0.0, 0.0)])
        append_pcm_chunks(session, "microphone", mic_pcm, rate=48000, channels=1)

        block_frames = 48000 * 5
        frame_bytes = 4
        cursor = 0
        total_frames = len(pcm) // frame_bytes
        while cursor < total_frames:
            take = min(block_frames, total_frames - cursor)
            chunk = pcm[cursor * frame_bytes : (cursor + take) * frame_bytes]
            offset_ms = cursor * 1000.0 / 48000.0
            host_ns = session._native_clock_origin_ns + round(offset_ms * (1_000_000 + drift_ppm))
            session.append_native("system", chunk, offset_ms, host_ns=host_ns)
            cursor += take
            if cursor in (48000 * 65, 48000 * 125):
                session.checkpoint()
        session.checkpoint()
        if live:
            # Wait event-driven until segments across both anchored 60 s intervals are precomputed.
            wait_on_queue(
                queue,
                lambda: cached_segment_count(session.directory, engine, bundle) >= 4,
                timeout=25.0,
                message="Follower did not precompute anchored multi-minute segments",
            )

    batch = _run_paired_lane(tmp_path, bundle, engine, False, record_meeting)
    live = _run_paired_lane(tmp_path, bundle, engine, True, record_meeting)
    _assert_lanes_byte_identical(batch, live)

    # Verify conclusive 2-voice diarization + G12 voiceSource stamps match identically.
    turns_doc = json.loads(live["turns_bytes"])
    assert len(turns_doc["groups"]) == 1
    group = turns_doc["groups"][0]
    assert group["id"] == "others"
    assert len({t["speaker"] for t in group["turns"]}) == 2
    expected_source = voice_source(group["inputSHA256"], group["turns"])
    assert expected_source["turnsSHA256"] == turns_sha256(group["turns"])

    live_speakers = {s["id"]: s for s in live["meta"]["speakers"]}
    batch_speakers = {s["id"]: s for s in batch["meta"]["speakers"]}
    assert live_speakers == batch_speakers
    assert "others~1" in live_speakers and "others~2" in live_speakers
    assert live_speakers["others~1"]["voiceSource"] == expected_source
    assert live_speakers["others~2"]["voiceSource"] == expected_source

    transcript_words = json.loads(live["transcript_bytes"])["items"]
    assigned = {w["speaker"] for w in transcript_words}
    assert {"me", "others~1", "others~2"}.issubset(assigned)

    # Verify warm handoff, staged 16 kHz prefix reuse, <60 s tail append, and warm Nemotron reuse!
    live_complete = live["tracker"]["completes"][0]
    batch_complete = batch["tracker"]["completes"][0]
    assert len(live["tracker"]["pids"]) == 1
    assert live_complete["workerPid"] == live["tracker"]["pids"][0]
    assert live_complete["diarization"]["stagedGroupsReused"] == 1
    assert batch_complete["diarization"]["stagedGroupsReused"] == 0
    assert live_complete["diarization"]["warmModelReused"] is True
    assert batch_complete["diarization"]["warmModelReused"] is False
    assert live_complete["postStopNemotronConstructs"] == 0
    assert batch_complete["postStopNemotronConstructs"] == 1
    assert live_complete["totalNemotronConstructs"] == 1
    assert live_complete["totalNemotronCloses"] == 1
    assert 0 <= live_complete["diarization"]["tailFramesAppended"] < 30 * 16000
    scratch_dir = bundle / "scratch"
    if scratch_dir.is_dir():
        assert list(scratch_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# 3. Direct bit-for-bit rejoin equivalence across 16k, RotatingWAV+ownership, and warped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["16k-offset", "48k-rotating-ownership", "48k-warped-neg-drift"])
def test_rejoin_with_staged_bit_exact_across_rotating_spans_warped_and_nonzero_offset(tmp_path, case):
    scratch = tmp_path / "scratch"
    lib = tmp_path / "lib"
    lib.mkdir()
    parent = capture_parent(lib) if case == "48k-warped-neg-drift" else lib
    session = RecordingSession(parent, f"BitExact-{case}", source="chromium" if "ownership" in case else "gnome")

    if case == "16k-offset":
        session.add_track("system", None, "system", 16000, 1, filename="track-system.wav")
        pcm_pre = make_speech_pcm16(16000, 1, [(1.2, 410.0, 0.3), (0.8, 0.0, 0.0)])
        pcm_tail = make_speech_pcm16(16000, 1, [(1.1, 590.0, 0.3), (0.5, 0.0, 0.0)])
        session.append("system", pcm_pre, 3456.25)
        session.checkpoint()
    elif case == "48k-rotating-ownership":
        max_bytes = 48000 * 4 * 3 // 2  # Rotate every 1.5 s!
        session.writer_factory = lambda p, r, c: WAVWriter(p, r, c, max_bytes=max_bytes)
        session.add_track("remote-1", None, "remote", 48000, 2)
        session.writers["remote-1"] = RotatingWAV(session, "remote-1", session.writers["remote-1"])
        session.manifest["events"] = [
            dict(type="selection", atMs=0, me=True, others=True),
            dict(type="othersSource", atMs=0, source="browser"),
            dict(type="othersSource", atMs=14200, source="system"),
            dict(type="othersSource", atMs=15400, source="browser"),
        ]
        pcm_all = make_speech_pcm16(
            48000,
            2,
            [(2.2, 410.0, 0.3), (0.8, 0.0, 0.0), (2.0, 590.0, 0.3), (0.8, 0.0, 0.0)],
        )
        step = 48000 * 4 // 4
        base_ms = 12345.5
        # Append first 4.0 s before Stop, then 1.8 s tail after staging.
        split_bytes = 4 * 48000 * 4
        pos = 0
        while pos < split_bytes:
            chunk = pcm_all[pos : pos + step]
            session.append("remote-1", chunk, base_ms + (pos // 4) * 1000.0 / 48000.0)
            pos += len(chunk)
        session.checkpoint()
        pcm_tail = pcm_all[split_bytes:]
    else:
        session.add_track("system", None, "system", 48000, 2, filename="track-system.wav")
        spec = [(1.2, 410.0, 0.3), (60.0, 0.0, 0.0), (1.2, 590.0, 0.3), (60.0, 0.0, 0.0), (2.0, 420.0, 0.3)]
        pcm_all = make_speech_pcm16(48000, 2, spec)
        frame_bytes = 4
        total_frames = len(pcm_all) // frame_bytes
        pre_frames = 48000 * 123  # Two fixed 60 s knots committed before tail
        block_frames = 48000 * 5
        cur = 0
        while cur < pre_frames:
            take = min(block_frames, pre_frames - cur)
            chunk = pcm_all[cur * frame_bytes : (cur + take) * frame_bytes]
            off_ms = cur * 1000.0 / 48000.0
            host_ns = session._native_clock_origin_ns + round(off_ms * (1_000_000 - 120))
            session.append_native("system", chunk, off_ms, host_ns=host_ns)
            cur += take
        session.checkpoint()

    follower = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        "whistle@" + ("a" * 64),
        DeterministicDetector(),
        on_cache_entry=lambda _e: None,
        diarization_options=dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
    )
    assert follower.refresh()
    while follower.step():
        pass
    staged_map = follower.export_diarization_staged()
    assert staged_map, f"Expected pre-staged diarization audio for {case}"

    # Now append tail and finish session!
    if case == "16k-offset":
        session.append("system", pcm_tail, 3456.25 + (len(pcm_pre) // 2) * 1000.0 / 16000.0)
    elif case == "48k-rotating-ownership":
        while pos < len(pcm_all):
            chunk = pcm_all[pos : pos + step]
            session.append("remote-1", chunk, base_ms + (pos // 4) * 1000.0 / 48000.0)
            pos += len(chunk)
    else:
        while cur < total_frames:
            take = min(block_frames, total_frames - cur)
            chunk = pcm_all[cur * frame_bytes : (cur + take) * frame_bytes]
            off_ms = cur * 1000.0 / 48000.0
            host_ns = session._native_clock_origin_ns + round(off_ms * (1_000_000 - 120))
            session.append_native("system", chunk, off_ms, host_ns=host_ns)
            cur += take
    session.finish()

    scanner = LibraryScanner.for_meeting(session.directory)
    meeting = next(
        m for m in scanner.scan(changed_directories={session.directory}).meetings if m.directory == session.directory
    )
    tracks, _speakers = prepare_tracks(meeting)
    assert len(tracks) == 1
    track = tracks[0]
    if case == "48k-warped-neg-drift":
        assert "clockAnchors" in track and len(track["clockAnchors"]) >= 3
    staged = diarization_audio.pop_staged_for_track(staged_map, track)
    assert staged is not None

    with diarization_audio.rejoin_with_staged([track], staged=staged, scratch=scratch) as (
        s_stream,
        s_frames,
        s_sha,
        reused,
        tail_frames,
    ):
        staged_bytes = s_stream.read()
    assert staged.closed

    with diarization_audio.rejoin([track], scratch=scratch) as (f_stream, f_frames, f_sha):
        full_bytes = f_stream.read()

    assert reused is True
    assert 0 < tail_frames < f_frames
    assert s_frames == f_frames
    assert s_sha == f_sha
    assert staged_bytes == full_bytes


# ---------------------------------------------------------------------------
# 4. Negative controls: mutated PCM, truncated WAV, mutated knot, retroactive ownership,
#    multi-source group, and solo-mic -> multi-track transition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation_kind",
    [
        "mutated-prefix-pcm",
        "truncated-wav",
        "mutated-clock-anchor",
        "retroactive-ownership-cut",
        "multi-source-group",
    ],
)
def test_negative_controls_reject_staged_prefix_and_fall_back_to_exact_full_rejoin(
    tmp_path, monkeypatch, mutation_kind
):
    monkeypatch.setattr("cassini_talk.nemotron.Nemotron", DeterministicNemotron)
    scratch = tmp_path / "scratch"
    lib = tmp_path / "lib"
    lib.mkdir()
    parent = capture_parent(lib) if mutation_kind == "mutated-clock-anchor" else lib
    session = RecordingSession(parent, f"Neg-{mutation_kind}", source="gnome")
    session.add_track("system", None, "system", 48000, 1, filename="track-system.wav")

    if mutation_kind == "mutated-clock-anchor":
        spec = [(1.2, 410.0, 0.3), (60.0, 0.0, 0.0), (1.2, 590.0, 0.3), (60.0, 0.0, 0.0)]
        pcm = make_speech_pcm16(48000, 1, spec)
        for cur in range(0, len(pcm) // 2, 48000 * 5):
            take = min(48000 * 5, len(pcm) // 2 - cur)
            off_ms = cur * 1000.0 / 48000.0
            host_ns = session._native_clock_origin_ns + round(off_ms * (1_000_000 + 80))
            session.append_native("system", pcm[cur * 2 : (cur + take) * 2], off_ms, host_ns=host_ns)
    else:
        pcm = make_speech_pcm16(48000, 1, [(1.5, 410.0, 0.3), (0.8, 0.0, 0.0), (1.5, 590.0, 0.3), (0.8, 0.0, 0.0)])
        append_pcm_chunks(session, "system", pcm, rate=48000, channels=1)
    session.checkpoint()

    follower = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        "whistle@" + ("a" * 64),
        DeterministicDetector(),
        on_cache_entry=lambda _e: None,
        diarization_options=dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
    )
    assert follower.refresh()
    while follower.step():
        pass
    staged_map = follower.export_diarization_staged()
    assert staged_map, "Precondition: prefix must be staged before injecting negative-control mutation"
    session.finish()

    wav_path = session.directory / "track-system.wav"
    if mutation_kind == "mutated-prefix-pcm":
        # Flip samples inside already-staged minute 1 without changing file size!
        with wav_path.open("r+b") as f:
            f.seek(44 + 24000 * 2)
            f.write(struct.pack("<h", 16000) * 4000)
    elif mutation_kind == "truncated-wav":
        keep_frames = 48000 * 2  # Shorter than staged prefix!
        raw_head = wav_path.read_bytes()[: 44 + keep_frames * 2]
        data_size = keep_frames * 2
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            data_size + 36,
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            48000,
            96000,
            2,
            16,
            b"data",
            data_size,
        )
        wav_path.write_bytes(header + raw_head[44:])

    scanner = LibraryScanner.for_meeting(session.directory)
    meeting = next(
        m for m in scanner.scan(changed_directories={session.directory}).meetings if m.directory == session.directory
    )
    tracks, _speakers = prepare_tracks(meeting)

    if mutation_kind == "mutated-clock-anchor":
        # Perturb the 60 s anchor timestamp in finalized track metadata.
        anchors = [list(pt) for pt in tracks[0]["clockAnchors"]]
        anchors[1][1] += 25_000_000  # +25 ms shift at knot 1
        tracks[0]["clockAnchors"] = anchors
    elif mutation_kind == "retroactive-ownership-cut":
        tracks[0]["ownershipWindows"] = [[0, 400], [1800, None]]
    elif mutation_kind == "multi-source-group":
        wav2 = session.directory / "track-system-extra.wav"
        extra_pcm = make_speech_pcm16(48000, 1, [(1.2, 610.0, 0.3)])
        w2 = WAVWriter(wav2, 48000, 1)
        w2.append(extra_pcm)
        w2.file.close()
        tracks.append(
            dict(
                id="track-system-extra.wav",
                path=str(wav2),
                offsetMs=500,
                speaker="others",
                kind="system",
                frames=len(extra_pcm) // 2,
                sampleRate=48000,
            )
        )

    events_live, events_ref = [], []
    opts = dict(modelPath="m", runtimePath="r", scratch=str(scratch))
    res_live = worker_diarization.run(
        tracks,
        opts,
        lambda kind, **kw: events_live.append(dict(type=kind, **kw)),
        diarization_store=staged_map,
    )
    assert staged_map == {}, "diarization_store must be emptied and closed on fallback"

    res_ref = worker_diarization.run(
        tracks,
        opts,
        lambda kind, **kw: events_ref.append(dict(type=kind, **kw)),
    )
    assert res_live["stagedGroupsReused"] == 0
    assert res_live["diagnostics"] == res_ref["diagnostics"] == []
    diarized_live = [e for e in events_live if e["type"] == "diarized"]
    diarized_ref = [e for e in events_ref if e["type"] == "diarized"]
    assert diarized_live == diarized_ref


def test_solo_mic_discards_staged_file_immediately_when_remote_track_joins(tmp_path, monkeypatch):
    meter = _TempFileMeter(monkeypatch)
    scratch = tmp_path / "scratch"
    session = RecordingSession(tmp_path / "lib", "SoloToCall", source="chromium")
    session.add_track("local-1", None, "local", 48000, 1)
    append_pcm_chunks(
        session,
        "local-1",
        make_speech_pcm16(48000, 1, [(1.2, 420.0, 0.3), (0.8, 0.0, 0.0)]),
        rate=48000,
        channels=1,
    )
    session.checkpoint()

    follower = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        "whistle@" + ("a" * 64),
        DeterministicDetector(),
        on_cache_entry=lambda _e: None,
        diarization_options=dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
    )
    assert follower.refresh()
    while follower.step():
        pass
    assert follower.staged_diarization_count() == 1
    assert len(meter.open_files) == 1

    # Remote participant joins -> "me" ("local-1") becomes ineligible; its scratch file closes immediately!
    session.add_track("remote-1", None, "remote", 48000, 1)
    append_pcm_chunks(
        session,
        "remote-1",
        make_speech_pcm16(48000, 1, [(1.2, 410.0, 0.3), (0.8, 0.0, 0.0), (1.2, 600.0, 0.3)]),
        rate=48000,
        channels=1,
        start_ms=2000.0,
    )
    session.checkpoint()

    assert follower.refresh()
    assert not follower.tracks["local-1"].has_staged_diarization()
    # Before remote-1 advances, 0 temp files are open!
    assert len(meter.open_files) == 0

    while follower.step():
        # G14 invariant: never more than 1 group scratch file open!
        assert len(meter.open_files) <= 1

    staged_map = follower.export_diarization_staged()
    assert (follower.resolved_dir, "remote-1") in staged_map
    assert (follower.resolved_dir, "local-1") not in staged_map
    diarization_audio.close_diarization_store(staged_map)
    assert len(meter.open_files) == 0
    session.finish()


# ---------------------------------------------------------------------------
# 5. G14 scratch_budget shortfall, mid-recording space loss, no double-count, and low RAM
# ---------------------------------------------------------------------------


def test_scratch_budget_shortfall_and_low_memory_skip_cleanly_without_disturbing_recording(tmp_path, monkeypatch):
    monkeypatch.setattr("cassini_talk.nemotron.Nemotron", DeterministicNemotron)
    meter = _TempFileMeter(monkeypatch)
    scratch = tmp_path / "scratch"
    lib = tmp_path / "lib"
    lib.mkdir()
    session = RecordingSession(lib, "LowDiskAndLowRam", source="gnome")
    session.add_track("system", None, "system", 48000, 1, filename="track-system.wav")
    append_pcm_chunks(
        session,
        "system",
        make_speech_pcm16(48000, 1, [(1.5, 410.0, 0.3), (0.9, 0.0, 0.0)]),
        rate=48000,
        channels=1,
    )
    session.checkpoint()

    # 1) Live recording on same 500 GiB volume requires max(1 GiB, 2%) = 10 GiB reserve.
    #    Give 9 GiB free -> above 1 GiB floor, but BELOW 10 GiB recorder reserve!
    def tight_live_statvfs(_path):
        return SimpleNamespace(f_bavail=9 << 30, f_frsize=1, f_blocks=500 << 30)

    cached = []
    opts = dict(
        modelPath="m",
        runtimePath="r",
        scratch=str(scratch),
        recordingDirectories=[str(session.directory)],
    )
    follower = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        "whistle@" + ("a" * 64),
        DeterministicDetector(),
        on_cache_entry=cached.append,
        diarization_options=opts,
        statvfs=tight_live_statvfs,
    )
    assert follower.refresh()
    while follower.step():
        pass
    assert len(cached) >= 1, "ASR segment precompute must proceed undisturbed when diarization scratch is tight"
    assert meter.created == 0, "Zero scratch TemporaryFiles may be created when below live recorder reserve"
    assert follower.staged_diarization_count() == 0

    # WarmDiarizer also refuses when scratch is below reserve OR when MemAvailable < 512 MiB.
    warm = worker_diarization.WarmDiarizer()
    constructs_before = DeterministicNemotron.constructs
    assert not warm.warm(opts, statvfs=tight_live_statvfs)
    assert not warm.warm(
        dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
        mem_available=lambda: 256 * 1024 * 1024,
    )
    assert DeterministicNemotron.constructs == constructs_before

    # 2) Sufficient memory + scratch warms cleanly, and pin signature mismatch invalidates take()!
    assert warm.warm(
        dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
        mem_available=lambda: 2 * 1024**3,
    )
    assert DeterministicNemotron.constructs == constructs_before + 1
    warm.model_sig = (("model.int8.onnx", 1, 10, 20),)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(nemotron_model, "model_signature_matches", lambda *_a: False)
        assert warm.take(dict(modelPath="m", runtimePath="r")) is None

    # 3) No double-counting at post-Stop when free space fits 1.3x group (not 2x group)!
    session.finish()
    meeting = LibraryScanner(lib).scan().meetings[0]
    tracks, _ = prepare_tracks(meeting)
    group_needed = (2400 + 1000) * 64
    floor = 1 << 30
    one_group_room = floor + int(group_needed * 1.3)

    # Simulate real filesystem statvfs that subtracts bytes currently held in scratch!
    def dynamic_statvfs(_path):
        return SimpleNamespace(f_bavail=max(0, one_group_room - meter.open_bytes()), f_frsize=1, f_blocks=10 << 30)

    follower2 = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        "whistle@" + ("a" * 64),
        DeterministicDetector(),
        on_cache_entry=lambda _e: None,
        diarization_options=dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
        statvfs=dynamic_statvfs,
    )
    assert follower2.refresh()
    while follower2.step():
        pass
    staged_map = follower2.export_diarization_staged()
    assert staged_map
    staged_obj = next(iter(staged_map.values()))
    staged_bytes = os.fstat(staged_obj.stream.fileno()).st_size
    assert staged_bytes > 0

    # At post-Stop, real os.statvfs reports (one_group_room - staged_bytes) free, which is LESS than
    # floor + group_needed if staged_bytes were double-counted, yet tail needs only ~64 KiB!
    post_stop_free = floor + group_needed - (staged_bytes // 2)
    assert post_stop_free < floor + group_needed  # Would fail without staged_ms credit!

    res = worker_diarization.run(
        tracks,
        dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
        lambda *_a, **_k: None,
        statvfs=lambda _p: SimpleNamespace(f_bavail=post_stop_free, f_frsize=1, f_blocks=10 << 30),
        diarization_store=staged_map,
    )
    assert res["diagnostics"] == []
    assert res["stagedGroupsReused"] == 1
    assert res["groupIds"] == ["others"]
    assert len(meter.open_files) == 0


def test_multi_group_remote_then_system_tight_disk_and_max_one_scratch_file(tmp_path, monkeypatch):
    """Multi-group G14 regression: remote -> system at reserve + 416,000 bytes never fails lowDisk
    and never holds > 1 group scratch TemporaryFile simultaneously, even when a later group is staged.
    """
    meter = _TempFileMeter(monkeypatch)
    scratch = tmp_path / "scratch"
    lib = tmp_path / "lib"
    lib.mkdir()
    reserve = 1 << 30
    unencumbered_free = reserve + 416_000  # Fits one 4 s group (needs 320,000 B), NOT two simultaneously!

    observed_open_counts: list[int] = []

    class GuardedNemotron(DeterministicNemotron):
        def infer(self, samples, progress):
            observed_open_counts.append(len(meter.open_files))
            assert len(meter.open_files) <= 1, "G14 violation: >1 scratch file open during Nemotron.infer"
            return super().infer(samples, progress)

    monkeypatch.setattr("cassini_talk.nemotron.Nemotron", GuardedNemotron)

    def tight_statvfs(_path):
        observed_open_counts.append(len(meter.open_files))
        return SimpleNamespace(
            f_bavail=max(0, unencumbered_free - meter.open_bytes()),
            f_frsize=1,
            f_blocks=10 << 30,
        )

    # Build a 4.0 s remote -> system meeting:
    # - 3.6 s pre-Stop -> 7 * 8192 = 57,344 frames = 229,376 bytes staged
    # - 0.4 s post-checkpoint tail -> total 4.0 s per group
    remote_pcm = make_speech_pcm16(48000, 1, [(1.3, 410.0, 0.3), (0.8, 0.0, 0.0), (1.3, 590.0, 0.3), (0.6, 0.0, 0.0)])
    system_pcm = make_speech_pcm16(48000, 1, [(1.2, 605.0, 0.3), (0.9, 0.0, 0.0), (1.3, 420.0, 0.3), (0.6, 0.0, 0.0)])
    split_bytes = int(3.6 * 48000) * 2
    tail_start_ms = 3600.0

    session = RecordingSession(lib, "MultiGroupRemoteSystem", source="chromium")
    session.add_track("remote-1", None, "remote", 48000, 1)
    session.add_track("system", None, "system", 48000, 1, filename="track-system.wav")
    append_pcm_chunks(session, "remote-1", remote_pcm[:split_bytes], rate=48000, channels=1, start_ms=0.0)
    append_pcm_chunks(session, "system", system_pcm[:split_bytes], rate=48000, channels=1, start_ms=0.0)
    session.checkpoint()

    opts = dict(modelPath="m", runtimePath="r", scratch=str(scratch))
    follower = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        "whistle@" + ("a" * 64),
        DeterministicDetector(),
        on_cache_entry=lambda _e: None,
        diarization_options=opts,
        statvfs=tight_statvfs,
    )
    assert follower.refresh()
    while follower.step():
        assert len(meter.open_files) <= 1
    staged_e2e = follower.export_diarization_staged()
    # SessionFollower aligns with canonical group_tracks order: remote-1 is selected first!
    assert (follower.resolved_dir, "remote-1") in staged_e2e
    staged_remote = next(iter(staged_e2e.values()))
    assert os.fstat(staged_remote.stream.fileno()).st_size == 229_376
    assert meter.max_open == 1

    append_pcm_chunks(session, "remote-1", remote_pcm[split_bytes:], rate=48000, channels=1, start_ms=tail_start_ms)
    append_pcm_chunks(session, "system", system_pcm[split_bytes:], rate=48000, channels=1, start_ms=tail_start_ms)
    session.finish()

    meeting = LibraryScanner(lib).scan().meetings[0]
    tracks, _speakers = prepare_tracks(meeting)
    assert [t["speaker"] for t in tracks] == ["track-remote-1", "others"]

    # 1) End-to-end SessionFollower -> worker_diarization.run at reserve + 416,000 bytes:
    events_e2e = []
    res_e2e = worker_diarization.run(
        tracks,
        opts,
        lambda kind, **kw: events_e2e.append(dict(type=kind, **kw)),
        statvfs=tight_statvfs,
        diarization_store=staged_e2e,
    )
    assert res_e2e["diagnostics"] == []
    assert res_e2e["stagedGroupsReused"] == 1
    assert res_e2e["groupIds"] == ["track-remote-1", "others"]
    assert meter.max_open == 1
    assert len(meter.open_files) == 0

    # Baseline after_stop batch at the exact same reserve + 416,000 bytes:
    meter.reset_max()
    events_batch = []
    res_batch = worker_diarization.run(
        tracks,
        opts,
        lambda kind, **kw: events_batch.append(dict(type=kind, **kw)),
        statvfs=tight_statvfs,
    )
    assert res_batch["diagnostics"] == []
    assert res_batch["stagedGroupsReused"] == 0
    assert res_batch["groupIds"] == ["track-remote-1", "others"]
    assert meter.max_open == 1
    assert len(meter.open_files) == 0

    diarized_e2e = [e for e in events_e2e if e["type"] == "diarized"]
    diarized_batch = [e for e in events_batch if e["type"] == "diarized"]
    assert [e["groupId"] for e in diarized_e2e] == ["track-remote-1", "others"]
    assert diarized_e2e == diarized_batch

    # 2) Direct later-group-staged case: groups = [track-remote-1, others] where ONLY the later group
    #    ("others", index 1) holds a 229,376-byte StagedDiarization (e.g., if remote-1 already crossed
    #    its first checkpoint before diarization became ready).
    def build_staged_for_track(track_id: str, filename: str, pcm_prefix: bytes, *, statvfs_fn=tight_statvfs):
        from cassini_talk.speech_pipeline import resampled_chunks

        stager = diarization_audio.TrackDiarizationStager(
            str(session.directory.resolve()),
            track_id,
            filename,
            opts,
            statvfs=statvfs_fn,
        )
        raw_sha = diarization_audio.hashlib.sha256(pcm_prefix).hexdigest()
        prov = (
            diarization_audio.StagedSpanProvenance(
                file=filename,
                start_offset_ms=0.0,
                rate=48000,
                channels=1,
                consumed_frames=len(pcm_prefix) // 2,
                raw_sha256=raw_sha,
                knots=None,
                ended=False,
            ),
        )
        block_n = diarization_audio.PREFIX_BLOCK_SAMPLES
        want_16k = 7 * block_n  # 57,344 frames = 229,376 bytes
        chunks_16k = []
        rem = want_16k
        for blk in resampled_chunks(str(session.directory / filename), start=0):
            take = min(len(blk), rem)
            if take > 0:
                chunks_16k.append(blk[:take])
                rem -= take
            if rem <= 0:
                break
        arr_16k = np.concatenate(chunks_16k).astype("<f4")
        for i in range(0, want_16k, block_n):
            assert stager.append_block(
                arr_16k[i : i + block_n].tobytes(),
                origin_16k=0,
                samples=i + block_n,
                ownership_windows=None,
                spans=prov,
            )
        staged_item = stager.export()
        assert staged_item is not None
        assert os.fstat(staged_item.stream.fileno()).st_size == 229_376
        return staged_item

    meter.reset_max()
    later_staged = build_staged_for_track("system", "track-system.wav", system_pcm[:split_bytes])
    later_store = {(str(session.directory.resolve()), "system"): later_staged}
    # Sanity check: while later_staged is open, remaining free space above reserve is 186,624 < 320,000!
    assert tight_statvfs(scratch).f_bavail - reserve == 186_624

    events_later = []
    res_later = worker_diarization.run(
        tracks,
        opts,
        lambda kind, **kw: events_later.append(dict(type=kind, **kw)),
        statvfs=tight_statvfs,
        diarization_store=later_store,
    )
    assert res_later["diagnostics"] == []
    assert res_later["stagedGroupsReused"] == 1
    assert res_later["groupIds"] == ["track-remote-1", "others"]
    assert [e["groupId"] for e in events_later if e["type"] == "diarized"] == ["track-remote-1", "others"]
    assert [e for e in events_later if e["type"] == "diarized"] == diarized_batch
    assert meter.max_open == 1
    assert len(meter.open_files) == 0
    assert max(observed_open_counts) <= 1

    # 3) Defensive store with >1 staged entry closes extra entries up front so <= 1 file stays open
    #    before budgeting/rejoin, AND later-group prefix mismatch closes its staged file before fallback!
    ample_statvfs = lambda _p: SimpleNamespace(f_bavail=10 << 30, f_frsize=1, f_blocks=20 << 30)  # noqa: E731
    s_remote = build_staged_for_track(
        "remote-1", Path(tracks[0]["path"]).name, remote_pcm[:split_bytes], statvfs_fn=ample_statvfs
    )
    s_system = build_staged_for_track("system", "track-system.wav", system_pcm[:split_bytes], statvfs_fn=ample_statvfs)
    assert len(meter.open_files) == 2  # Synthetic pre-call condition
    observed_open_counts.clear()
    res_defensive = worker_diarization.run(
        tracks,
        opts,
        lambda *_a, **_k: None,
        statvfs=tight_statvfs,
        diarization_store={
            (str(session.directory.resolve()), "remote-1"): s_remote,
            (str(session.directory.resolve()), "system"): s_system,
        },
    )
    assert res_defensive["diagnostics"] == []
    assert res_defensive["stagedGroupsReused"] == 1
    assert res_defensive["groupIds"] == ["track-remote-1", "others"]
    assert max(observed_open_counts) <= 1
    assert len(meter.open_files) == 0

    # Mutate system's prefix and verify later-group mismatch fallback ALSO succeeds at reserve + 416,000!
    meter.reset_max()
    bad_later = build_staged_for_track("system", "track-system.wav", system_pcm[:split_bytes])
    with (session.directory / "track-system.wav").open("r+b") as f:
        f.seek(44 + 1000)
        f.write(struct.pack("<h", 12000) * 200)
    events_fallback, events_mut_ref = [], []
    res_fallback = worker_diarization.run(
        tracks,
        opts,
        lambda kind, **kw: events_fallback.append(dict(type=kind, **kw)),
        statvfs=tight_statvfs,
        diarization_store={(str(session.directory.resolve()), "system"): bad_later},
    )
    res_mut_ref = worker_diarization.run(
        tracks,
        opts,
        lambda kind, **kw: events_mut_ref.append(dict(type=kind, **kw)),
        statvfs=tight_statvfs,
    )
    assert res_fallback["diagnostics"] == res_mut_ref["diagnostics"] == []
    assert res_fallback["stagedGroupsReused"] == 0
    assert res_fallback["groupIds"] == res_mut_ref["groupIds"] == ["track-remote-1", "others"]
    assert [e for e in events_fallback if e["type"] == "diarized"] == [
        e for e in events_mut_ref if e["type"] == "diarized"
    ]
    assert meter.max_open == 1
    assert len(meter.open_files) == 0


def test_paired_multi_group_remote_and_system_byte_identical_across_modes(tmp_path):
    """Full paired TranscriptionQueue integration test for remote -> system multi-group meetings."""
    bundle = FakeBundle.create(tmp_path)
    remote_pcm = make_speech_pcm16(
        48000, 1, [(1.2, 405.0, 0.3), (0.9, 0.0, 0.0), (1.2, 595.0, 0.3), (0.9, 0.0, 0.0), (0.6, 0.0, 0.0)]
    )
    system_pcm = make_speech_pcm16(
        48000, 1, [(1.2, 610.0, 0.3), (0.9, 0.0, 0.0), (1.2, 415.0, 0.3), (0.9, 0.0, 0.0), (0.6, 0.0, 0.0)]
    )
    split_bytes = int(4.2 * 48000) * 2

    def record_two_groups(session, queue, _tracker, live, _sig):
        session.add_track("remote-1", None, "remote", 48000, 1)
        session.add_track("system", None, "system", 48000, 1, filename="track-system.wav")
        append_pcm_chunks(session, "remote-1", remote_pcm[:split_bytes], rate=48000, channels=1, start_ms=0.0)
        append_pcm_chunks(session, "system", system_pcm[:split_bytes], rate=48000, channels=1, start_ms=0.0)
        session.checkpoint()
        if live:
            wait_on_queue(
                queue,
                lambda: cached_segment_count(session.directory, "whistle", bundle) >= 4,
                timeout=15.0,
                message="Follower did not precompute two-group meeting segments",
            )
        append_pcm_chunks(session, "remote-1", remote_pcm[split_bytes:], rate=48000, channels=1, start_ms=4200.0)
        append_pcm_chunks(session, "system", system_pcm[split_bytes:], rate=48000, channels=1, start_ms=4200.0)
        session.checkpoint()

    batch = _run_paired_lane(tmp_path, bundle, "whistle", False, record_two_groups)
    live = _run_paired_lane(tmp_path, bundle, "whistle", True, record_two_groups)
    _assert_lanes_byte_identical(batch, live)

    turns_doc = json.loads(live["turns_bytes"])
    assert [g["id"] for g in turns_doc["groups"]] == ["track-remote-1", "others"]
    assert live["tracker"]["completes"][0]["diarization"]["stagedGroupsReused"] == 1
    assert live["tracker"]["completes"][0]["diarization"]["warmModelReused"] is True


# ---------------------------------------------------------------------------
# 6. Symlink alias Trash/preempt, model_changed diarization toggle, SIGKILL recovery,
#    and GTK-thread Path.resolve() guard
# ---------------------------------------------------------------------------


def test_symlink_alias_trash_preempt_diarization_toggle_and_no_gtk_resolve(tmp_path, monkeypatch):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    sym_lib = tmp_path / "sym-lib"
    sym_lib.symlink_to(real_lib, target_is_directory=True)
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    models = FakeSpeechModels("whistle", tracker)

    class ToggleDiarizationModels:
        def __init__(self):
            self.ready = True

        def snapshot(self):
            return dict(ready=self.ready, downloaded=self.ready, busy=False)

        def request(self, _directory, *, origin="own"):
            if not self.ready:
                return None
            return dict(modelPath="/dev/null", runtimePath="/dev/null", origin=origin)

        def measured(self, _rtf):
            pass

    diar_models = ToggleDiarizationModels()
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=real_lib,
        models=models,
        diarization_models=diar_models,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=15,
    )
    session = RecordingSession(capture_parent(real_lib), "Symlink Meeting", source="gnome")
    sym_session_dir = sym_lib / ".cassini" / "sessions" / session.directory.name

    gtk_thread_id = threading.get_ident()
    orig_resolve = Path.resolve

    def guarded_resolve(self, *args, **kwargs):
        if threading.get_ident() == gtk_thread_id and "Recording-" in str(self):
            raise AssertionError(f"Path.resolve() called on GTK thread for {self}")
        return orig_resolve(self, *args, **kwargs)

    try:
        session.add_track("system", None, "system", 48000, 1, filename="track-system.wav")
        append_pcm_chunks(
            session,
            "system",
            make_speech_pcm16(48000, 1, [(1.2, 410.0, 0.3), (0.9, 0.0, 0.0), (1.2, 590.0, 0.3), (0.9, 0.0, 0.0)]),
            rate=48000,
            channels=1,
        )
        session.checkpoint()

        # Poison Path.resolve on the GTK (test main) thread across all GTK-facing entry points!
        monkeypatch.setattr(Path, "resolve", guarded_resolve)
        assert queue.follow_recording(sym_session_dir)
        queue.sync_recordings([sym_session_dir])
        assert queue.progress_for(sym_session_dir) is None
        assert not queue.stopping_meeting(sym_session_dir)

        wait_on_queue(
            queue,
            lambda: cached_segment_count(session.directory, "whistle", bundle) >= 2,
            timeout=15.0,
            message="Follower did not precompute via symlink alias",
        )
        assert queue._follow_diarization_ready is True

        # Toggling diarization readiness in Preferences preempts follower without Path.resolve on GTK!
        diar_models.ready = False
        queue.model_changed()
        wait_on_queue(queue, lambda: queue._follow_directory is None, message="Diarization toggle did not preempt")

        # Re-enable and re-follow via canonical path, then Trash via symlink alias on a worker thread!
        diar_models.ready = True
        assert queue.follow_recording(session.directory)
        wait_on_queue(queue, lambda: queue._follow_diarization_ready is True, message="Follower did not restart")

        monkeypatch.setattr(Path, "resolve", orig_resolve)
        with queue.cancel_meeting(sym_session_dir):
            assert queue._follow_directory is None
            assert queue._warm_client is None
            assert all(not c.is_alive() for c in tracker["clients"])
    finally:
        monkeypatch.setattr(Path, "resolve", orig_resolve)
        session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)


def test_worker_sigkill_after_diarization_staging_recovers_byte_identically(tmp_path):
    bundle = FakeBundle.create(tmp_path)
    spec = [(1.2, 410.0, 0.3), (0.9, 0.0, 0.0), (1.2, 590.0, 0.3), (0.9, 0.0, 0.0), (1.1, 420.0, 0.3)]
    pcm = make_speech_pcm16(48000, 2, spec)

    def record_and_kill(session, queue, tracker, live, sig):
        session.add_track("system", None, "system", 48000, 2, filename="track-system.wav")
        split = (len(pcm) // 4 // 2) * 4
        append_pcm_chunks(session, "system", pcm[:split], rate=48000, channels=2, start_ms=0.0)
        session.checkpoint()
        if live and sig is not None:
            wait_on_queue(
                queue,
                lambda: cached_segment_count(session.directory, "whistle", bundle) >= 1 and len(tracker["pids"]) == 1,
                timeout=15.0,
                message="Follower did not stage before SIGKILL",
            )
            os.kill(tracker["pids"][0], sig)
            wait_on_queue(queue, lambda: queue._follow_directory is None, message="Killed follower did not clean up")
        append_pcm_chunks(
            session, "system", pcm[split:], rate=48000, channels=2, start_ms=(split // 4) * 1000.0 / 48000.0
        )
        session.checkpoint()

    batch = _run_paired_lane(tmp_path, bundle, "whistle", False, record_and_kill)
    live = _run_paired_lane(tmp_path, bundle, "whistle", True, record_and_kill, inject_signal=signal.SIGKILL)
    _assert_lanes_byte_identical(batch, live)
    assert len(live["tracker"]["pids"]) == 2


# ---------------------------------------------------------------------------
# 7. Long-input linear scaling & O(1) tail append work-count test (BUG-CLASSES #1, #17, D88)
# ---------------------------------------------------------------------------


def test_diarization_staging_and_tail_rejoin_linear_scaling_cpu_budget(tmp_path, monkeypatch):
    """Verify deterministic O(N) staging and O(1) tail-rejoin work across rotated spans (D88: no timing ratios)."""
    real_read_wav = session_follower.read_wav_mono_f32
    real_resampler_feed = speech_pipeline.IncrementalResampler.feed
    real_append_block = diarization_audio.TrackDiarizationStager.append_block
    real_span_prov = diarization_audio.StagedSpanProvenance
    real_verify_prefix = diarization_audio._verify_wav_pcm_prefix

    def measure_work(sub_root: Path, n_spans: int) -> tuple[dict[str, int], dict[str, int]]:
        sub_root.mkdir()
        scratch = sub_root / "scratch"
        lib = sub_root / "lib"
        lib.mkdir()
        # Rotate every 1.0 s (96,000 bytes at 48 kHz mono PCM16) across n_spans spans!
        span_bytes = 48000 * 2
        session = RecordingSession(lib, f"Scale-{n_spans}", source="gnome")
        session.writer_factory = lambda p, r, c: WAVWriter(p, r, c, max_bytes=span_bytes)
        session.add_track("system", None, "system", 48000, 1, filename="track-system.wav")
        session.writers["system"] = RotatingWAV(session, "system", session.writers["system"])
        one_sec = make_speech_pcm16(48000, 1, [(0.4, 410.0, 0.25), (0.6, 0.0, 0.0)])
        for i in range(n_spans):
            session.append("system", one_sec, i * 1000.0)
        session.checkpoint()

        def fresh_counters() -> dict:
            return dict(
                wav_frames_read=0,
                wav_spans_read=set(),
                span_cursors={},
                resampler_calls=0,
                resampler_samples=0,
                staged_blocks=0,
                staged_bytes=0,
                provenance_builds=0,
                verify_prefix_calls=0,
                verify_prefix_frames=0,
            )

        active = fresh_counters()

        def counted_read_wav(dir_fd, filename, start_frame, count, channels, *, raw_hasher=None):
            prev_end = active["span_cursors"].get(filename, 0)
            assert start_frame >= prev_end, (
                f"Re-read already-processed frames in {filename}: start={start_frame} < prev_end={prev_end}"
            )
            active["span_cursors"][filename] = start_frame + count
            active["wav_frames_read"] += count
            active["wav_spans_read"].add(filename)
            return real_read_wav(dir_fd, filename, start_frame, count, channels, raw_hasher=raw_hasher)

        def counted_resampler_feed(self, samples):
            active["resampler_calls"] += 1
            active["resampler_samples"] += len(samples)
            return real_resampler_feed(self, samples)

        def counted_append_block(self, block_bytes, **kwargs):
            active["staged_blocks"] += 1
            active["staged_bytes"] += len(block_bytes)
            return real_append_block(self, block_bytes, **kwargs)

        def counted_span_prov(*args, **kwargs):
            active["provenance_builds"] += 1
            return real_span_prov(*args, **kwargs)

        def counted_verify_prefix(dir_fd, filename, expected_rate, expected_channels, consumed_frames, *args, **kwargs):
            active["verify_prefix_calls"] += 1
            active["verify_prefix_frames"] += consumed_frames
            return real_verify_prefix(
                dir_fd, filename, expected_rate, expected_channels, consumed_frames, *args, **kwargs
            )

        monkeypatch.setattr(session_follower, "read_wav_mono_f32", counted_read_wav)
        monkeypatch.setattr(speech_pipeline.IncrementalResampler, "feed", counted_resampler_feed)
        monkeypatch.setattr(diarization_audio.TrackDiarizationStager, "append_block", counted_append_block)
        monkeypatch.setattr(diarization_audio, "StagedSpanProvenance", counted_span_prov)
        monkeypatch.setattr(diarization_audio, "_verify_wav_pcm_prefix", counted_verify_prefix)

        follower = SessionFollower(
            session.directory,
            DeterministicRecognizer("whistle"),
            "whistle@" + ("a" * 64),
            DeterministicDetector(),
            on_cache_entry=lambda _e: None,
            diarization_options=dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
        )
        assert follower.refresh()
        while follower.step():
            pass
        staged_map = follower.export_diarization_staged()
        assert staged_map
        stage_work = {k: len(v) if isinstance(v, set) else v for k, v in active.items() if k != "span_cursors"}
        staged_preview = next(iter(staged_map.values()))
        assert stage_work["wav_frames_read"] <= n_spans * 48000
        assert stage_work["resampler_samples"] <= n_spans * 48000
        assert stage_work["staged_bytes"] == staged_preview.samples * 4
        assert stage_work["staged_blocks"] == staged_preview.samples // diarization_audio.PREFIX_BLOCK_SAMPLES

        # Append identical 1-second tail and measure post-Stop rejoin_with_staged work!
        session.append("system", one_sec, n_spans * 1000.0)
        session.finish()
        meeting = LibraryScanner(lib).scan().meetings[0]
        tracks, _ = prepare_tracks(meeting)
        staged = diarization_audio.pop_staged_for_track(staged_map, tracks[0])
        assert staged is not None
        staged_span_count = len(staged.spans)

        active = fresh_counters()
        with diarization_audio.rejoin_with_staged(tracks, staged=staged, scratch=scratch) as (
            _stream,
            _frames,
            _sha,
            reused,
            tail_frames,
        ):
            assert reused is True
            assert tail_frames < 2 * 16000
        rejoin_work = {k: len(v) if isinstance(v, set) else v for k, v in active.items() if k != "span_cursors"}
        assert rejoin_work["verify_prefix_calls"] == staged_span_count
        assert rejoin_work["verify_prefix_frames"] <= n_spans * 48000
        # Tail decoding/resampling must touch only the tail boundary, never re-reading already-staged prefix spans.
        assert rejoin_work["wav_spans_read"] <= 2
        assert rejoin_work["wav_frames_read"] < 2 * 48000
        assert rejoin_work["resampler_samples"] < 2 * 48000
        assert rejoin_work["resampler_calls"] <= 2
        return stage_work, rejoin_work

    stage_n, rejoin_n = measure_work(tmp_path / "n20", 20)
    stage_2n, rejoin_2n = measure_work(tmp_path / "n40", 40)

    # Staging work must scale linearly O(N) when doubling spans from N=20 to 2N=40 (well below quadratic 4x).
    for metric in (
        "wav_frames_read",
        "resampler_calls",
        "resampler_samples",
        "staged_blocks",
        "staged_bytes",
        "provenance_builds",
    ):
        assert stage_n[metric] > 0, f"Expected positive staging work for {metric}"
        assert stage_2n[metric] <= 2.2 * stage_n[metric], (
            f"Non-linear staging scaling for {metric}: N={stage_n[metric]}, 2N={stage_2n[metric]}"
        )

    # Prefix SHA verification scales O(N) linearly with staged spans; tail decode/resample stays O(1).
    assert rejoin_2n["verify_prefix_calls"] <= 2.2 * rejoin_n["verify_prefix_calls"]
    assert rejoin_2n["verify_prefix_frames"] <= 2.2 * rejoin_n["verify_prefix_frames"]
    assert rejoin_2n["wav_frames_read"] <= rejoin_n["wav_frames_read"] + 48000
    assert rejoin_2n["resampler_samples"] <= rejoin_n["resampler_samples"] + 48000
    assert rejoin_2n["resampler_calls"] <= rejoin_n["resampler_calls"] + 1


def test_worker_diarization_low_disk_and_disabled_paths_do_not_import_numpy(tmp_path):
    """Alpine/no-NumPy guard: complete(..., None, ...) and low-disk run() never require numpy."""
    code = """
import sys
from pathlib import Path
from types import SimpleNamespace
sys.modules["numpy"] = None
sys.path.insert(0, sys.argv[1])
from cassini_talk import worker_diarization

summary = worker_diarization.complete(dict(language="en"), [], None, lambda *_a, **_k: None, diarization_store={})
assert summary == dict(language="en")
low = lambda _p: SimpleNamespace(f_bavail=0, f_frsize=1, f_blocks=10 << 30)
tracks = [dict(id="room", path="/nonexistent/room.wav", frames=160000, sampleRate=16000, speaker="others", kind="system", offsetMs=0)]
res = worker_diarization.run(tracks, dict(scratch=sys.argv[2]), lambda *_a, **_k: None, statvfs=low)
assert res["groupIds"] == [] and res["diagnostics"][0]["reason"] == "lowDisk"
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1] / "src"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
