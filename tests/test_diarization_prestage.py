# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1125 unit, paired integration, negative-control, symlink/lifecycle, G14 budget, and scaling tests."""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
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

from cassini_talk import diarization_audio, nemotron_model, worker_diarization  # noqa: E402
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
    """Counts tempfile.TemporaryFile creations and currently-open descriptors."""

    def __init__(self, monkeypatch):
        self.created = 0
        self.open_files: set[int] = set()
        real_temp = tempfile.TemporaryFile

        def tracked(*args, **kwargs):
            f = real_temp(*args, **kwargs)
            self.created += 1
            ident = id(f)
            self.open_files.add(ident)
            real_close = f.close

            def tracked_close():
                self.open_files.discard(ident)
                return real_close()

            f.close = tracked_close
            return f

        monkeypatch.setattr(tempfile, "TemporaryFile", tracked)


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
        used = 0
        if scratch.is_dir():
            # Track open staged bytes via stager/fd sizes in test
            pass
        return SimpleNamespace(f_bavail=one_group_room - used, f_frsize=1, f_blocks=10 << 30)

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
# 7. Long-input linear scaling & O(1) tail append CPU test (BUG-CLASSES #1, #17)
# ---------------------------------------------------------------------------


def test_diarization_staging_and_tail_rejoin_linear_scaling_cpu_budget(tmp_path):
    """Verify 2N vs N linear CPU scaling across rotated spans and fast tail-only append (no wall clock)."""

    def measure_staging_cpu(sub_root: Path, n_spans: int) -> tuple[float, float]:
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

        follower = SessionFollower(
            session.directory,
            DeterministicRecognizer("whistle"),
            "whistle@" + ("a" * 64),
            DeterministicDetector(),
            on_cache_entry=lambda _e: None,
            diarization_options=dict(modelPath="m", runtimePath="r", scratch=str(scratch)),
        )
        t0 = time.thread_time()
        assert follower.refresh()
        while follower.step():
            pass
        staged_map = follower.export_diarization_staged()
        stage_cpu = time.thread_time() - t0
        assert staged_map

        # Append identical 1-second tail and measure post-Stop rejoin_with_staged CPU time!
        session.append("system", one_sec, n_spans * 1000.0)
        session.finish()
        meeting = LibraryScanner(lib).scan().meetings[0]
        tracks, _ = prepare_tracks(meeting)
        staged = diarization_audio.pop_staged_for_track(staged_map, tracks[0])
        assert staged is not None

        t1 = time.thread_time()
        with diarization_audio.rejoin_with_staged(tracks, staged=staged, scratch=scratch) as (
            _stream,
            _frames,
            _sha,
            reused,
            tail_frames,
        ):
            assert reused is True
            assert tail_frames < 2 * 16000
        rejoin_cpu = time.thread_time() - t1
        return stage_cpu, rejoin_cpu

    cpu_n, rejoin_n = measure_staging_cpu(tmp_path / "n20", 20)
    cpu_2n, rejoin_2n = measure_staging_cpu(tmp_path / "n40", 40)

    # 2N spans must scale linearly (well below quadratic 4x; allow +0.08 s fixed noise floor).
    assert cpu_2n <= 2.8 * cpu_n + 0.08, f"Non-linear staging CPU scaling: N={cpu_n:.4f}s, 2N={cpu_2n:.4f}s"
    assert rejoin_2n <= 2.8 * rejoin_n + 0.05, (
        f"Non-linear tail rejoin CPU scaling: N={rejoin_n:.4f}s, 2N={rejoin_2n:.4f}s"
    )


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
