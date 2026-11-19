# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1121 integration, paired replay, warm handoff, failure recovery, and scaling tests."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from durable import Finished, durable

np = pytest.importorskip("numpy")

from fake_follow_worker import DeterministicDetector, DeterministicRecognizer  # noqa: E402

from cassini_talk import portable, recognition_cache  # noqa: E402
from cassini_talk import speech_pipeline as pipeline  # noqa: E402
from cassini_talk.library import LibraryScanner  # noqa: E402
from cassini_talk.library_publication import (  # noqa: E402
    capture_parent,
    publish_stopped,
    register_publish_observer,
    unregister_publish_observer,
)
from cassini_talk.library_v2 import finalize  # noqa: E402
from cassini_talk.meeting_store import read_metadata  # noqa: E402
from cassini_talk.parakeet_model import MODEL_ID as PARAKEET_MODEL_ID  # noqa: E402
from cassini_talk.parakeet_model import MODEL_TAG as PARAKEET_MODEL_TAG  # noqa: E402
from cassini_talk.recording_session import RecordingSession, WAVWriter  # noqa: E402
from cassini_talk.rotating_wav import RotatingWAV  # noqa: E402
from cassini_talk.session_follower import SessionFollower  # noqa: E402
from cassini_talk.transcription import (  # noqa: E402
    RECOGNITION_CACHE_FILE,
    TranscriptionMode,
    TranscriptionQueue,
    recognition_cache_model,
    should_auto_write,
)
from cassini_talk.whistle_client import WhistleClient  # noqa: E402

WORKER_SCRIPT = Path(__file__).with_name("fake_follow_worker.py")


def wait_on_queue(queue, predicate, *, timeout=15.0, message="Condition not met"):
    with queue._idle:
        ok = queue._idle.wait_for(predicate, timeout=timeout)
    assert ok, message


def cached_segment_count(directory: Path, engine: str, bundle: Path) -> int:
    model_id = recognition_cache_model(PARAKEET_MODEL_ID if engine == "parakeet" else "whistle", bundle)
    return len(recognition_cache.read(directory / RECOGNITION_CACHE_FILE, model_id))


def make_speech_pcm16(rate: int, channels: int, segments_spec: list[tuple[float, float, float]]) -> bytes:
    """Synthesize deterministic PCM16 audio: list of (duration_s, freq_hz, amp)."""
    parts = []
    for dur_s, freq, amp in segments_spec:
        n = int(round(dur_s * rate))
        if amp == 0.0:
            mono = np.zeros(n, dtype=np.float64)
        else:
            t = np.arange(n, dtype=np.float64) / rate
            mono = amp * np.sin(2.0 * np.pi * freq * t)
        pcm = np.clip(np.round(mono * 32767.0), -32768, 32767).astype("<i2")
        if channels == 2:
            pcm = np.column_stack((pcm, pcm)).reshape(-1)
        parts.append(pcm)
    return np.concatenate(parts).tobytes() if parts else b""


class FakeBundle:
    """Minimal bundle dir with verified whistle.cact digest for WhistleClient tests."""

    @staticmethod
    def create(root: Path) -> Path:
        bundle = root / "fake-bundle"
        bundle.mkdir(exist_ok=True)
        (bundle / "pins.json").write_text(json.dumps({"installed": {"whistle.cact": "a" * 64}}))
        return bundle


class SubprocessTestClient(WhistleClient):
    """Uses the real WhistleClient IPC + real child process running fake_follow_worker.py."""

    def __init__(self, bundle, *, engine="whistle", timeout=30, tracker=None):
        self.bundle = Path(bundle)
        self.engine = engine
        self.timeout = timeout
        self.process = None
        self.selector = None
        self.ready = None
        self._buffer = b""
        self.cancelled = threading.Event()
        self._process_lock = threading.Lock()
        self._tracker = tracker
        self.last_complete = None
        self.last_planned = None

    def worker_command(self):
        return [sys.executable, str(WORKER_SCRIPT), self.engine, str(self.bundle)]

    def worker_environment(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return env

    def start(self):
        if self._tracker is not None:
            alive_before = [c.process.pid for c in self._tracker["clients"] if c is not self and c.is_alive()]
            self._tracker.setdefault("concurrent_alive_at_start", []).append(alive_before)
        super().start()
        if self._tracker is not None and self.process is not None:
            self._tracker["pids"].append(self.process.pid)
            self._tracker["clients"].append(self)
        return self

    def process_tracks(self, tracks, progress):
        def observed(event):
            if event.get("type") == "planned":
                self.last_planned = event
                if self._tracker is not None:
                    self._tracker["planned"].append(event)
            progress(event)

        result = super().process_tracks(tracks, observed)
        self.last_complete = result
        if self._tracker is not None:
            self._tracker["completes"].append(result)
        return result


class FakeSpeechModels:
    """Selects whistle or parakeet cleanly and creates real SubprocessTestClient children."""

    def __init__(self, selected: str, tracker: dict):
        self._selected = selected
        self._tracker = tracker

    def choice_for_job(self):
        return self._selected

    def snapshot(self):
        return dict(selected=self._selected, downloaded=True, busy=False)

    def select_for_test(self, value: str):
        self._selected = value

    def start_parakeet(self, bundle, *, register):
        if self._selected != PARAKEET_MODEL_ID:
            return None
        client = SubprocessTestClient(bundle, engine="parakeet", tracker=self._tracker)
        register(client)
        client.start()
        return client


class FakeDiarizationModels:
    def request(self, _directory, *, origin="own"):
        return dict(modelPath="/dev/null", runtimePath="/dev/null", origin=origin)

    def measured(self, _rtf):
        pass

    def snapshot(self):
        return dict(ready=True)


def _run_paired_lane(
    root: Path,
    bundle: Path,
    engine: str,
    live: bool,
    record_fn,
    *,
    diarize: bool = True,
    inject_signal: int | None = None,
    follow_stop_timeout: float = 3.0,
):
    """Run one complete RecordingSession -> publish_stopped -> TranscriptionQueue lane."""
    lib = root / ("live" if live else "batch")
    lib.mkdir()
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    selected = PARAKEET_MODEL_ID if engine == "parakeet" else "whistle"
    models = FakeSpeechModels(selected, tracker)
    finished = Finished()

    def whistle_factory(b, *, timeout=120):
        return SubprocessTestClient(b, engine="whistle", timeout=timeout, tracker=tracker)

    mode = TranscriptionMode.TRANSCRIBE_WHILE_RECORDING if live else TranscriptionMode.AFTER_STOP
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        models=models,
        client_factory=whistle_factory,
        diarization_models=FakeDiarizationModels() if diarize else None,
        mode=mode,
        follow_stop_timeout=follow_stop_timeout,
        follow_poll_interval_ms=15,
    )
    session = RecordingSession(capture_parent(lib), "Paired Meeting", source="gnome")
    try:
        if live:
            assert queue.follow_recording(session.directory)
        record_fn(session, queue, tracker, live, inject_signal)
        session.finish()
        audio_file = publish_stopped(session)
        assert audio_file is not None
        assert queue.enqueue(session.directory)
        finished.wait(1, bound=60)
        meta = read_metadata(session.directory)
        assert meta["transcript"]["status"] == "done", meta
        transcript_bytes = (session.directory / "transcript.json").read_bytes()
        turns_path = session.directory / "speaker-turns.json"
        turns_bytes = turns_path.read_bytes() if turns_path.is_file() else b""
        embedded = portable.read_meeting(str(lib / audio_file.name))
        assert embedded.is_readable
        reopened_words = portable.load_words(embedded, embedded.default_transcript_id)
        opus_sha = embedded.manifest["integrity"]["opusAudioSha256"]
        return dict(
            tracker=tracker,
            transcript_bytes=transcript_bytes,
            turns_bytes=turns_bytes,
            reopened_words=reopened_words,
            reopened_speakers=embedded.speakers,
            opus_sha=opus_sha,
            session_dir=session.directory,
            meta=meta,
        )
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)


def _assert_lanes_byte_identical(batch: dict, live: dict):
    assert live["opus_sha"] == batch["opus_sha"], "Published .opus payload digest differs"
    assert live["transcript_bytes"] == batch["transcript_bytes"], "transcript.json differs"
    assert live["turns_bytes"] == batch["turns_bytes"], "speaker-turns.json differs"
    assert live["reopened_words"] == batch["reopened_words"], "Reopened portable words differ"
    assert live["reopened_speakers"] == batch["reopened_speakers"], "Reopened portable speakers differ"


# ---------------------------------------------------------------------------
# 1. TranscriptionMode seam & zero workers during record_only / after_stop
# ---------------------------------------------------------------------------


def test_transcription_mode_normalization_and_record_only_policy(tmp_path):
    assert TranscriptionMode.parse("recordOnly") is TranscriptionMode.RECORD_ONLY
    assert TranscriptionMode.parse("record_only") is TranscriptionMode.RECORD_ONLY
    assert TranscriptionMode.parse("afterStop") is TranscriptionMode.AFTER_STOP
    assert TranscriptionMode.parse("after_stop") is TranscriptionMode.AFTER_STOP
    assert TranscriptionMode.parse("transcribeWhileRecording") is TranscriptionMode.TRANSCRIBE_WHILE_RECORDING
    assert TranscriptionMode.parse("transcribe_while_recording") is TranscriptionMode.TRANSCRIBE_WHILE_RECORDING
    for bad in (None, "", "fast", "AFTER_STOP", "RecordOnly", 42, True):
        with pytest.raises(ValueError):
            TranscriptionMode.parse(bad)

    lib = tmp_path / "lib"
    lib.mkdir()
    session = RecordingSession(lib, "Battery saving meeting", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
    session.finish()
    meeting = LibraryScanner(lib).scan().meetings[0]
    assert not should_auto_write(meeting, {}, mode=TranscriptionMode.RECORD_ONLY)
    assert should_auto_write(meeting, {}, mode=TranscriptionMode.AFTER_STOP)
    assert should_auto_write(meeting, {}, mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING)
    assert meeting.transcript == "none"

    # Explicit manual enqueue still transcribes cleanly even in record_only mode.
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.RECORD_ONLY,
    )
    try:
        assert queue.enqueue(session.directory)
        finished.wait(1, bound=30)
        updated = LibraryScanner(lib).scan().meetings[0]
        assert updated.transcript == "done" and len(updated.words) > 0
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)


@pytest.mark.parametrize("mode", [TranscriptionMode.RECORD_ONLY, TranscriptionMode.AFTER_STOP])
@pytest.mark.parametrize("engine", ["whistle", "parakeet"])
def test_record_only_and_after_stop_spawn_zero_workers_during_capture(tmp_path, mode, engine):
    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    selected = PARAKEET_MODEL_ID if engine == "parakeet" else "whistle"
    models = FakeSpeechModels(selected, tracker)
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        models=models,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=mode,
    )
    session = RecordingSession(capture_parent(lib), "Silent mode", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 48000, 1, filename="track-microphone.wav")
        session.append("microphone", make_speech_pcm16(48000, 1, [(1.2, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        assert not queue.follow_recording(session.directory)
        queue.sync_recordings([session.directory])
        assert not queue.notify_session_finished(session.directory)
        assert tracker["pids"] == [], f"{mode.value} spawned worker(s) during capture!"
        assert not (session.directory / RECOGNITION_CACHE_FILE).exists()
    finally:
        session.finish()
        queue.close()


# ---------------------------------------------------------------------------
# 2. Pure v2 segment-PCM cache finalization & rejection checks
# ---------------------------------------------------------------------------


def test_v2_pure_cache_finalization_and_rejection(tmp_path, monkeypatch):
    import wave

    wav_path = tmp_path / "seg.wav"
    pcm = make_speech_pcm16(16000, 1, [(0.6, 440.0, 0.3), (1.0, 0.0, 0.0), (0.6, 660.0, 0.3)])
    with wave.open(str(wav_path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(pcm)

    monkeypatch.setattr(pipeline, "SpeechDetector", DeterministicDetector)
    model_a = DeterministicRecognizer("whistle")
    cache_path = tmp_path / RECOGNITION_CACHE_FILE
    model_id = "whistle@" + ("b" * 64)

    # First run populates v2 pure cache at offsetMs=0.
    events_a = []
    pipeline.process_tracks(
        model_a,
        [dict(id="t1", path=str(wav_path), offsetMs=0, frames=len(pcm) // 2, sampleRate=16000)],
        None,
        lambda kind, **kw: events_a.append(dict(type=kind, **kw)),
        recognition_cache=dict(path=str(cache_path), model=model_id),
    )
    entries = {e["cacheEntry"]["key"]: e["cacheEntry"] for e in events_a if "cacheEntry" in e}
    assert len(entries) == 2 and model_a.calls == 2
    cache_path.write_text(
        json.dumps(
            recognition_cache.document(
                model_id,
                {k: {name: v[name] for name in ("text", "language", "words")} for k, v in entries.items()},
            )
        )
    )

    # Reuse the EXACT SAME pure cache at a NON-ZERO offsetMs=5250 with sessionPresence filtering!
    model_hit = DeterministicRecognizer("whistle")
    presence = [[5250 * 16, 6500 * 16]]  # Keeps only segment 1 words, filters segment 2!
    events_hit = []
    pipeline.process_tracks(
        model_hit,
        [
            dict(
                id="t1",
                path=str(wav_path),
                offsetMs=5250,
                frames=len(pcm) // 2,
                sampleRate=16000,
                sessionPresence=presence,
            )
        ],
        None,
        lambda kind, **kw: events_hit.append(dict(type=kind, **kw)),
        recognition_cache=dict(path=str(cache_path), model=model_id),
    )
    assert model_hit.calls == 0, "v2 pure cache must hit across non-zero offsetMs and presence"

    # Compare against uncached run at the exact same offsetMs=5250 + presence!
    model_ref = DeterministicRecognizer("whistle")
    events_ref = []
    pipeline.process_tracks(
        model_ref,
        [
            dict(
                id="t1",
                path=str(wav_path),
                offsetMs=5250,
                frames=len(pcm) // 2,
                sampleRate=16000,
                sessionPresence=presence,
            )
        ],
        None,
        lambda kind, **kw: events_ref.append(dict(type=kind, **kw)),
    )
    assert [e["words"] for e in events_hit if e["type"] == "progress"] == [
        e["words"] for e in events_ref if e["type"] == "progress"
    ]

    # Reject legacy v1 format (D51), model mismatch, corrupted JSON, and invalid word timestamps.
    v1_path = tmp_path / "v1.json"
    v1_path.write_text(json.dumps(dict(format="cassini.recognition-cache.v1", model=model_id, segments={})))
    assert recognition_cache.read(v1_path, model_id) == {}
    assert recognition_cache.read(cache_path, PARAKEET_MODEL_TAG) == {}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{truncated")
    assert recognition_cache.read(corrupt, model_id) == {}

    bad_words_path = tmp_path / "bad_words.json"
    bad_key = recognition_cache.segment_key("0" * 64, 16000)
    bad_words_path.write_text(
        json.dumps(
            recognition_cache.document(
                model_id,
                {
                    bad_key: dict(
                        text="bad",
                        language="en",
                        words=[dict(start=0.5, end=0.2, text="bad")],
                    )
                },
            )
        )
    )
    assert recognition_cache.read(bad_words_path, model_id) == {}


# ---------------------------------------------------------------------------
# 3. Paired replay: single-track & simultaneous two-track + warm handoff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["whistle", "parakeet"])
@pytest.mark.parametrize("two_tracks", [False, True], ids=["single-track", "two-track"])
def test_paired_replay_single_and_two_track_warm_handoff(tmp_path, engine, two_tracks):
    bundle = FakeBundle.create(tmp_path)
    spec_sys = [(1.2, 440.0, 0.3), (0.9, 0.0, 0.0), (1.1, 550.0, 0.3), (0.9, 0.0, 0.0), (0.8, 330.0, 0.3)]
    spec_mic = [(0.5, 0.0, 0.0), (1.0, 620.0, 0.3), (0.9, 0.0, 0.0), (1.0, 480.0, 0.3), (0.7, 0.0, 0.0)]
    pcm_sys = make_speech_pcm16(48000, 2, spec_sys)
    pcm_mic = make_speech_pcm16(48000, 1, spec_mic)

    def record(session, queue, tracker, live, _sig):
        session.add_track("system", None, "system", 48000, 2, filename="track-system.wav")
        if two_tracks:
            session.add_track("microphone", None, "microphone", 48000, 1, filename="track-microphone.wav")
        step_sys = 48000 * 2 * 2 // 4  # 0.25 s
        step_mic = 48000 * 1 * 2 // 4
        pos_sys = pos_mic = 0
        t_ms = 0.0
        while pos_sys < len(pcm_sys) or (two_tracks and pos_mic < len(pcm_mic)):
            if pos_sys < len(pcm_sys):
                chunk = pcm_sys[pos_sys : pos_sys + step_sys]
                session.append("system", chunk, t_ms)
                pos_sys += len(chunk)
            if two_tracks and pos_mic < len(pcm_mic):
                chunk = pcm_mic[pos_mic : pos_mic + step_mic]
                session.append("microphone", chunk, t_ms)
                pos_mic += len(chunk)
            t_ms += 250.0
        session.checkpoint()
        if live:
            # Wait deterministically until pre-Stop follower caches at least 2 segments (no wall-clock sleep).
            wait_on_queue(
                queue,
                lambda: cached_segment_count(session.directory, engine, bundle) >= (3 if two_tracks else 2),
                message="Follower did not precompute closed segments before Stop",
            )

    batch = _run_paired_lane(tmp_path, bundle, engine, False, record)
    live = _run_paired_lane(tmp_path, bundle, engine, True, record)
    _assert_lanes_byte_identical(batch, live)

    # Warm handoff: exact same child PID served follow AND post-Stop pipeline!
    assert len(live["tracker"]["pids"]) == 1, f"Expected 1 warm worker PID, got {live['tracker']['pids']}"
    assert live["tracker"]["completes"][0]["workerPid"] == live["tracker"]["pids"][0]
    assert live["tracker"]["planned"][0]["prefixesReused"] == (2 if two_tracks else 1)
    assert live["tracker"]["completes"][0]["postStopAsrCalls"] < batch["tracker"]["completes"][0]["postStopAsrCalls"]
    assert live["tracker"]["completes"][0]["postStopVadFrames"] < batch["tracker"]["completes"][0]["postStopVadFrames"]


# ---------------------------------------------------------------------------
# 4. Multi-track earliest-session-time fairness & bounded memory
# ---------------------------------------------------------------------------


def test_multi_track_earliest_session_time_fairness_and_bounded_memory(tmp_path):
    session = RecordingSession(tmp_path, "Fairness", source="chromium")
    session.add_track("microphone", None, "microphone", 48000, 1, filename="track-microphone.wav")
    session.add_track("system", None, "system", 48000, 1, filename="track-system.wav")
    session.add_track("remote-late", None, "remote", 48000, 1)

    pcm_a = make_speech_pcm16(48000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0), (1.0, 520.0, 0.3), (0.8, 0.0, 0.0)])
    pcm_b = make_speech_pcm16(48000, 1, [(1.0, 330.0, 0.3), (0.8, 0.0, 0.0), (1.0, 610.0, 0.3), (0.8, 0.0, 0.0)])
    pcm_c = make_speech_pcm16(48000, 1, [(1.0, 700.0, 0.3), (0.8, 0.0, 0.0)])
    step = 48000 * 2 // 2  # 0.5 s chunks (< MAX_CHUNK)
    for tid, pcm, base_ms in (("microphone", pcm_a, 0.0), ("system", pcm_b, 0.0), ("remote-late", pcm_c, 1500.0)):
        for off in range(0, len(pcm), step):
            session.append(tid, pcm[off : off + step], base_ms + (off // 2) * 1000.0 / 48000.0)
    session.checkpoint()

    detector = DeterministicDetector()
    model = DeterministicRecognizer("whistle")
    cached = []
    follower = SessionFollower(
        session.directory,
        model,
        "whistle@" + ("a" * 64),
        detector,
        on_cache_entry=cached.append,
        record_trace=True,
    )
    follower.refresh()

    max_ready = 0
    steps = 0
    while follower.step():
        steps += 1
        for tr in follower.tracks.values():
            max_ready = max(max_ready, len(tr.ready_segments))
            # Open utterance pieces never exceed max_seconds (15 s + small margin).
            open_samples = sum(len(p) for p in tr._open_pieces)
            assert open_samples <= 16 * 16000
    session.finish()

    assert max_ready <= 1, "At most one bounded ready segment per active track in RAM"
    assert len(cached) >= 4
    # Verify earliest-session-time interleaving: microphone and system alternate before remote-late at 1500 ms.
    advances = [(tid, ms) for tid, action, ms in follower.schedule_trace if action == "advance"]
    early_tracks = [tid for tid, ms in advances if ms < 1500]
    assert "microphone" in early_tracks and "system" in early_tracks and "remote-late" not in early_tracks
    # Neither microphone nor system should starve the other: max consecutive advances by one track <= 2.
    run = max_run = 0
    prev = None
    for tid in early_tracks:
        run = run + 1 if tid == prev else 1
        max_run = max(max_run, run)
        prev = tid
    assert max_run <= 2, f"Track starvation detected in schedule trace: {early_tracks}"


# ---------------------------------------------------------------------------
# 5. Native clock drift (+100 ppm across multiple 60 s anchors) + negative control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["whistle", "parakeet"])
def test_native_clock_drift_multi_minute_and_negative_control(tmp_path, engine):
    bundle = FakeBundle.create(tmp_path)
    drift_ppm = 100

    # Positive control: 130 s stereo 48 kHz with utterances in minute 1, spanning 60 s boundary, and in minute 2.
    # Keep total speech compact (sparse utterances separated by silence) so test executes fast (~1 s).
    spec_long = [
        (1.0, 440.0, 0.3),
        (57.5, 0.0, 0.0),
        (2.5, 520.0, 0.3),  # Spans right across the 60.0 s anchor boundary!
        (1.0, 0.0, 0.0),
        (1.0, 610.0, 0.3),
        (60.0, 0.0, 0.0),
        (1.0, 390.0, 0.3),
        (6.0, 0.0, 0.0),
    ]
    pcm_long = make_speech_pcm16(48000, 2, spec_long)

    def record_long_drift(session, queue, tracker, live, _sig):
        session.add_track("system", None, "system", 48000, 2, filename="track-system.wav")
        # Append in 5-second blocks with +100 ppm synthetic device clock drift.
        block_frames = 48000 * 5
        frame_bytes = 2 * 2
        frame_cursor = 0
        total_frames = len(pcm_long) // frame_bytes
        while frame_cursor < total_frames:
            take = min(block_frames, total_frames - frame_cursor)
            chunk = pcm_long[frame_cursor * frame_bytes : (frame_cursor + take) * frame_bytes]
            offset_ms = frame_cursor * 1000.0 / 48000.0
            host_ns = session._native_clock_origin_ns + round(offset_ms * (1_000_000 + drift_ppm))
            session.append_native("system", chunk, offset_ms, host_ns=host_ns)
            frame_cursor += take
            if frame_cursor in (48000 * 65, 48000 * 125):
                session.checkpoint()
        session.checkpoint()
        if live:
            # Expect segments from BOTH minute 1 (t=0..1s) AND spanning/minute 2 (t=58.5..63s) cached BEFORE Stop!
            wait_on_queue(
                queue,
                lambda: cached_segment_count(session.directory, engine, bundle) >= 3,
                timeout=20.0,
                message="Drifted multi-minute follower did not decode across consecutive 60 s fixed intervals",
            )

    pos_root = tmp_path / "pos"
    pos_root.mkdir()
    batch = _run_paired_lane(pos_root, bundle, engine, False, record_long_drift)
    live = _run_paired_lane(pos_root, bundle, engine, True, record_long_drift)
    _assert_lanes_byte_identical(batch, live)
    assert len(live["tracker"]["pids"]) == 1
    assert live["tracker"]["planned"][0]["prefixesReused"] == 1
    assert live["tracker"]["completes"][0]["postStopAsrCalls"] < batch["tracker"]["completes"][0]["postStopAsrCalls"]

    # Negative control: < 60 s (+100 ppm drifted) recording MUST get 0 pre-Stop cache entries and match batch!
    spec_short = [(1.2, 440.0, 0.3), (1.0, 0.0, 0.0), (1.2, 550.0, 0.3), (1.0, 0.0, 0.0)]
    pcm_short = make_speech_pcm16(48000, 2, spec_short)
    pre_stop_cache_existed = []

    def record_short_drift(session, queue, tracker, live, _sig):
        session.add_track("system", None, "system", 48000, 2, filename="track-system.wav")
        frame_bytes = 4
        block_frames = 48000
        frame_cursor = 0
        total_frames = len(pcm_short) // frame_bytes
        while frame_cursor < total_frames:
            take = min(block_frames, total_frames - frame_cursor)
            chunk = pcm_short[frame_cursor * frame_bytes : (frame_cursor + take) * frame_bytes]
            offset_ms = frame_cursor * 1000.0 / 48000.0
            host_ns = session._native_clock_origin_ns + round(offset_ms * (1_000_000 + drift_ppm))
            session.append_native("system", chunk, offset_ms, host_ns=host_ns)
            frame_cursor += take
        session.checkpoint()
        if live:
            # Wait for the worker process to be ready, then verify SessionFollower directly produces 0 segments.
            wait_on_queue(queue, lambda: len(tracker["pids"]) == 1, message="Follower worker did not start")
            direct = SessionFollower(
                session.directory,
                DeterministicRecognizer(engine),
                PARAKEET_MODEL_TAG if engine == "parakeet" else "whistle@" + ("a" * 64),
                DeterministicDetector(),
                on_cache_entry=lambda _e: pre_stop_cache_existed.append(True),
            )
            direct.refresh()
            while direct.step():
                pass
            assert not pre_stop_cache_existed, "< 60 s drifted recording must produce 0 pre-Stop cache entries"
            assert not (session.directory / RECOGNITION_CACHE_FILE).exists()

    neg_root = tmp_path / "neg"
    neg_root.mkdir()
    neg_batch = _run_paired_lane(neg_root, bundle, engine, False, record_short_drift)
    neg_live = _run_paired_lane(neg_root, bundle, engine, True, record_short_drift)
    _assert_lanes_byte_identical(neg_batch, neg_live)


# ---------------------------------------------------------------------------
# 6. RotatingWAV span rotation mid-utterance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["whistle", "parakeet"])
def test_rotating_wav_mid_utterance_paired_equivalence(tmp_path, engine):
    bundle = FakeBundle.create(tmp_path)
    # 3.0 s continuous utterance + silence + 1.0 s utterance.
    # Rotate WAV every ~1.5 s (288,000 bytes at 48 kHz stereo PCM16) -> rotation lands RIGHT mid-utterance!
    spec = [(3.0, 440.0, 0.3), (1.0, 0.0, 0.0), (1.2, 560.0, 0.3), (0.8, 0.0, 0.0)]
    pcm = make_speech_pcm16(48000, 2, spec)
    max_span_bytes = 48000 * 4 * 3 // 2  # 1.5 s per physical WAV span

    def record_rotating(session, queue, tracker, live, _sig):
        session.writer_factory = lambda path, rate, ch: WAVWriter(path, rate, ch, max_bytes=max_span_bytes)
        session.add_track("microphone", None, "microphone", 48000, 2, filename="track-microphone.wav")
        session.writers["microphone"] = RotatingWAV(session, "microphone", session.writers["microphone"])
        step = 48000 * 4 // 4  # 0.25 s chunks
        pos = 0
        t_ms = 0.0
        while pos < len(pcm):
            chunk = pcm[pos : pos + step]
            session.append("microphone", chunk, t_ms)
            pos += len(chunk)
            t_ms += 250.0
        session.checkpoint()
        assert len(session.manifest["tracks"][0].get("spans", [])) >= 3, "Expected >=3 rotated physical spans"
        if live:
            wait_on_queue(
                queue,
                lambda: cached_segment_count(session.directory, engine, bundle) >= 2,
                message="RotatingWAV follower did not decode across mid-utterance rotation",
            )

    batch = _run_paired_lane(tmp_path, bundle, engine, False, record_rotating)
    live = _run_paired_lane(tmp_path, bundle, engine, True, record_rotating)
    _assert_lanes_byte_identical(batch, live)
    assert len(live["tracker"]["pids"]) == 1
    assert live["tracker"]["planned"][0]["prefixesReused"] == 1
    assert live["tracker"]["completes"][0]["postStopAsrCalls"] < batch["tracker"]["completes"][0]["postStopAsrCalls"]


# ---------------------------------------------------------------------------
# 7. Companion ownershipWindows switches & non-zero offsetMs (+ retroactive cut fallback)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["whistle", "parakeet"])
def test_companion_ownership_windows_and_nonzero_offset(tmp_path, engine):
    bundle = FakeBundle.create(tmp_path)
    # Track starts at non-zero offsetMs=15325.5. Ownership switches off at 17500 ms and back on at 19500 ms!
    spec = [
        (1.0, 440.0, 0.3),  # 15325.5..16325.5 ms (owned)
        (0.9, 0.0, 0.0),  # 16325.5..17225.5 ms
        (1.2, 520.0, 0.3),  # 17225.5..18425.5 ms (cut by ownership off at 17500 ms!)
        (1.2, 0.0, 0.0),  # 18425.5..19625.5 ms (ownership back on at 19500 ms)
        (1.0, 620.0, 0.3),  # 19625.5..20625.5 ms (owned)
        (0.9, 0.0, 0.0),
    ]
    pcm = make_speech_pcm16(48000, 1, spec)
    base_offset = 15325.5

    def record_companion(session, queue, tracker, live, _sig):
        session.manifest["source"] = "chromium"
        session.add_track("remote-1", None, "remote", 48000, 1)
        session.manifest["events"] = [
            dict(type="selection", atMs=0, me=True, others=True),
            dict(type="othersSource", atMs=0, source="browser"),
        ]
        session.checkpoint()

        step = 48000 * 2 // 4  # 0.25 s
        pos = 0
        rel_ms = 0.0
        switched_off = False
        switched_on = False
        while pos < len(pcm):
            at_ms = base_offset + rel_ms
            if not switched_off and at_ms >= 17500.0:
                session.manifest["events"].append(dict(type="othersSource", atMs=17500, source="system"))
                session.checkpoint()
                switched_off = True
            if not switched_on and at_ms >= 19500.0:
                session.manifest["events"].append(dict(type="othersSource", atMs=19500, source="browser"))
                session.checkpoint()
                switched_on = True
            chunk = pcm[pos : pos + step]
            session.append("remote-1", chunk, at_ms)
            pos += len(chunk)
            rel_ms += 250.0
        session.checkpoint()
        if live:
            wait_on_queue(
                queue,
                lambda: cached_segment_count(session.directory, engine, bundle) >= 2,
                message="Companion follower did not precompute non-zero offsetMs segments",
            )

    batch = _run_paired_lane(tmp_path, bundle, engine, False, record_companion)
    live = _run_paired_lane(tmp_path, bundle, engine, True, record_companion)
    _assert_lanes_byte_identical(batch, live)
    assert len(live["tracker"]["pids"]) == 1
    assert live["tracker"]["completes"][0]["postStopAsrCalls"] < batch["tracker"]["completes"][0]["postStopAsrCalls"]


def test_retroactive_ownership_cut_invalidates_prefix_and_falls_back_cleanly(tmp_path, monkeypatch):
    import wave

    wav_path = tmp_path / "retro.wav"
    pcm = make_speech_pcm16(16000, 1, [(1.5, 440.0, 0.3), (0.8, 0.0, 0.0), (1.0, 520.0, 0.3)])
    with wave.open(str(wav_path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(pcm)

    # Build a prefix assuming full ownership, then run process_tracks with a retroactive cut at 500..1000 ms!
    detector = DeterministicDetector()
    session = RecordingSession(tmp_path, "Retro", source="chromium")
    session.add_track("system", None, "system", 16000, 1, filename="track-system.wav")
    session.append("system", pcm, 0.0)
    session.checkpoint()
    follower = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        "whistle@" + ("a" * 64),
        detector,
        on_cache_entry=lambda _: None,
    )
    follower.refresh()
    while follower.step():
        pass
    prefixes = follower.export_prefixes()
    session.finish()
    assert len(prefixes) > 0

    # Final track has a retroactive ownership cut inside the prefix (500..1000 ms excluded).
    track = dict(
        id="track-system.wav",
        captureTrackId="system",
        path=str(session.directory / "track-system.wav"),
        sessionDirectory=str(session.directory),
        offsetMs=0,
        frames=len(pcm) // 2,
        sampleRate=16000,
        ownershipWindows=[[0, 500], [1000, None]],
    )
    monkeypatch.setattr(pipeline, "SpeechDetector", DeterministicDetector)
    planned_events = []
    pipeline.process_tracks(
        DeterministicRecognizer("whistle"),
        [track],
        None,
        lambda kind, **kw: planned_events.append(dict(type=kind, **kw)),
        detector=DeterministicDetector(),
        prefix_store=prefixes,
    )
    planned = next(e for e in planned_events if e["type"] == "planned")
    assert planned["prefixesReused"] == 0, "Retroactive ownership cut must fall back to full segment_ranges"

    # Reference without prefix_store must produce identical progress words.
    ref_events = []
    pipeline.process_tracks(
        DeterministicRecognizer("whistle"),
        [track],
        None,
        lambda kind, **kw: ref_events.append(dict(type=kind, **kw)),
    )
    assert [e["words"] for e in planned_events if e["type"] == "progress"] == [
        e["words"] for e in ref_events if e["type"] == "progress"
    ]


# ---------------------------------------------------------------------------
# 8. Mid-recording SIGKILL / SIGSTOP resilience & recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["whistle", "parakeet"])
@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGSTOP], ids=["SIGKILL", "SIGSTOP"])
def test_mid_recording_sigkill_and_sigstop_recovery(tmp_path, engine, sig):
    bundle = FakeBundle.create(tmp_path)
    part1 = make_speech_pcm16(48000, 1, [(1.1, 440.0, 0.3), (0.9, 0.0, 0.0)])
    part2 = make_speech_pcm16(48000, 1, [(1.1, 610.0, 0.3), (0.9, 0.0, 0.0)])

    def record_with_signal(session, queue, tracker, live, inject_sig):
        session.add_track("microphone", None, "microphone", 48000, 1, filename="track-microphone.wav")
        session.append("microphone", part1, 0.0)
        session.checkpoint()
        if live and inject_sig is not None:
            wait_on_queue(
                queue,
                lambda: cached_segment_count(session.directory, engine, bundle) >= 1,
                message="Follower did not cache first segment before signal injection",
            )
            pid = tracker["pids"][0]
            os.kill(pid, inject_sig)
            if inject_sig == signal.SIGKILL:
                wait_on_queue(
                    queue, lambda: queue._follow_directory is None, message="Queue did not clean up after SIGKILL"
                )
        # Capture continues completely untouched after worker SIGKILL/SIGSTOP!
        session.append("microphone", part2, 2000.0)
        session.checkpoint()

    batch = _run_paired_lane(tmp_path, bundle, engine, False, record_with_signal)
    live = _run_paired_lane(
        tmp_path,
        bundle,
        engine,
        True,
        record_with_signal,
        inject_signal=sig,
        follow_stop_timeout=0.3,  # Short bounded drain deadline for SIGSTOP recovery
    )
    _assert_lanes_byte_identical(batch, live)
    # A replacement worker ran post-Stop AND still got a cache hit for part1!
    assert len(live["tracker"]["pids"]) == 2
    assert live["tracker"]["completes"][0]["postStopAsrCalls"] < batch["tracker"]["completes"][0]["postStopAsrCalls"]


# ---------------------------------------------------------------------------
# 9. Overlap read-only planning/ASR with Opus audio publication
# ---------------------------------------------------------------------------


def test_overlap_readonly_planning_asr_with_opus_publication(tmp_path, monkeypatch):
    bundle = FakeBundle.create(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    finished = Finished()

    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=15,
    )

    def on_publish(directory):
        queue.notify_session_finished(directory)

    register_publish_observer(on_publish)

    opus_entered = threading.Event()
    opus_release = threading.Event()
    orig_build = finalize.build

    def gated_build(*args, **kwargs):
        opus_entered.set()
        durable(opus_release, "test did not release Opus publication")
        return orig_build(*args, **kwargs)

    monkeypatch.setattr(finalize, "build", gated_build)
    session = RecordingSession(capture_parent(lib), "Overlap Meeting", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 48000, 1, filename="track-microphone.wav")
        pcm = make_speech_pcm16(48000, 1, [(1.1, 440.0, 0.3), (0.9, 0.0, 0.0), (1.0, 550.0, 0.3)])
        step = 48000 * 2 // 4
        for off in range(0, len(pcm), step):
            session.append("microphone", pcm[off : off + step], (off // 2) * 1000.0 / 48000.0)
        session.finish()

        # Start Opus publication on a background thread (blocked inside gated_build).
        pub_result = []
        pub_thread = threading.Thread(target=lambda: pub_result.append(publish_stopped(session)))
        pub_thread.start()
        durable(opus_entered, "publish_stopped did not enter Opus encoding")

        # While Opus encoding is STILL paused, the read-only follower finishes ALL tail ASR!
        assert queue._follow_drained.wait(timeout=15.0), "Tail planning/ASR did not overlap Opus publication"
        assert cached_segment_count(session.directory, "whistle", bundle) == 2
        # Final metadata MUST NOT be committed while Opus publication is still running!
        assert not (session.directory / "transcript.json").exists()
        assert read_metadata(session.directory)["transcript"]["status"] == "none"

        # Now let Opus publication finish, then enqueue finalization.
        opus_release.set()
        pub_thread.join(timeout=30)
        assert pub_result and pub_result[0] is not None
        assert queue.enqueue(session.directory)
        finished.wait(1, bound=30)

        assert read_metadata(session.directory)["transcript"]["status"] == "done"
        assert len(tracker["pids"]) == 1, "Warm worker must be reused after Opus-overlap tail drain"
        assert tracker["completes"][0]["postStopAsrCalls"] == 0, "Zero post-Stop ASR calls left after overlap"
        assert tracker["planned"][0]["prefixesReused"] == 1
    finally:
        opus_release.set()
        unregister_publish_observer(on_publish)
        queue.close()
        queue.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# 10. Preemption: manual enqueue, model change, Trash cancel, and close
# ---------------------------------------------------------------------------


def test_preemption_manual_enqueue_model_change_trash_and_close(tmp_path):
    bundle = FakeBundle.create(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    models = FakeSpeechModels("whistle", tracker)
    finished = Finished()

    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        models=models,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=15,
    )
    live_session = RecordingSession(capture_parent(lib), "Live", source="gnome")
    saved_session = RecordingSession(lib, "Saved", source="gnome")
    try:
        live_session.add_track("microphone", None, "microphone", 48000, 1, filename="track-microphone.wav")
        live_session.append("microphone", make_speech_pcm16(48000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        live_session.checkpoint()

        saved_session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        saved_session.append("microphone", make_speech_pcm16(16000, 1, [(0.9, 520.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        saved_session.finish()

        # 1) Start following live_session -> manual enqueue of saved_session preempts follower!
        assert queue.follow_recording(live_session.directory)
        wait_on_queue(queue, lambda: len(tracker["pids"]) == 1, message="Follower did not start")
        assert queue.enqueue(saved_session.directory)
        finished.wait(1, bound=30)
        assert queue._follow_directory is None
        assert read_metadata(saved_session.directory)["transcript"]["status"] == "done"

        # 2) Re-attach follower, then switch model -> model_changed() preempts follower!
        assert queue.follow_recording(live_session.directory)
        wait_on_queue(queue, lambda: queue._follow_client_model == "whistle", message="Follower did not reattach")
        assert sum(1 for c in tracker["clients"] if c.is_alive()) == 1
        models.select_for_test(PARAKEET_MODEL_ID)
        queue.model_changed()
        wait_on_queue(queue, lambda: queue._follow_directory is None, message="model_changed did not preempt follower")

        # 3) Re-attach follower on Parakeet, then Trash (cancel_meeting) cancels & waits cleanly!
        assert queue.follow_recording(live_session.directory)
        wait_on_queue(
            queue, lambda: queue._follow_client_model == PARAKEET_MODEL_ID, message="Parakeet follower did not start"
        )
        assert sum(1 for c in tracker["clients"] if c.is_alive()) == 1
        assert all(not alive for alive in tracker["concurrent_alive_at_start"])
        with queue.cancel_meeting(live_session.directory):
            assert queue._follow_directory is None
            assert queue._warm_client is None
    finally:
        live_session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)
    # Every spawned subprocess was cleanly reaped (no zombies).
    for client in tracker["clients"]:
        assert client.process is None or client.process.poll() is not None


# ---------------------------------------------------------------------------
# 11. Controlled Linux capture stress check: 2 simultaneous tracks, 0 frame loss
# ---------------------------------------------------------------------------


def test_capture_stress_two_simultaneous_tracks_zero_frame_loss(tmp_path):
    bundle = FakeBundle.create(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=10,
    )
    session = RecordingSession(capture_parent(lib), "Stress 2-track", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 48000, 2, filename="track-microphone.wav")
        session.add_track("system", None, "system", 48000, 2, filename="track-system.wav")
        assert queue.follow_recording(session.directory)

        # Write 8 seconds of 48 kHz stereo PCM in 20 ms packets (400 packets per track = 800 writes)
        # while the real background_worker_priority() child reads both WAVs concurrently.
        # Gate the second half on follower cache progress from the first half, then append distinct audio.
        packet_frames = 960  # 20 ms at 48 kHz
        speech_pkt_a = make_speech_pcm16(48000, 2, [(0.02, 440.0, 0.3)])
        speech_pkt_b = make_speech_pcm16(48000, 2, [(0.02, 620.0, 0.3)])
        silence_pkt = bytes(packet_frames * 4)
        expected_mic = bytearray()
        expected_sys = bytearray()

        def write_batch(start_idx: int, end_idx: int, speech_pkt: bytes) -> None:
            for idx in range(start_idx, end_idx):
                t_ms = idx * 20.0
                mic_on = (idx // 50) % 2 == 0  # alternating 1 s speech / 1 s silence
                sys_on = ((idx + 25) // 50) % 2 == 0
                mp = speech_pkt if mic_on else silence_pkt
                sp = speech_pkt if sys_on else silence_pkt
                session.append("microphone", mp, t_ms)
                session.append("system", sp, t_ms)
                expected_mic.extend(mp)
                expected_sys.extend(sp)
                if idx % 25 == 24:
                    session.checkpoint()
            session.checkpoint()

        write_batch(0, 200, speech_pkt_a)
        wait_on_queue(
            queue,
            lambda: cached_segment_count(session.directory, "whistle", bundle) >= 2,
            message="Stress follower did not cache first-half segments during live capture",
        )
        mid_cached = cached_segment_count(session.directory, "whistle", bundle)

        write_batch(200, 400, speech_pkt_b)
        wait_on_queue(
            queue,
            lambda: cached_segment_count(session.directory, "whistle", bundle) >= mid_cached + 2,
            message="Stress follower did not cache distinct post-gate segments during live capture",
        )
        session.finish()

        # Verify ZERO recorder loss and exact bit-for-bit WAV PCM integrity!
        assert not session._writer_losses
        assert not session._failed_writers
        assert session.manifest["state"] == "completed"
        for track, expected in (
            (session.directory / "track-microphone.wav", expected_mic),
            (session.directory / "track-system.wav", expected_sys),
        ):
            raw = track.read_bytes()[44:]
            assert len(raw) == len(expected)
            assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(expected).hexdigest()
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# 12. BUG-CLASSES #1 & #17 (D88): Linear scaling on 11-hour scale (thread_time)
# ---------------------------------------------------------------------------


def test_long_input_11h_v2_cache_and_prefix_linear_scaling():
    # 11 hours ~ 40,000 segments/words; verify O(N) scaling with time.thread_time() (never wall clock).
    def run_workload(n_segments: int) -> float:
        presence = [[i * 32000, i * 32000 + 24000] for i in range(n_segments)]
        starts = [p[0] for p in presence]
        start = time.thread_time()
        for i in range(n_segments):
            pcm_hash = f"{i:064x}"
            key = recognition_cache.segment_key(pcm_hash, 16000)
            assert key.endswith(":16000")
            segment = dict(startSample=i * 32000, endSample=i * 32000 + 16000, reason="silence")
            raw = dict(
                text="alpha beta",
                language="en",
                words=[dict(start=0.05, end=0.3, text="alpha"), dict(start=0.35, end=0.7, text="beta")],
            )
            _val, words, _lang = pipeline.place_words(raw, segment, 16000, 0)
            kept = pipeline.present_words(words, presence, starts=starts)
            assert len(kept) == 2
        return time.thread_time() - start

    # Warm up bytecode/allocators.
    run_workload(500)
    t_n = run_workload(10_000)
    t_2n = run_workload(20_000)
    # 2N must scale near-linearly (well below quadratic 4x; generous 3.2x margin prevents flake on busy CI).
    assert t_2n <= max(t_n * 3.2, t_n + 0.25), f"Non-linear scaling: t(10k)={t_n:.4f}s, t(20k)={t_2n:.4f}s"

    # Also exercise TrackFollower's O(1) prefix checkpointing + single materialization at export_prefix().
    from cassini_talk.session_follower import TrackFollower

    def run_prefix_checkpoints(n_blocks: int) -> float:
        track = dict(id="t1", kind="microphone", sampleRate=16000, channels=1, offsetMs=0, file="track.wav")
        follower = TrackFollower("t1", 0, track, [], DeterministicDetector())
        speech_block = np.full(pipeline.PREFIX_BLOCK_SAMPLES, 0.25, dtype="<f4")
        silence_block = np.zeros(pipeline.PREFIX_BLOCK_SAMPLES, dtype="<f4")
        start = time.thread_time()
        for idx in range(n_blocks):
            follower._feed_16k_masked(speech_block if idx % 3 == 0 else silence_block, flush=False)
        prefix = follower.export_prefix()
        elapsed = time.thread_time() - start
        assert prefix is not None
        assert prefix.samples == n_blocks * pipeline.PREFIX_BLOCK_SAMPLES
        assert len(prefix.block_hashes) == n_blocks
        assert len(prefix.meter_codes) == n_blocks * (pipeline.PREFIX_BLOCK_SAMPLES // 512)
        return elapsed

    run_prefix_checkpoints(30)
    p_n = run_prefix_checkpoints(300)
    p_2n = run_prefix_checkpoints(600)
    assert p_2n <= max(p_n * 3.2, p_n + 0.25), f"Non-linear checkpointing: t(300)={p_n:.4f}s, t(600)={p_2n:.4f}s"

    # Also exercise incremental minute-anchor clock mapping + bit-for-bit equality with mixdown.Mapping.
    from types import SimpleNamespace

    from cassini_talk.library_v2 import mixdown
    from cassini_talk.session_follower import _IncrementalSpanMapping

    def run_incremental_anchors(n_minutes: int) -> tuple[float, _IncrementalSpanMapping, list[list[int]]]:
        anchors: list[list[int]] = []
        mapping = _IncrementalSpanMapping(0.0, 48000)
        start = time.thread_time()
        for m in range(n_minutes):
            # 1 minute = 2,880,000 samples at 48 kHz with slight +50 ppm drift.
            anchors.append([m * 48000 * 60, int(round(m * 60 * 1_000_050_000))])
            fixed = len(anchors) - 1 if len(anchors) >= 2 else 0
            mapping.sync(anchors, fixed)
            if mapping.knots is not None:
                pos = np.arange(m * 48000 * 60 - 24576, m * 48000 * 60, dtype=np.float64)
                _ = mapping.source(pos)
        return time.thread_time() - start, mapping, anchors

    run_incremental_anchors(20)
    a_n, _, _ = run_incremental_anchors(300)
    a_2n, inc_map, all_anchors = run_incremental_anchors(600)
    assert a_2n <= max(a_n * 3.2, a_n + 0.25), f"Non-linear anchor mapping: t(300)={a_n:.4f}s, t(600)={a_2n:.4f}s"

    # Verify bit-for-bit agreement with mixdown.Mapping on the full anchored history.
    ref_map = mixdown.Mapping(
        SimpleNamespace(
            start_ms=0.0,
            anchors=tuple(tuple(a) for a in all_anchors[:-1]),
            info=SimpleNamespace(sample_rate=48000),
        )
    )
    probe = np.arange(48000 * 120, 48000 * 120 + 24576, dtype=np.float64)
    np.testing.assert_allclose(inc_map.source(probe), ref_map.source(probe), rtol=0, atol=1e-9)


# ---------------------------------------------------------------------------
# 13. Review regressions: idle LibraryRelocator polls & consecutive one-child invariant
# ---------------------------------------------------------------------------


def test_library_relocator_idle_polls_preserve_follower_and_relocation_preempts_when_needed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gi.repository import GLib

    from cassini_talk import library_location as location
    from cassini_talk.library_relocator import LibraryRelocator

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(lib))
    monkeypatch.setattr(location, "_PRIMARY_CACHE", None)

    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=15,
    )
    session = RecordingSession(lib, "Idle Poll Meeting", source="gnome")
    relocator = None
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session.checkpoint()

        assert queue.follow_recording(session.directory)
        wait_on_queue(
            queue,
            lambda: len(tracker["pids"]) == 1 and cached_segment_count(session.directory, "whistle", bundle) >= 1,
            message="Follower did not cache initial segment",
        )
        initial_pid = tracker["pids"][0]
        initial_thread = queue._follow_thread

        recorder = SimpleNamespace(pipeline=object(), session=session)
        app = SimpleNamespace(
            meeting_trash=SimpleNamespace(hidden=set()),
            recorder=recorder,
            transcriber=queue,
            comfort=None,
            settings_dialog=None,
            get_windows=lambda: [],
            hold=lambda: None,
            release=lambda: None,
        )

        # Avoid relying on a running GLib MainLoop or leaving idle callbacks on the shared MainContext.
        monkeypatch.setattr(GLib, "timeout_add_seconds", lambda _secs, _cb: 0)
        monkeypatch.setattr(GLib, "idle_add", lambda *_args: 0)
        monkeypatch.setattr(GLib, "source_remove", lambda _tid: True)

        def drive_poll():
            relocator.poll()
            fut = relocator.future
            assert fut is not None
            fut.result(timeout=15)
            relocator._complete(fut)

        relocator = LibraryRelocator(app)
        # Complete the initial poll triggered by __init__, then run several more idle polls with pending=[].
        init_future = relocator.future
        assert init_future is not None
        init_future.result(timeout=15)
        relocator._complete(init_future)
        for _ in range(3):
            drive_poll()

        assert queue._follow_directory == session.directory.absolute()
        assert queue._follow_thread is initial_thread and initial_thread.is_alive()
        assert tracker["pids"] == [initial_pid]
        assert tracker["clients"][0].is_alive()

        # A failed pending move with retry=False must also leave the follower untouched.
        dest = tmp_path / "moved-lib"
        dest.mkdir()
        state = location.load()
        state["pending"] = [
            dict(source=str(session.directory.resolve()), target=str(dest / session.directory.name), state="failed")
        ]
        location.save(state)
        drive_poll()
        assert queue._follow_directory == session.directory.absolute()
        assert tracker["clients"][0].is_alive()

        # A waiting move for the actively recording session is blocked by recorder_busy and must NOT kill the follower.
        state = location.load()
        state["pending"] = [
            dict(source=str(session.directory.resolve()), target=str(dest / session.directory.name), state="waiting")
        ]
        location.save(state)
        drive_poll()
        assert queue._follow_directory == session.directory.absolute()
        assert tracker["clients"][0].is_alive()
        assert session.directory.exists()

        # Once recording finishes and the meeting is no longer busy, relocation preempts the follower and moves it.
        session.finish()
        recorder.pipeline = None
        recorder.session = None
        drive_poll()
        assert queue._follow_directory is None
        assert not tracker["clients"][0].is_alive()
        assert (dest / session.directory.name).is_dir()
        assert not session.directory.exists()
    finally:
        if relocator is not None:
            relocator.close()
        if session.manifest.get("state") == "recording":
            session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_consecutive_whistle_job_and_follower_preserves_one_child_invariant(tmp_path):
    bundle = FakeBundle.create(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    models = FakeSpeechModels("whistle", tracker)
    finished = Finished()

    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        models=models,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=15,
    )
    sessions = []
    try:
        # Cycle 1: Completed Whistle batch job leaves persistent self.client alive.
        saved_1 = RecordingSession(lib, "Cycle 1 Batch", source="gnome")
        sessions.append(saved_1)
        saved_1.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        saved_1.append("microphone", make_speech_pcm16(16000, 1, [(0.9, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        saved_1.finish()

        assert queue.enqueue(saved_1.directory)
        finished.wait(1, bound=30)
        assert read_metadata(saved_1.directory)["transcript"]["status"] == "done"
        assert len(tracker["pids"]) == 1
        assert queue.client is not None and queue.client.is_alive()
        whistle_pid = tracker["pids"][0]

        # Cycle 2: Starting a follower transfers ownership of the persistent Whistle child (zero second process).
        live_2 = RecordingSession(lib, "Cycle 2 Follow", source="gnome")
        sessions.append(live_2)
        live_2.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        live_2.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 520.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        live_2.checkpoint()

        assert queue.follow_recording(live_2.directory)
        wait_on_queue(
            queue,
            lambda: cached_segment_count(live_2.directory, "whistle", bundle) >= 1,
            message="Cycle 2 follower did not cache segment using transferred Whistle worker",
        )
        assert queue.client is None
        assert queue._follow_client is not None and queue._follow_client.process.pid == whistle_pid
        assert tracker["pids"] == [whistle_pid]
        assert sum(1 for c in tracker["clients"] if c.is_alive()) == 1

        # Stop & finalize Cycle 2 -> same single Whistle PID hands back warm to queue.client.
        live_2.finish()
        assert queue.enqueue(live_2.directory)
        finished.wait(2, bound=30)
        assert read_metadata(live_2.directory)["transcript"]["status"] == "done"
        assert tracker["pids"] == [whistle_pid]
        assert queue.client is not None and queue.client.process.pid == whistle_pid
        assert sum(1 for c in tracker["clients"] if c.is_alive()) == 1

        # Cycle 3: Switch model from Whistle -> Parakeet while idle Whistle self.client is alive,
        # then start a follower: Whistle must be closed & reaped BEFORE Parakeet spawns.
        models.select_for_test(PARAKEET_MODEL_ID)
        live_3 = RecordingSession(lib, "Cycle 3 Parakeet Follow", source="gnome")
        sessions.append(live_3)
        live_3.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        live_3.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 610.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        live_3.checkpoint()

        assert queue.follow_recording(live_3.directory)
        wait_on_queue(
            queue,
            lambda: (
                queue._follow_client_model == PARAKEET_MODEL_ID
                and cached_segment_count(live_3.directory, "parakeet", bundle) >= 1
            ),
            message="Cycle 3 Parakeet follower did not start and cache segment",
        )
        assert len(tracker["pids"]) == 2
        assert not tracker["clients"][0].is_alive(), "Idle Whistle child must be closed and reaped before Parakeet"
        assert tracker["clients"][1].is_alive()
        assert sum(1 for c in tracker["clients"] if c.is_alive()) == 1
        assert all(not alive for alive in tracker["concurrent_alive_at_start"])
    finally:
        for s in sessions:
            if s.manifest.get("state") == "recording":
                s.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)
    for client in tracker["clients"]:
        assert client.process is None or client.process.poll() is not None


# ---------------------------------------------------------------------------
# 14. Review regressions: env mode unification, SpeechModels.select, cache
#     linearity + byte budget, and lock-free Path.resolve in relocator
# ---------------------------------------------------------------------------


def test_env_transcription_mode_gates_all_auto_write_paths_and_falls_back_on_invalid(monkeypatch, caplog):
    from cassini_talk.transcription import auto_write_enabled, resolve_transcription_mode

    class AppStub:
        transcription_mode = "afterStop"
        transcription_enabled = True

    app = AppStub()

    # Env recordOnly must gate BOTH resolve_transcription_mode and auto_write_enabled
    # (which controls native Stop, browser Stop, and close-and-save).
    monkeypatch.setenv("CASSINI_TRANSCRIPTION_MODE", "recordOnly")
    assert resolve_transcription_mode(app) is TranscriptionMode.RECORD_ONLY
    assert not auto_write_enabled(app)

    # Env transcribeWhileRecording enables auto-write and live following.
    monkeypatch.setenv("CASSINI_TRANSCRIPTION_MODE", "transcribeWhileRecording")
    assert resolve_transcription_mode(app) is TranscriptionMode.TRANSCRIBE_WHILE_RECORDING
    assert auto_write_enabled(app)

    # Invalid env value falls back to afterStop with a warning instead of raising.
    caplog.clear()
    monkeypatch.setenv("CASSINI_TRANSCRIPTION_MODE", "invalidModeName")
    with caplog.at_level("WARNING", logger="cassini_talk.transcription"):
        assert resolve_transcription_mode(app) is TranscriptionMode.AFTER_STOP
        assert auto_write_enabled(app)
    assert any("CASSINI_TRANSCRIPTION_MODE" in rec.message for rec in caplog.records)

    # Explicit per-instance override takes precedence over env.
    monkeypatch.setenv("CASSINI_TRANSCRIPTION_MODE", "recordOnly")
    app.transcription_mode = "afterStop"
    assert resolve_transcription_mode(app) is TranscriptionMode.AFTER_STOP
    assert auto_write_enabled(app)


def test_speech_models_select_notifies_changed_and_preempts_live_follower(tmp_path):
    from cassini_talk.speech_models import SpeechModels

    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])

    class StubStore:
        def installed(self, *_args):
            return tmp_path

        def install_path(self, *_args):
            return tmp_path

    changes = []
    selected_box = ["whistle"]
    queue_ref = []

    def on_models_changed():
        changes.append(True)
        if queue_ref:
            queue_ref[0].model_changed()

    speech_models = SpeechModels(
        "whistle",
        lambda v: selected_box.__setitem__(0, v),
        on_models_changed,
        store=StubStore(),
        preference="whistle",
        persist_preference=lambda _v: None,
        defer_inspect=True,
    )
    # Parakeet is already inspected & downloaded and no notice is showing.
    speech_models._inspect_started = True
    speech_models._inspected.set()
    speech_models._state["downloaded"] = True
    speech_models._state["busy"] = False
    speech_models._state["notice"] = None

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        models=speech_models,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    queue_ref.append(queue)
    session = RecordingSession(lib, "Model switch meeting", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session.checkpoint()

        assert queue.follow_recording(session.directory)
        wait_on_queue(queue, lambda: queue._follow_client_model == "whistle", message="Whistle follower did not start")

        # Switching to already-downloaded Parakeet with notice=None MUST fire changed() and preempt follower.
        before = len(changes)
        assert speech_models.select(PARAKEET_MODEL_ID)
        assert len(changes) == before + 1
        wait_on_queue(
            queue, lambda: queue._follow_directory is None, message="SpeechModels.select did not preempt follower"
        )
    finally:
        session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_follower_cache_writes_are_linear_and_respect_byte_budget(tmp_path, monkeypatch):
    from cassini_talk import meeting_store

    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    session = RecordingSession(lib, "Linear cache meeting", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.checkpoint()

    n_segments = 40
    atomic_writes = []
    orig_atomic_json = meeting_store.atomic_json

    def counting_atomic_json(path, value, *, before_commit=None):
        if Path(path).name == RECOGNITION_CACHE_FILE:
            atomic_writes.append(len(json.dumps(value, separators=(",", ":")).encode("utf-8")))
        return orig_atomic_json(path, value, before_commit=before_commit)

    monkeypatch.setattr("cassini_talk.transcription.atomic_json", counting_atomic_json)

    class BurstFollowClient:
        def __init__(self, *_args, **_kw):
            self._alive = True
            self._idx = 0
            self._done = False

        def start(self):
            return self

        def is_alive(self):
            return self._alive

        def start_follow(self, directory, model, *, poll_interval_ms=50):
            self._dir = str(directory)

        def poll_follow_event(self, timeout=0.05):
            if self._idx < n_segments:
                i = self._idx
                self._idx += 1
                key = recognition_cache.segment_key(f"{i:064x}", 16000)
                return dict(
                    type="follow-cache",
                    directory=self._dir,
                    cacheEntry=dict(
                        key=key,
                        text=f"segment-{i} " + ("word " * 8),
                        language="en",
                        words=[dict(start=0.05, end=0.4, text=f"seg{i}")],
                    ),
                )
            if not self._done:
                self._done = True
                return dict(type="follow-idle", directory=self._dir)
            return None

        def stop_follow(self, *, drain=False, timeout=5.0, on_event=None):
            return dict(type="follow-stopped", directory=self._dir, segments=self._idx)

        def cancel(self):
            self._alive = False

        def close(self):
            self._alive = False

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        client_factory=BurstFollowClient,
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )
    try:
        assert queue.follow_recording(session.directory)
        assert queue._follow_drained.wait(timeout=10.0), "Follower did not finish burst"
        # During live recording, ZERO full-file atomic_json rewrites happened (entries were appended to JSONL).
        assert atomic_writes == []
        model_id = recognition_cache_model("whistle", bundle)
        assert len(recognition_cache.read(session.directory / RECOGNITION_CACHE_FILE, model_id)) == n_segments

        # Draining on Stop compacts once into .recognition-cache.json and removes the journal.
        queue.preempt_follower(drain=True, wait=True)
        assert len(atomic_writes) == 1
        assert not (session.directory / recognition_cache.JOURNAL_FILE).exists()
        assert len(recognition_cache.read(session.directory / RECOGNITION_CACHE_FILE, model_id)) == n_segments
    finally:
        session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)

    # Byte-budget enforcement: entries stop before crossing max_bytes so Stop reuses all accepted entries.
    budget_dir = tmp_path / "budget"
    budget_dir.mkdir()
    model_id = recognition_cache_model("whistle", bundle)
    entries: dict[str, dict] = {}
    small_limit = 1024
    budget = recognition_cache.CacheBudget(model_id, entries, max_bytes=small_limit)
    accepted = 0
    for i in range(50):
        key = recognition_cache.segment_key(f"{i:064x}", 16000)
        val = dict(text="hello world " * 4, language="en", words=[dict(start=0.0, end=0.5, text="hello")])
        if budget.try_add(key, val):
            accepted += 1
    assert 0 < accepted < 50
    cache_path = budget_dir / RECOGNITION_CACHE_FILE
    orig_atomic_json(cache_path, recognition_cache.document(model_id, entries))
    assert cache_path.stat().st_size <= small_limit
    assert len(recognition_cache.read(cache_path, model_id)) == accepted


def test_library_relocator_never_calls_path_resolve_under_transcriber_lock(tmp_path, monkeypatch):
    from types import ModuleType, SimpleNamespace

    if "gi.repository" not in sys.modules:
        try:
            import gi.repository  # noqa: F401
        except ModuleNotFoundError:
            gi_mod = ModuleType("gi")
            repo_mod = ModuleType("gi.repository")
            repo_mod.GLib = SimpleNamespace(idle_add=lambda fn, *a: fn(*a), get_language_names=lambda: ["en"])
            gi_mod.repository = repo_mod
            monkeypatch.setitem(sys.modules, "gi", gi_mod)
            monkeypatch.setitem(sys.modules, "gi.repository", repo_mod)

    from cassini_talk.library_relocator import LibraryRelocator

    class TrackingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self.held = False

        def __enter__(self):
            self._lock.acquire()
            self.held = True
            return self

        def __exit__(self, *args):
            self.held = False
            self._lock.release()

    lock = TrackingLock()
    d1 = tmp_path / "m1"
    d2 = tmp_path / "m2"
    d3 = tmp_path / "m3"
    for d in (d1, d2, d3):
        d.mkdir()

    transcriber = SimpleNamespace(
        lock=lock,
        pending=[d1],
        voice_pending=[d2],
        _follow_directory=d3,
        _warm_directory=None,
        preempt_follower=lambda **_kw: None,
    )
    relocator = LibraryRelocator.__new__(LibraryRelocator)
    relocator.app = SimpleNamespace(transcriber=transcriber)
    relocator.cancelled = threading.Event()

    orig_resolve = Path.resolve
    resolve_calls = 0

    def guarded_resolve(self, *args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        assert not lock.held, "Path.resolve() must never be called while transcriber.lock is held"
        return orig_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    busy = relocator._prepare_relocation_busy([], set(), {})
    assert resolve_calls >= 3
    assert {d1.resolve(), d2.resolve(), d3.resolve()}.issubset(busy)


# ---------------------------------------------------------------------------
# 15. Review regressions (round 2): windowless browser follow, failed
#     publication release, cold-start model switch, async warm reap,
#     manifest stat cache, FIFO journal open, and journal-only cleanup
# ---------------------------------------------------------------------------


def _ensure_gi_for_main(monkeypatch):
    from types import ModuleType, SimpleNamespace

    if "gi.repository" in sys.modules:
        return
    try:
        import gi.repository  # noqa: F401
    except ModuleNotFoundError:
        gi_mod = ModuleType("gi")
        gi_mod.require_version = lambda *_a, **_k: None
        repo_mod = ModuleType("gi.repository")
        repo_mod.Adw = SimpleNamespace(Application=object)
        repo_mod.Gio = SimpleNamespace()
        repo_mod.GLib = SimpleNamespace(idle_add=lambda fn, *a: fn(*a), get_language_names=lambda: ["en"])
        repo_mod.Gst = SimpleNamespace()
        repo_mod.Gtk = SimpleNamespace()
        gi_mod.repository = repo_mod
        monkeypatch.setitem(sys.modules, "gi", gi_mod)
        monkeypatch.setitem(sys.modules, "gi.repository", repo_mod)


def test_windowless_browser_recording_starts_live_follower(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    from cassini_talk.main import CassiniApplication

    rec_dir = tmp_path / "browser-meeting"
    rec_dir.mkdir()
    synced = []

    class StubTranscriber:
        def sync_recordings(self, dirs, *, saving=()):
            synced.append(list(dirs))

    transcriber = StubTranscriber()
    app = SimpleNamespace(
        transcription_enabled=True,
        transcription_mode="transcribeWhileRecording",
        _quitting=False,
        _capture_exiting=False,
        native_captures={},
        _last_native=None,
        _browser_snapshot=dict(statuses=[dict(recording=True, path=str(rec_dir))]),
        _permission_sources=set(),
        record_first=None,
        settings_dialog=None,
        comfort=None,
        windowless_background=SimpleNamespace(refresh=lambda: None),
        build_refresh=None,
        get_windows=lambda: [],  # Zero windows open!
        recording_active=lambda: True,
        discard_test_recording=lambda: None,
        _ensure_transcriber=lambda: transcriber,
    )
    app.live_recording_directories = lambda: CassiniApplication.live_recording_directories(app)
    app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
    app._sync_live_follower = lambda **kw: CassiniApplication._sync_live_follower(app, **kw)

    CassiniApplication._native_changed(app)
    assert synced == [[str(rec_dir)]]


def test_failed_publication_releases_tail_follower_and_does_not_preempt_new_recording(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    from cassini_talk import library_publication
    from cassini_talk.library_v2 import finalize as lib_finalize
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    failed_session = RecordingSession(capture_parent(lib), "Failed publish", source="gnome")
    new_session = RecordingSession(capture_parent(lib), "New live meeting", source="gnome")

    def on_start(d):
        queue.notify_session_finished(d)

    def on_fail(d):
        queue.release_failed_publication(d)

    library_publication.register_publish_observer(on_start, failed=on_fail)
    try:
        failed_session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        failed_session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        failed_session.finish()

        # Simulate Opus publication failing -> failure observer releases the tail follower.
        monkeypatch.setattr(lib_finalize, "finalize", lambda *_a, **_k: (_ for _ in ()).throw(OSError("encode failed")))
        with pytest.raises(OSError, match="encode failed"):
            library_publication.publish_stopped(failed_session)
        wait_on_queue(
            queue,
            lambda: queue._follow_directory is None and queue._warm_client is None,
            message="Failed publication did not release tail follower",
        )

        # Now start a new live recording and deliver a library snapshot containing the failed unpublished session.
        new_session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        new_session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 520.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        new_session.checkpoint()
        assert queue.follow_recording(new_session.directory)
        wait_on_queue(
            queue,
            lambda: queue._follow_directory == new_session.directory.resolve(),
            message="New live recording follower did not start",
        )

        failed_meeting = SimpleNamespace(
            key="failed-key",
            directory=failed_session.directory,
            published_file=None,
            lifecycle="saved",
            capture_state="completed",
            transcript="none",
            origin="own",
            metadata_editable=True,
        )
        app = SimpleNamespace(
            transcription_enabled=True,
            transcription_mode="transcribeWhileRecording",
            _quitting=False,
            _capture_exiting=False,
            _last_meetings={},
            _voice_library_keys=frozenset(),
            voice_backfill=None,
            voice_store=None,
            settings_dialog=None,
            get_windows=lambda: [],
            _browser_snapshot=dict(statuses=[dict(recording=True, path=str(new_session.directory))]),
            live_recording_directories=lambda: [str(new_session.directory)],
            _ensure_transcriber=lambda: queue,
            write_down=lambda *_a, **_k: None,
        )
        app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
        app._sync_live_follower = lambda **kw: CassiniApplication._sync_live_follower(app, **kw)

        CassiniApplication.observe_library(app, SimpleNamespace(meetings=[failed_meeting], unavailable=False), {})
        assert queue._follow_directory == new_session.directory.resolve(), (
            "Failed unpublished session must not preempt a new live recording"
        )
    finally:
        library_publication.unregister_publish_observer(on_start, failed=on_fail)
        new_session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_cold_start_model_switch_preempts_before_start_follow(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    models = FakeSpeechModels("whistle", tracker)

    start_entered = threading.Event()
    release_start = threading.Event()
    follow_started = []

    class GatedColdStartClient:
        def __init__(self, *_args, **_kw):
            self._alive = True
            self.cancelled_flag = False
            self.closed_flag = False

        def start(self):
            start_entered.set()
            durable(release_start, "start() was not released")
            return self

        def is_alive(self):
            return self._alive

        def start_follow(self, *_args, **_kw):
            follow_started.append(True)

        def poll_follow_event(self, timeout=0.05):
            return None

        def stop_follow(self, **_kw):
            return dict(type="follow-stopped")

        def cancel(self):
            self.cancelled_flag = True

        def close(self):
            self._alive = False
            self.closed_flag = True

    created = []

    def factory(*args, **kw):
        c = GatedColdStartClient(*args, **kw)
        created.append(c)
        return c

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        models=models,
        client_factory=factory,
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )
    session = RecordingSession(lib, "Cold start race", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.checkpoint()

        assert queue.follow_recording(session.directory)
        durable(start_entered, "Cold-start client did not enter start()")

        # Switch model while start() is still blocked -> model_changed() must see intended model & preempt!
        models.select_for_test(PARAKEET_MODEL_ID)
        queue.model_changed()
        release_start.set()

        wait_on_queue(queue, lambda: queue._follow_directory is None, message="Stale follower did not exit")
        assert follow_started == [], "Stale model must not call start_follow() after model switch during start()"
        assert created and created[0].closed_flag
    finally:
        release_start.set()
        session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)


def test_preempt_and_stop_do_not_block_gtk_on_warm_client_close(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    models = FakeSpeechModels("whistle", tracker)

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        models=models,
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )

    class BlockingWarmClient:
        def __init__(self):
            self.cancel_called = threading.Event()
            self.close_entered = threading.Event()
            self.release_close = threading.Event()
            self.close_done = threading.Event()

        def cancel(self):
            self.cancel_called.set()

        def close(self):
            self.close_entered.set()
            durable(self.release_close, "warm.close() was not released")
            self.close_done.set()

    try:
        # 1) model_changed() -> preempt_follower(wait=False) must return while warm.close() is blocked.
        warm1 = BlockingWarmClient()
        with queue.lock:
            queue._warm_client = warm1
            queue._warm_client_model = "whistle"
            queue._warm_directory = lib / "m1"
        models.select_for_test(PARAKEET_MODEL_ID)
        queue.model_changed()
        assert warm1.cancel_called.is_set()
        durable(warm1.close_entered, "Background reaper did not start closing warm1")
        assert not warm1.close_done.is_set()
        warm1.release_close.set()
        durable(warm1.close_done, "warm1.close() did not finish after release")

        # 2) stop(directory) must also return while warm.close() is blocked.
        warm2 = BlockingWarmClient()
        target = lib / "m2"
        target.mkdir()
        with queue.lock:
            queue._warm_client = warm2
            queue._warm_client_model = PARAKEET_MODEL_ID
            queue._warm_directory = target.absolute()
            queue._warm_aliases = frozenset((target.absolute(),))
        assert queue.stop(target)
        assert warm2.cancel_called.is_set()
        durable(warm2.close_entered, "Background reaper did not start closing warm2")
        assert not warm2.close_done.is_set()
        warm2.release_close.set()
        durable(warm2.close_done, "warm2.close() did not finish after release")
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)


def test_session_follower_skips_reparse_when_manifest_unchanged(tmp_path, monkeypatch):
    from cassini_talk import session_follower as sf

    lib = tmp_path / "lib"
    lib.mkdir()
    session = RecordingSession(lib, "Manifest cache", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.checkpoint()

    reads = []
    orig_read = sf.read_session_readonly

    def counting_read(directory):
        reads.append(directory)
        return orig_read(directory)

    monkeypatch.setattr(sf, "read_session_readonly", counting_read)
    try:
        follower = SessionFollower(
            session.directory,
            DeterministicRecognizer("whistle"),
            "whistle@" + ("a" * 64),
            DeterministicDetector(),
            on_cache_entry=lambda _e: None,
        )
        assert follower.refresh()
        assert len(reads) == 1
        # Repeated refreshes while session.json is unchanged must NOT reparse.
        for _ in range(10):
            assert follower.refresh()
        assert len(reads) == 1
        # Updating session.json invalidates the stat signature and triggers exactly one reparse.
        session.append("microphone", make_speech_pcm16(16000, 1, [(0.5, 440.0, 0.3)]), 0.0)
        session.checkpoint()
        assert follower.refresh()
        assert len(reads) == 2
    finally:
        session.finish()


def test_journal_append_never_blocks_on_fifo_without_reader(tmp_path):
    fifo = tmp_path / recognition_cache.JOURNAL_FILE
    os.mkfifo(fifo)
    key = recognition_cache.segment_key("a" * 64, 16000)
    value = dict(text="hello", language="en", words=[dict(start=0.0, end=0.5, text="hello")])
    assert not recognition_cache.append_journal_entry(tmp_path, "whistle@" + ("a" * 64), key, value)


def test_journal_only_cache_is_cleaned_up_after_successful_transcription(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])

    session = RecordingSession(lib, "Journal cleanup meeting", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
    session.finish()

    # Pre-populate ONLY the append-only journal (simulating crash recovery where all segments hit cache).
    model_id = recognition_cache_model("whistle", bundle)
    direct_entries = []
    direct = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        model_id,
        DeterministicDetector(),
        on_cache_entry=direct_entries.append,
    )
    assert direct.refresh()
    while direct.step():
        pass
    assert len(direct_entries) >= 1
    for entry in direct_entries:
        val = {name: entry[name] for name in ("text", "language", "words")}
        assert recognition_cache.append_journal_entry(session.directory, model_id, entry["key"], val)
    assert (session.directory / recognition_cache.JOURNAL_FILE).is_file()
    assert not (session.directory / RECOGNITION_CACHE_FILE).exists()

    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.AFTER_STOP,
    )
    try:
        assert queue.enqueue(session.directory)
        finished.wait(1, bound=30)
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
        assert not (session.directory / RECOGNITION_CACHE_FILE).exists()
        assert not (session.directory / recognition_cache.JOURNAL_FILE).exists()
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# 16. Review regressions (round 3): empty-live/symlink release, lock-free
#     Popen cancel, hard-linked journal rejection, warm identity mismatch
# ---------------------------------------------------------------------------


def test_empty_live_recordings_and_symlinked_library_release_follower(tmp_path):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    link_lib = tmp_path / "link-lib"
    link_lib.symlink_to(real_lib)

    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=link_lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    session = RecordingSession(link_lib, "Symlinked meeting", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session.checkpoint()

        # Follow via symlinked path; release via resolved path must still match and stop it!
        assert queue.follow_recording(session.directory)
        wait_on_queue(queue, lambda: queue._follow_client_model == "whistle", message="Follower did not start")
        queue.release_failed_publication(session.directory.resolve())
        wait_on_queue(queue, lambda: queue._follow_directory is None, message="Resolved path did not release symlinked")

        # Re-attach: empty live list with active saving preserves follower; empty saving releases it.
        assert queue.follow_recording(session.directory)
        wait_on_queue(queue, lambda: queue._follow_client_model == "whistle", message="Follower did not restart")
        queue.sync_recordings([], saving=[session.directory.resolve()])
        assert queue._follow_directory is not None, "Active saving publication must not be released"
        queue.sync_recordings([], saving=[])
        wait_on_queue(
            queue, lambda: queue._follow_directory is None, message="Empty live + empty saving did not release follower"
        )
    finally:
        session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_preempt_and_stop_never_block_on_popen_in_start(tmp_path, monkeypatch):
    import subprocess

    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)

    popen_entered = threading.Event()
    release_popen = threading.Event()
    killed = threading.Event()
    waited = threading.Event()

    class FakeProc:
        def __init__(self):
            self.pid = 999999
            self.stdin = type("S", (), {"close": lambda self: None})()
            self.stdout = type("S", (), {"close": lambda self: None})()
            self._rc = None

        def poll(self):
            return self._rc

        def kill(self):
            self._rc = -9
            killed.set()

        def wait(self, timeout=None):
            waited.set()
            return self._rc

    def gated_popen(*_args, **_kwargs):
        popen_entered.set()
        durable(release_popen, "Popen gate was not released")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", gated_popen)
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(b, engine="whistle", timeout=timeout),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )
    session = RecordingSession(lib, "Blocked Popen", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.checkpoint()

        assert queue.follow_recording(session.directory)
        durable(popen_entered, "Worker start did not reach Popen")

        # Main-thread preempt_follower(wait=False) MUST return immediately while Popen is blocked!
        queue.preempt_follower(drain=False, wait=False)
        assert not killed.is_set()

        # Releasing Popen must immediately kill/reap the spawned child and retire the follower.
        release_popen.set()
        durable(killed, "Cancelled Popen child was not killed")
        durable(waited, "Cancelled Popen child was not reaped")
        wait_on_queue(queue, lambda: queue._follow_directory is None, message="Follower did not retire after Popen")
    finally:
        release_popen.set()
        session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)


def test_journal_append_rejects_hard_linked_file(tmp_path):
    backup = tmp_path / "backup.jsonl"
    backup.write_text("precious-backup\n")
    journal = tmp_path / recognition_cache.JOURNAL_FILE
    os.link(backup, journal)
    key = recognition_cache.segment_key("b" * 64, 16000)
    value = dict(text="hello", language="en", words=[dict(start=0.0, end=0.5, text="hello")])
    assert not recognition_cache.append_journal_entry(tmp_path, "whistle@" + ("a" * 64), key, value)
    assert backup.read_text() == "precious-backup\n"


def test_warm_worker_restarts_when_bundle_identity_changes_on_disk(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])

    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    session = RecordingSession(lib, "Bundle upgrade meeting", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session.checkpoint()

        assert queue.follow_recording(session.directory)
        wait_on_queue(
            queue,
            lambda: cached_segment_count(session.directory, "whistle", bundle) >= 1,
            message="Follower v1 did not cache segment",
        )
        session.finish()
        # Drain warm v1 worker cleanly.
        queue.preempt_follower(drain=True, wait=True)
        assert queue._warm_client is not None and len(tracker["pids"]) == 1

        # Upgrade bundle pins.json on disk while warm v1 worker is resident!
        (bundle / "pins.json").write_text(json.dumps({"installed": {"whistle.cact": "b" * 64}}))

        # Finalization MUST reject the stale v1 warm worker and spawn a fresh v2 worker!
        assert queue.enqueue(session.directory)
        finished.wait(1, bound=30)
        assert len(tracker["pids"]) == 2, "Stale warm worker was reused despite on-disk bundle identity change"
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 17. Review regressions (round 4): protect queued/active post-Stop handoff
#     from empty sync_recordings, zero Path.resolve() on GTK/lock, and
#     whistle_worker ready.modelIdentity pinned to verified pins.json bytes
# ---------------------------------------------------------------------------


def test_enqueue_plus_empty_sync_recordings_preserves_warm_worker_handoff(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])

    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    session = RecordingSession(lib, "Post-stop warm handoff", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session.checkpoint()

        assert queue.follow_recording(session.directory)
        wait_on_queue(
            queue,
            lambda: cached_segment_count(session.directory, "whistle", bundle) >= 1,
            message="Follower did not cache segment before Stop",
        )
        session.finish()

        # Reproduce _native_finished() -> write_down() -> _native_changed() -> sync_recordings([], saving=[]):
        # the queued post-Stop job MUST protect the follower/warm worker and reuse the same PID + prefix!
        assert queue.enqueue(session.directory)
        queue.sync_recordings([], saving=[])
        finished.wait(1, bound=30)

        assert len(tracker["pids"]) == 1, "Empty sync_recordings must not cancel follower for a queued post-Stop job"
        assert tracker["planned"] and tracker["planned"][-1].get("prefixesReused", 0) >= 1
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_sync_recordings_and_release_never_call_path_resolve_on_gtk(tmp_path, monkeypatch):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    link_lib = tmp_path / "link-lib"
    link_lib.symlink_to(real_lib)

    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=link_lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    session = RecordingSession(link_lib, "No resolve on GTK", source="gnome")
    other_dir = link_lib / "other-meeting"
    other_dir.mkdir()
    resolved_dir = session.directory.resolve()
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session.checkpoint()

        assert queue.follow_recording(session.directory)
        wait_on_queue(queue, lambda: queue._follow_client_model == "whistle", message="Follower did not start")

        def forbid_resolve(self, *args, **kwargs):
            raise AssertionError("Path.resolve() must never run on GTK or under transcriber.lock")

        monkeypatch.setattr(Path, "resolve", forbid_resolve)

        # All of these must succeed with ZERO Path.resolve() calls!
        queue.sync_recordings([session.directory])
        queue.sync_recordings([resolved_dir])
        queue.sync_recordings([], saving=[resolved_dir])
        assert queue._follow_directory is not None
        queue.release_failed_publication(other_dir)
        assert queue._follow_directory is not None
        queue.release_failed_publication(resolved_dir)
        wait_on_queue(queue, lambda: queue._follow_directory is None, message="Symlink alias did not release follower")
    finally:
        monkeypatch.undo()
        session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_whistle_worker_ready_identity_uses_verified_pins_not_reread_after_load(tmp_path, monkeypatch):
    import hashlib
    import io
    import zipfile

    from cassini_talk import whistle_worker

    bundle = tmp_path / "whistle-bundle"
    bundle.mkdir()
    weights = bundle / "whistle.cact"
    weights.write_bytes(b"fake-weights-v1")
    libneedle = bundle / "libneedle3.so"
    libneedle.write_bytes(b"fake-libneedle3")
    libcpu = bundle / "libcassini_whistle_cpu.so"
    libcpu.write_bytes(b"fake-libcpu")
    wheel = bundle / "wrapper.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("needle.py", "class Whistle:\n    def __init__(self, weights):\n        self.weights = weights\n")

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    installed = {
        "whistle.cact": sha(weights),
        "libneedle3.so": sha(libneedle),
        "wrapper.whl": sha(wheel),
    }
    manifest = dict(installed, **{"libcassini_whistle_cpu.so": sha(libcpu)})
    pins_v1 = json.dumps(dict(platform="manylinux2014_x86_64", installed=installed)).encode("utf-8")
    (bundle / "pins.json").write_bytes(pins_v1)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    expected_v1 = "whistle@" + hashlib.sha256(pins_v1).hexdigest()

    for k, v in whistle_worker.PRIVACY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(whistle_worker, "onnxruntime_dir", lambda: None)
    monkeypatch.setattr(sys, "addaudithook", lambda _fn: None)

    emitted = []
    monkeypatch.setattr(whistle_worker, "emit", lambda kind, **kw: emitted.append(dict(type=kind, **kw)))
    monkeypatch.setattr(sys, "stdin", io.BytesIO(b'{"type":"quit"}\n'))

    # Mutate pins.json on disk DURING model load (after verify_bundle, before emit("ready")).
    def loading_with_upgrade(loader):
        model = loader()
        (bundle / "pins.json").write_text(json.dumps(dict(platform="manylinux2014_x86_64", installed={"v": 2})))
        return model

    monkeypatch.setattr(whistle_worker, "guarded_load", loading_with_upgrade)
    sys.modules.pop("needle", None)
    try:
        assert whistle_worker.main(bundle) == 0
    finally:
        sys.modules.pop("needle", None)

    ready_events = [e for e in emitted if e.get("type") == "ready"]
    assert len(ready_events) == 1
    assert ready_events[0]["modelIdentity"] == expected_v1, (
        "ready.modelIdentity must come from the verified pins.json bytes, not a post-load reread"
    )


# ---------------------------------------------------------------------------
# 18. Review regressions (round 5): observe_library enqueue-before-cleanup,
#     symlink opposite-spelling cancel_meeting & enqueue, opt-in schedule_trace
# ---------------------------------------------------------------------------


def test_observe_library_saving_to_saved_enqueues_before_empty_sync_recordings(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(lib))
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])

    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    session = RecordingSession(lib, "Observe transition", source="gnome")
    try:
        session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session.checkpoint()

        assert queue.follow_recording(session.directory)
        wait_on_queue(
            queue,
            lambda: cached_segment_count(session.directory, "whistle", bundle) >= 1,
            message="Follower did not cache segment before saving -> saved transition",
        )
        session.finish()

        saved_meeting = SimpleNamespace(
            key="m-key",
            directory=session.directory,
            published_file=session.directory / "audio.opus",
            lifecycle="saved",
            capture_state="completed",
            transcript="none",
            origin="own",
            metadata_editable=True,
        )
        previous = {"m-key": ("saving", "completed", False)}
        app = SimpleNamespace(
            transcription_enabled=True,
            transcription_mode="transcribeWhileRecording",
            _quitting=False,
            _capture_exiting=False,
            _voice_library_keys=frozenset(),
            voice_backfill=None,
            voice_store=None,
            settings_dialog=None,
            transcriber=queue,
            native_captures={},
            get_windows=lambda: [],
            live_recording_directories=lambda: [],
            _ensure_transcriber=lambda: queue,
            write_down=lambda meeting, resume=False: queue.enqueue(meeting.directory),
        )
        app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
        app._sync_live_follower = lambda **kw: CassiniApplication._sync_live_follower(app, **kw)

        CassiniApplication.observe_library(app, SimpleNamespace(meetings=[saved_meeting], unavailable=False), previous)
        finished.wait(1, bound=30)

        assert len(tracker["pids"]) == 1, "observe_library must enqueue before empty sync_recordings cleanup"
        assert tracker["planned"] and tracker["planned"][-1].get("prefixesReused", 0) >= 1
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_symlink_library_cancel_meeting_and_enqueue_match_opposite_spelling(tmp_path):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    link_lib = tmp_path / "link-lib"
    link_lib.symlink_to(real_lib)

    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=link_lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    session_trash = RecordingSession(link_lib, "Trash via canonical", source="gnome")
    session_handoff = RecordingSession(link_lib, "Enqueue via canonical", source="gnome")
    try:
        # 1) Trash (cancel_meeting) using canonical path while follower uses symlink path.
        session_trash.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session_trash.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session_trash.checkpoint()

        assert queue.follow_recording(session_trash.directory)
        wait_on_queue(queue, lambda: queue._follow_client_model == "whistle", message="Follower did not start")
        with queue.cancel_meeting(session_trash.directory.resolve()):
            assert queue._follow_directory is None, "cancel_meeting via canonical path must retire symlink follower"
            assert queue._warm_client is None
        session_trash.finish()

        # 2) Enqueue using canonical path while follower uses symlink path -> warm handoff preserved!
        tracker["pids"].clear()
        tracker["planned"].clear()
        session_handoff.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session_handoff.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 520.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session_handoff.checkpoint()

        assert queue.follow_recording(session_handoff.directory)
        wait_on_queue(
            queue,
            lambda: cached_segment_count(session_handoff.directory, "whistle", bundle) >= 1,
            message="Follower did not cache segment",
        )
        session_handoff.finish()
        assert queue.enqueue(session_handoff.directory.resolve())
        finished.wait(1, bound=30)

        assert len(tracker["pids"]) == 1, "enqueue via canonical path must drain and reuse symlink follower"
        assert tracker["planned"] and tracker["planned"][-1].get("prefixesReused", 0) >= 1
        assert read_metadata(session_handoff.directory)["transcript"]["status"] == "done"
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 19. Review regressions (round 6): stable single follower per refresh,
#     reverse-direction canonical->symlink cancel/enqueue, lazy library_root
# ---------------------------------------------------------------------------


def test_observe_library_saving_plus_live_keeps_stable_single_follower(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(lib))
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    saving_session = RecordingSession(capture_parent(lib), "Saving meeting A", source="gnome")
    live_session = RecordingSession(capture_parent(lib), "Live meeting B", source="gnome")
    try:
        saving_session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        saving_session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        saving_session.finish()

        live_session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        live_session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 520.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        live_session.checkpoint()

        saving_meeting = SimpleNamespace(
            key="a-key",
            directory=saving_session.directory,
            published_file=None,
            lifecycle="saving",
            capture_state="completed",
            transcript="none",
            origin="own",
            metadata_editable=True,
        )
        app = SimpleNamespace(
            transcription_enabled=True,
            transcription_mode="transcribeWhileRecording",
            _quitting=False,
            _capture_exiting=False,
            _voice_library_keys=frozenset(),
            voice_backfill=None,
            voice_store=None,
            settings_dialog=None,
            transcriber=queue,
            native_captures={
                "native-a": SimpleNamespace(pipeline=object(), stopping=True, _finishing=True, session=saving_session)
            },
            _browser_snapshot=dict(statuses=[dict(recording=True, stopping=False, path=str(live_session.directory))]),
            get_windows=lambda: [],
            _ensure_transcriber=lambda: queue,
            write_down=lambda *_a, **_k: None,
        )
        app.follower_capture_directories = lambda: CassiniApplication.follower_capture_directories(app)
        app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
        app._sync_live_follower = lambda **kw: CassiniApplication._sync_live_follower(app, **kw)

        snapshot = SimpleNamespace(meetings=[saving_meeting], unavailable=False)
        CassiniApplication.observe_library(app, snapshot, {})
        wait_on_queue(
            queue,
            lambda: queue._follow_client_model == "whistle" and bool(tracker["pids"]),
            message="Live follower did not start",
        )
        initial_pid = tracker["pids"][0]
        initial_thread = queue._follow_thread

        # Repeating identical snapshots (A saving + B live) and firing _on_publish_started(A)
        # must NEVER restart or churn B's live follower.
        for _ in range(5):
            CassiniApplication.observe_library(app, snapshot, {})
        CassiniApplication._on_publish_started(app, saving_session.directory)
        assert tracker["pids"] == [initial_pid]
        assert queue._follow_thread is initial_thread
        assert queue._follow_directory == live_session.directory.absolute()
    finally:
        live_session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_symlink_library_canonical_follow_matches_symlink_cancel_and_enqueue(tmp_path):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    link_lib = tmp_path / "link-lib"
    link_lib.symlink_to(real_lib)

    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=real_lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
        follow_poll_interval_ms=20,
    )
    session_trash = RecordingSession(real_lib, "Canonical follow, symlink trash", source="gnome")
    session_handoff = RecordingSession(real_lib, "Canonical follow, symlink enqueue", source="gnome")
    try:
        # 1) Follower starts on canonical path; Trash calls cancel_meeting via symlink path.
        session_trash.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session_trash.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session_trash.checkpoint()

        assert queue.follow_recording(session_trash.directory)
        wait_on_queue(queue, lambda: queue._follow_client_model == "whistle", message="Follower did not start")
        symlink_trash_dir = link_lib / session_trash.directory.name
        with queue.cancel_meeting(symlink_trash_dir):
            assert queue._follow_directory is None, "cancel_meeting via symlink path must retire canonical follower"
            assert queue._warm_client is None
        session_trash.finish()

        # 2) Follower starts on canonical path; enqueue is called via symlink path -> warm handoff preserved!
        tracker["pids"].clear()
        tracker["planned"].clear()
        session_handoff.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
        session_handoff.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 520.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
        session_handoff.checkpoint()

        assert queue.follow_recording(session_handoff.directory)
        wait_on_queue(
            queue,
            lambda: cached_segment_count(session_handoff.directory, "whistle", bundle) >= 1,
            message="Follower did not cache segment",
        )
        session_handoff.finish()
        symlink_handoff_dir = link_lib / session_handoff.directory.name
        assert queue.enqueue(symlink_handoff_dir)
        # Calling empty sync_recordings([]) immediately while symlink_handoff_dir is in self.pending
        # MUST NOT cancel the canonical follower before the worker resolves the symlink!
        queue.sync_recordings([])
        finished.wait(1, bound=30)

        assert len(tracker["pids"]) == 1, "enqueue via symlink path must drain and reuse canonical follower"
        assert tracker["planned"] and tracker["planned"][-1].get("prefixesReused", 0) >= 1
        assert read_metadata(session_handoff.directory)["transcript"]["status"] == "done"
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_transcription_queue_init_does_not_call_library_root_on_gtk(tmp_path, monkeypatch):
    bundle = FakeBundle.create(tmp_path)

    def forbid_library_root(*_args, **_kwargs):
        raise AssertionError("TranscriptionQueue.__init__ must not call library_root(None) synchronously on GTK")

    monkeypatch.setattr("cassini_talk.transcription.library_root", forbid_library_root)
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=None,
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )
    try:
        assert queue._root is None
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)


def test_follow_stop_drain_true_emits_ready_segment_before_acknowledging(tmp_path):
    from cassini_talk.session_follower import run_follow_loop

    lib = tmp_path / "lib"
    lib.mkdir()
    session = RecordingSession(lib, "Drain stop test", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
    session.finish()

    # Deliver follow-stop(drain=True) right after advancing enough quanta to queue a ready segment
    # (before the loop's next recognize step runs!), proving drain=True finishes ready recognition.
    class StopWhenReadyReader:
        def __init__(self):
            self.calls = 0
            self.sent = False

        def try_readline(self, timeout=0.0):
            self.calls += 1
            # 1.8 s of 16 kHz audio = 4 quanta of 512 ms to close the segment into ready_segments;
            # inject follow-stop(drain=True) on the very next poll before recognize runs.
            if not self.sent and self.calls >= 5:
                self.sent = True
                return b'{"type":"follow-stop","drain":true}\n'
            return None

    events = []
    prefix_store = {}
    action, _det = run_follow_loop(
        DeterministicRecognizer("whistle"),
        tmp_path,
        dict(directory=str(session.directory), model="whistle@" + ("a" * 64), pollIntervalMs=10),
        lambda kind, **kw: events.append(dict(type=kind, **kw)),
        reader=StopWhenReadyReader(),
        detector=DeterministicDetector(),
        prefix_store=prefix_store,
    )
    assert action == "continue"
    types = [e["type"] for e in events]
    assert "follow-cache" in types and "follow-stopped" in types
    assert types.index("follow-cache") < types.index("follow-stopped")
    stopped = next(e for e in events if e["type"] == "follow-stopped")
    assert stopped["segments"] >= 1


# ---------------------------------------------------------------------------
# 20. Review regressions (round 8): canonical cancel_meeting vs symlink job
#     (including pre-commit check & zero Path.resolve under lock) and gated
#     _owned() startup window
# ---------------------------------------------------------------------------


def test_cancel_meeting_via_canonical_waits_and_cancels_symlink_enqueued_job_without_resolve_under_lock(
    tmp_path, monkeypatch
):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    link_lib = tmp_path / "link-lib"
    link_lib.symlink_to(real_lib)
    bundle = FakeBundle.create(tmp_path)

    job_entered = threading.Event()
    release_job = threading.Event()
    client_cancelled = threading.Event()
    trash_entered = threading.Event()
    trash_done = threading.Event()

    class GatedJobClient:
        def __init__(self, *_args, **_kw):
            self.recognition_cache = None
            self.diarization = None
            self.keep_for_voices = False

        def start(self):
            return self

        def close(self):
            pass

        def cancel(self):
            client_cancelled.set()

        def process_tracks(self, tracks, progress):
            progress(dict(type="planned", etaMinutes=0, segments=1))
            job_entered.set()
            durable(release_job, "Gated job was not released")
            # Return normally so TranscriptionQueue hits its pre-commit _check_not_trashed()!
            progress(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=300, text="ShouldNotCommit")],
                )
            )
            return dict(diagnostics=[], segments=1)

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=link_lib,
        client_factory=GatedJobClient,
        mode=TranscriptionMode.AFTER_STOP,
    )
    session = RecordingSession(link_lib, "Symlink job vs canonical trash", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
    session.finish()

    try:
        assert queue.enqueue(session.directory)
        durable(job_entered, "Symlink-enqueued job did not enter process_tracks")

        # Verify cancel_meeting never calls Path.resolve() while holding queue.lock.
        class TrackingRLock:
            def __init__(self, inner):
                self._inner = inner
                self._local = threading.local()

            def _depth(self):
                return getattr(self._local, "depth", 0)

            def acquire(self, *args, **kwargs):
                ok = self._inner.acquire(*args, **kwargs)
                if ok:
                    self._local.depth = self._depth() + 1
                return ok

            def release(self):
                self._local.depth = max(0, self._depth() - 1)
                return self._inner.release()

            def _release_save(self):
                saved = self._inner._release_save()
                self._local.depth = 0
                return saved

            def _acquire_restore(self, state):
                self._inner._acquire_restore(state)
                self._local.depth = state[0]

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *args):
                self.release()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        tracked_lock = TrackingRLock(queue.lock)
        queue.lock = tracked_lock
        queue._idle = threading.Condition(tracked_lock)

        orig_resolve = Path.resolve
        resolve_under_lock = []

        def checked_resolve(self, *args, **kwargs):
            if tracked_lock._depth() > 0:
                resolve_under_lock.append(str(self))
            return orig_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", checked_resolve)

        canonical_dir = orig_resolve(session.directory)

        def run_trash():
            with queue.cancel_meeting(canonical_dir):
                trash_entered.set()
                assert queue._active_directory is None
                assert not queue.pending
            trash_done.set()

        trash_thread = threading.Thread(target=run_trash, daemon=True)
        trash_thread.start()

        # Trash must signal cancel on the symlink-enqueued client and WAIT until the job exits!
        durable(client_cancelled, "Canonical cancel_meeting did not cancel symlink-enqueued job")
        assert not trash_entered.is_set(), "Trash entered exclusion block while symlink-enqueued job was still active"

        release_job.set()
        durable(trash_entered, "Trash did not enter after cancelled job exited")
        durable(trash_done, "Trash did not complete")
        trash_thread.join(timeout=5.0)

        assert not resolve_under_lock, f"Path.resolve() called while queue.lock was held: {resolve_under_lock}"
        # Pre-commit _check_not_trashed() must have aborted committing the transcript!
        assert read_metadata(session.directory)["transcript"]["status"] != "done"
    finally:
        release_job.set()
        queue.close()
        queue.executor.shutdown(wait=True)


def test_cancel_meeting_waits_for_follower_gated_before_owned_returns(tmp_path, monkeypatch):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    link_lib = tmp_path / "link-lib"
    link_lib.symlink_to(real_lib)
    bundle = FakeBundle.create(tmp_path)

    owned_entered = threading.Event()
    release_owned = threading.Event()
    trash_entered = threading.Event()
    trash_done = threading.Event()

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=link_lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(b, engine="whistle", timeout=timeout),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )
    orig_owned = queue._owned

    def gated_owned(directory):
        owned_entered.set()
        durable(release_owned, "_owned() gate was not released")
        return orig_owned(directory)

    monkeypatch.setattr(queue, "_owned", gated_owned)
    session = RecordingSession(link_lib, "Gated _owned startup", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.checkpoint()

    try:
        assert queue.follow_recording(session.directory)
        durable(owned_entered, "Follower did not reach _owned()")

        canonical_dir = session.directory.resolve()
        cancelled_added = threading.Event()

        class TrackingSet(set):
            def add(self, item):
                super().add(item)
                if item == canonical_dir:
                    cancelled_added.set()

        queue._cancelled_meetings = TrackingSet()

        def run_trash():
            with queue.cancel_meeting(canonical_dir):
                trash_entered.set()
                assert queue._follow_directory is None
            trash_done.set()

        trash_thread = threading.Thread(target=run_trash, daemon=True)
        trash_thread.start()

        durable(cancelled_added, "cancel_meeting did not register canonical_dir in _cancelled_meetings")
        # Ensure Trash cannot enter its exclusion block while follower is still inside _owned().
        assert not trash_entered.is_set()

        release_owned.set()
        durable(trash_entered, "Trash did not enter after gated follower retired")
        durable(trash_done, "Trash did not finish")
        trash_thread.join(timeout=5.0)
    finally:
        release_owned.set()
        session.finish()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 21. Review regressions (round 9): cancel_meeting lock-gap symlink enqueue
#     and canonical stop() vs symlink-enqueued active job
# ---------------------------------------------------------------------------


def test_cancel_meeting_catches_symlink_enqueue_during_off_lock_resolve_gap(tmp_path, monkeypatch):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    link_lib = tmp_path / "link-lib"
    link_lib.symlink_to(real_lib)
    bundle = FakeBundle.create(tmp_path)

    resolve_gap_entered = threading.Event()
    release_resolve_gap = threading.Event()
    trash_entered = threading.Event()
    trash_done = threading.Event()

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=link_lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(b, engine="whistle", timeout=timeout),
        mode=TranscriptionMode.AFTER_STOP,
    )
    session = RecordingSession(link_lib, "Gap enqueue meeting", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
    session.finish()

    other_dir = (link_lib / "other-unrelated").absolute()
    other_dir.mkdir()
    canonical_dir = session.directory.resolve()

    orig_canonical = queue._canonical_follow_dir

    def gated_canonical(path):
        result = orig_canonical(path)
        if Path(path).absolute() == other_dir and not resolve_gap_entered.is_set():
            resolve_gap_entered.set()
            durable(release_resolve_gap, "Resolve gap was not released")
        return result

    monkeypatch.setattr(queue, "_canonical_follow_dir", gated_canonical)
    try:
        # Place an unrelated path in pending so cancel_meeting must execute its off-lock resolve pass.
        with queue.lock:
            queue.pending.add(other_dir)

        def run_trash():
            with queue.cancel_meeting(canonical_dir):
                trash_entered.set()
                assert session.directory.absolute() not in queue.pending
                assert queue._active_directory not in (session.directory.absolute(), canonical_dir)
            trash_done.set()

        trash_thread = threading.Thread(target=run_trash, daemon=True)
        trash_thread.start()

        durable(resolve_gap_entered, "cancel_meeting did not enter off-lock resolve gap")
        # Enqueue via symlink spelling DURING cancel_meeting's off-lock resolve gap!
        queue.enqueue(session.directory)
        release_resolve_gap.set()

        durable(trash_entered, "cancel_meeting did not enter after cancelling gap-enqueued symlink job")
        durable(trash_done, "cancel_meeting did not finish")
        trash_thread.join(timeout=5.0)
        assert read_metadata(session.directory)["transcript"]["status"] != "done"
    finally:
        release_resolve_gap.set()
        with queue.lock:
            queue.pending.discard(other_dir)
        queue.close()
        queue.executor.shutdown(wait=True)


def test_stop_via_canonical_cancels_and_waits_for_symlink_enqueued_job(tmp_path):
    real_lib = tmp_path / "real-lib"
    real_lib.mkdir()
    link_lib = tmp_path / "link-lib"
    link_lib.symlink_to(real_lib)
    bundle = FakeBundle.create(tmp_path)

    job_entered = threading.Event()
    release_job = threading.Event()
    client_cancelled = threading.Event()
    stop_done = threading.Event()

    class GatedStopClient:
        def __init__(self, *_args, **_kw):
            self.recognition_cache = None
            self.diarization = None
            self.keep_for_voices = False

        def start(self):
            return self

        def close(self):
            pass

        def cancel(self):
            client_cancelled.set()

        def process_tracks(self, tracks, progress):
            progress(dict(type="planned", etaMinutes=0, segments=1))
            job_entered.set()
            durable(release_job, "Gated job was not released")
            raise CancelledError()

    from concurrent.futures import CancelledError

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=link_lib,
        client_factory=GatedStopClient,
        mode=TranscriptionMode.AFTER_STOP,
    )
    session = RecordingSession(link_lib, "Symlink enqueue, canonical stop", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
    session.finish()

    try:
        assert queue.enqueue(session.directory)
        durable(job_entered, "Symlink-enqueued job did not enter process_tracks")

        # Stop via canonical path must cancel the running client and NOT fire done while job is active!
        canonical_dir = session.directory.resolve()
        assert queue.stop(canonical_dir, done=stop_done.set)
        durable(client_cancelled, "Canonical stop() did not cancel symlink-enqueued job")
        assert not stop_done.is_set(), "stop() completed before the symlink-enqueued active job retired"

        release_job.set()
        durable(stop_done, "stop() did not complete after active job retired")
        assert read_metadata(session.directory)["transcript"]["status"] == "none"
    finally:
        release_job.set()
        queue.close()
        queue.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# 22. Review regressions (round 10): moved-meeting destination alias across
#     Trash & Stop, and unpaired Unicode surrogate rejection in cache
# ---------------------------------------------------------------------------


def test_moved_meeting_destination_alias_preserved_across_trash_and_stop(tmp_path, monkeypatch):
    from concurrent.futures import CancelledError

    old_lib = tmp_path / "old-lib"
    dest_lib = tmp_path / "dest-lib"
    old_lib.mkdir()
    dest_lib.mkdir()
    bundle = FakeBundle.create(tmp_path)

    # Create the actual meeting in dest_lib, while enqueue() is called with the pre-move path in old_lib.
    session = RecordingSession(dest_lib, "Moved meeting", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
    session.finish()
    dest_dir = session.directory.resolve()
    old_dir = (old_lib / dest_dir.name).absolute()

    state = dict(
        job_entered=threading.Event(),
        release_job=threading.Event(),
        client_cancelled=threading.Event(),
        raise_on_release=False,
    )

    class GatedMovedClient:
        def __init__(self, *_args, **_kw):
            self.recognition_cache = None
            self.diarization = None
            self.keep_for_voices = False

        def start(self):
            return self

        def close(self):
            pass

        def cancel(self):
            state["client_cancelled"].set()

        def process_tracks(self, tracks, progress):
            progress(dict(type="planned", etaMinutes=0, segments=1))
            state["job_entered"].set()
            durable(state["release_job"], "Moved job was not released")
            if state["raise_on_release"]:
                raise CancelledError()
            progress(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=300, text="ShouldNotCommit")],
                )
            )
            return dict(diagnostics=[], segments=1)

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=dest_lib,
        client_factory=GatedMovedClient,
        mode=TranscriptionMode.AFTER_STOP,
    )
    # Simulate Library relocation routing old_dir -> dest_dir via _owned() and transaction().
    monkeypatch.setattr(queue, "_owned", lambda d: dest_dir if Path(d).absolute() in (old_dir, dest_dir) else Path(d))
    monkeypatch.setattr("cassini_talk.library_location.owned_directory", lambda d: dest_dir)
    try:
        # Part A: Trash via destination path must cancel and wait for job enqueued via old_dir.
        assert queue.enqueue(old_dir)
        durable(state["job_entered"], "Moved job did not enter process_tracks (Trash)")
        trash_entered = threading.Event()
        trash_done = threading.Event()

        def run_trash():
            with queue.cancel_meeting(dest_dir):
                trash_entered.set()
                assert queue._active_directory is None
                assert not queue.pending
            trash_done.set()

        t_trash = threading.Thread(target=run_trash, daemon=True)
        t_trash.start()
        durable(state["client_cancelled"], "Trash via dest_dir did not cancel job enqueued via old_dir")
        assert not trash_entered.is_set(), "Trash entered while moved job was still active"
        state["release_job"].set()
        durable(trash_entered, "Trash did not enter after moved job exited")
        durable(trash_done, "Trash did not finish")
        t_trash.join(timeout=5.0)
        assert read_metadata(dest_dir)["transcript"]["status"] != "done"

        # Part B: Stop via destination path must cancel and wait for job enqueued via old_dir.
        state["job_entered"] = threading.Event()
        state["release_job"] = threading.Event()
        state["client_cancelled"] = threading.Event()
        state["raise_on_release"] = True
        stop_done = threading.Event()

        assert queue.enqueue(old_dir, resume=True)
        durable(state["job_entered"], "Moved job did not enter process_tracks (Stop)")
        assert queue.stop(dest_dir, done=stop_done.set)
        durable(state["client_cancelled"], "Stop via dest_dir did not cancel job enqueued via old_dir")
        assert not stop_done.is_set(), "Stop completed while moved job was still active"
        state["release_job"].set()
        durable(stop_done, "Stop did not complete after moved job retired")
        assert read_metadata(dest_dir)["transcript"]["status"] == "none"
    finally:
        state["release_job"].set()
        queue.close()
        queue.executor.shutdown(wait=True)


def test_recognition_cache_rejects_unpaired_surrogates_and_keeps_valid_entries(tmp_path):
    model_id = "whistle@" + ("a" * 64)
    good_key = recognition_cache.segment_key("1" * 64, 16000)
    bad_key = recognition_cache.segment_key("2" * 64, 16000)
    good_entry = dict(text="valid words", language="en", words=[dict(start=0.0, end=0.5, text="valid")])
    # Raw JSON with an escaped unpaired UTF-16 surrogate (\ud800) that json.loads decodes into a str.
    raw = (
        '{"format":"cassini.recognition-cache.v2","model":"'
        + model_id
        + '","segments":{"'
        + good_key
        + '":'
        + json.dumps(good_entry)
        + ',"'
        + bad_key
        + '":{"text":"bad\\ud800text","language":"en","words":[{"start":0.0,"end":0.4,"text":"bad\\ud800"}]}}}'
    ).encode("utf-8")
    cache_path = tmp_path / RECOGNITION_CACHE_FILE
    cache_path.write_bytes(raw)

    loaded = recognition_cache.read(cache_path, model_id)
    assert list(loaded) == [good_key]

    # CacheBudget must initialize and accept additional valid entries without UnicodeEncodeError.
    budget = recognition_cache.CacheBudget(model_id, loaded)
    extra_key = recognition_cache.segment_key("3" * 64, 16000)
    assert budget.try_add(extra_key, good_entry)
    assert not budget.try_add(
        bad_key, dict(text="bad\ud800text", language="en", words=[dict(start=0.0, end=0.4, text="ok")])
    )


# ---------------------------------------------------------------------------
# 23. Review regressions (round 11): pre-preparer moved-meeting routing in
#     Trash/Stop, follower restart cache reuse, and huge-int overflow guard
# ---------------------------------------------------------------------------


def test_moved_meeting_pre_preparer_trash_and_stop_match_via_owned(tmp_path, monkeypatch):
    old_lib = tmp_path / "old-lib"
    dest_lib = tmp_path / "dest-lib"
    old_lib.mkdir()
    dest_lib.mkdir()
    bundle = FakeBundle.create(tmp_path)

    session = RecordingSession(dest_lib, "Pre-preparer moved meeting", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)]), 0.0)
    session.finish()
    dest_dir = session.directory.resolve()
    old_dir = (old_lib / dest_dir.name).absolute()

    processed = []

    class NeverProcessClient:
        def __init__(self, *_args, **_kw):
            self.recognition_cache = None
            self.diarization = None
            self.keep_for_voices = False

        def start(self):
            return self

        def close(self):
            pass

        def cancel(self):
            pass

        def process_tracks(self, tracks, progress):
            processed.append(tracks)
            return dict(diagnostics=[], segments=0)

    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=dest_lib,
        client_factory=NeverProcessClient,
        mode=TranscriptionMode.AFTER_STOP,
    )
    monkeypatch.setattr(queue, "_owned", lambda d: dest_dir if Path(d).absolute() in (old_dir, dest_dir) else Path(d))
    monkeypatch.setattr("cassini_talk.library_location.owned_directory", lambda d: dest_dir)

    orig_mark_queued = queue._mark_queued
    gate = dict(entered=threading.Event(), release=threading.Event())

    def held_mark_queued(directory, resume):
        gate["entered"].set()
        durable(gate["release"], "Preparer gate was not released")
        return orig_mark_queued(directory, resume)

    monkeypatch.setattr(queue, "_mark_queued", held_mark_queued)

    class WatchSet(set):
        def __init__(self, target, event):
            super().__init__()
            self._target = target
            self._event = event

        def add(self, item):
            super().add(item)
            if item == self._target:
                self._event.set()

    try:
        # Part A: Trash via dest_dir while preparer for old_dir is held BEFORE _job_aliases is recorded.
        old_cancelled = threading.Event()
        queue._cancelled_meetings = WatchSet(old_dir, old_cancelled)
        assert queue.enqueue(old_dir)
        durable(gate["entered"], "Preparer did not enter _mark_queued (Trash)")
        assert old_dir not in queue._job_aliases

        trash_entered = threading.Event()
        trash_done = threading.Event()

        def run_trash():
            with queue.cancel_meeting(dest_dir):
                trash_entered.set()
                assert not queue.pending
                assert queue._active_directory is None
            trash_done.set()

        t_trash = threading.Thread(target=run_trash, daemon=True)
        t_trash.start()
        durable(old_cancelled, "cancel_meeting(dest_dir) did not discover pre-preparer old_dir via _owned()")
        assert not trash_entered.is_set(), "Trash entered while pre-preparer old_dir job was still pending"
        gate["release"].set()
        durable(trash_entered, "Trash did not enter after pre-preparer job retired")
        durable(trash_done, "Trash did not finish")
        t_trash.join(timeout=5.0)
        assert not processed

        # Part B: Stop via dest_dir while preparer for old_dir is held BEFORE _job_aliases is recorded.
        gate["entered"] = threading.Event()
        gate["release"] = threading.Event()
        old_stopping = threading.Event()
        queue._stopping_meetings = WatchSet(old_dir, old_stopping)
        stop_done = threading.Event()

        assert queue.enqueue(old_dir)
        durable(gate["entered"], "Preparer did not enter _mark_queued (Stop)")
        assert old_dir not in queue._job_aliases

        assert queue.stop(dest_dir, done=stop_done.set)
        durable(old_stopping, "stop(dest_dir) did not discover pre-preparer old_dir via _owned()")
        assert not stop_done.is_set(), "Stop finished while pre-preparer old_dir job was still pending"
        gate["release"].set()
        durable(stop_done, "Stop did not finish after pre-preparer job retired")
        assert not processed
        assert read_metadata(dest_dir)["transcript"]["status"] == "none"
    finally:
        gate["release"].set()
        queue.close()
        queue.executor.shutdown(wait=True)


def test_session_follower_restart_skips_cached_same_model_segments(tmp_path):
    model_id = "whistle@" + ("a" * 64)
    session = RecordingSession(tmp_path, "Follower restart cache reuse", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    seg1 = make_speech_pcm16(16000, 1, [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0)])
    session.append("microphone", seg1, 0.0)
    session.checkpoint()

    class CountingRecognizer(DeterministicRecognizer):
        def __init__(self, engine):
            super().__init__(engine)
            self.calls = 0

        def recognize(self, samples):
            self.calls += 1
            return super().recognize(samples)

    rec1 = CountingRecognizer("whistle")
    emitted1 = []
    f1 = SessionFollower(
        session.directory,
        rec1,
        model_id,
        DeterministicDetector(),
        on_cache_entry=emitted1.append,
    )
    assert f1.refresh()
    while f1.step():
        pass
    assert rec1.calls == 1
    assert len(emitted1) == 1

    # Persist the emitted cache entry as the parent queue does before preemption.
    entries = {e["key"]: {k: e[k] for k in ("text", "language", "words")} for e in emitted1}
    (session.directory / RECOGNITION_CACHE_FILE).write_text(
        json.dumps(recognition_cache.document(model_id, entries)),
        encoding="utf-8",
    )

    # Append a new distinct speech segment, then restart a fresh SessionFollower.
    seg2 = make_speech_pcm16(16000, 1, [(1.0, 660.0, 0.3), (0.8, 0.0, 0.0)])
    session.append("microphone", seg2, 1800.0)
    session.checkpoint()

    rec2 = CountingRecognizer("whistle")
    emitted2 = []
    f2 = SessionFollower(
        session.directory,
        rec2,
        model_id,
        DeterministicDetector(),
        on_cache_entry=emitted2.append,
    )
    assert f2.refresh()
    while f2.step():
        pass
    assert rec2.calls == 1, f"Expected only newly appended segment to run inference, got {rec2.calls} calls"
    assert len(emitted2) == 1
    assert emitted2[0]["key"] != emitted1[0]["key"]


def test_recognition_cache_rejects_huge_int_overflow_in_json_and_journal(tmp_path):
    model_id = "whistle@" + ("b" * 64)
    good_key = recognition_cache.segment_key("1" * 64, 16000)
    bad_key = recognition_cache.segment_key("2" * 64, 16000)
    good_entry = dict(text="ok", language="en", words=[dict(start=0.0, end=0.5, text="ok")])
    huge_int = "1" + ("0" * 309)

    json_dir = tmp_path / "json-case"
    json_dir.mkdir()
    raw_json = (
        '{"format":"cassini.recognition-cache.v2","model":"'
        + model_id
        + '","segments":{"'
        + good_key
        + '":'
        + json.dumps(good_entry)
        + ',"'
        + bad_key
        + '":{"text":"huge","language":"en","words":[{"start":0,"end":'
        + huge_int
        + ',"text":"huge"}]}}}'
    ).encode("utf-8")
    (json_dir / RECOGNITION_CACHE_FILE).write_bytes(raw_json)
    assert list(recognition_cache.read(json_dir / RECOGNITION_CACHE_FILE, model_id)) == [good_key]

    journal_dir = tmp_path / "journal-case"
    journal_dir.mkdir()
    good_line = json.dumps(dict(format=recognition_cache.FORMAT, model=model_id, key=good_key, entry=good_entry))
    bad_line = (
        '{"format":"cassini.recognition-cache.v2","model":"'
        + model_id
        + '","key":"'
        + bad_key
        + '","entry":{"text":"huge","language":"en","words":[{"start":0,"end":'
        + huge_int
        + ',"text":"huge"}]}}'
    )
    (journal_dir / recognition_cache.JOURNAL_FILE).write_bytes((good_line + "\n" + bad_line + "\n").encode("utf-8"))
    assert list(recognition_cache.read(journal_dir / RECOGNITION_CACHE_FILE, model_id)) == [good_key]


# ---------------------------------------------------------------------------
# 24. Review regression (round 12): native changed -> finished signal order
#     preserves warm follower worker across handoff
# ---------------------------------------------------------------------------


def test_native_changed_before_finished_preserves_warm_worker_handoff(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    monkeypatch.setattr("cassini_talk.library_location.owned_directory", lambda d: Path(d).resolve())
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )

    session = RecordingSession(lib, "Native changed before finished", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    pcm = make_speech_pcm16(
        16000,
        1,
        [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0), (1.0, 520.0, 0.3), (0.8, 0.0, 0.0)],
    )
    session.append("microphone", pcm, 0.0)
    session.checkpoint()

    recorder = SimpleNamespace(
        session=session,
        pipeline=object(),
        starting=False,
        stopping=False,
        _finishing=False,
        _stop_requested=False,
        discarded=False,
        _discard=False,
        publication_error=None,
        close=lambda: None,
    )
    app = SimpleNamespace(
        transcription_enabled=True,
        transcription_mode="transcribeWhileRecording",
        _quitting=False,
        _capture_exiting=False,
        transcriber=queue,
        native_captures={"cap-1": recorder},
        _last_native=recorder,
        _browser_snapshot={},
        _permission_sources=set(),
        _pending_recording=None,
        record_first=None,
        settings_dialog=None,
        comfort=None,
        windowless_background=SimpleNamespace(refresh=lambda: None),
        build_refresh=None,
        get_windows=lambda: [],
        recording_active=lambda: bool(recorder.pipeline),
        discard_test_recording=lambda: None,
        _notify_shortcut_saved=lambda _path: None,
        _release_native=lambda: None,
        _ensure_transcriber=lambda: queue,
        write_down=lambda path, resume=False: queue.enqueue(Path(path), resume=resume),
    )
    app.follower_capture_directories = lambda: CassiniApplication.follower_capture_directories(app)
    app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
    app._sync_live_follower = lambda **kw: CassiniApplication._sync_live_follower(app, **kw)
    app._native_changed = lambda *a: CassiniApplication._native_changed(app, *a)

    try:
        # 1. Active native recording starts the live follower and precomputes closed segments.
        CassiniApplication._native_changed(app)
        wait_on_queue(
            queue,
            lambda: len(tracker["pids"]) == 1 and cached_segment_count(session.directory, "whistle", bundle) >= 2,
            message="Live native follower did not precompute segments before Stop",
        )

        # 2. Mirror NativeRecorder._finished(): finalize session, clear busy flags while
        #    recorder is STILL in native_captures, emit `changed`, then emit `finished`.
        session.finish()
        recorder.pipeline = None
        recorder.stopping = recorder._finishing = recorder._stop_requested = False

        assert CassiniApplication.follower_capture_directories(app) == ([], [str(session.directory)])
        CassiniApplication._native_changed(app)
        with queue.lock:
            assert queue._follow_directory is not None or queue._warm_client is not None, (
                "changed signal before finished cancelled the warm follower prematurely"
            )

        CassiniApplication._native_finished(app, recorder, str(session.directory))
        finished.wait(1, bound=30)

        # 3. Exact same worker PID must serve both live follow and final post-Stop transcription.
        assert len(tracker["pids"]) == 1, f"Expected 1 warm worker PID, got {tracker['pids']}"
        assert tracker["completes"][0]["workerPid"] == tracker["pids"][0]
        assert tracker["planned"][0]["prefixesReused"] >= 1

        # Discarded idle recorder in native_captures must not be kept in saving.
        discarded = SimpleNamespace(
            session=session,
            pipeline=None,
            starting=False,
            stopping=False,
            _finishing=False,
            _stop_requested=False,
            discarded=True,
            _discard=True,
            publication_error=None,
        )
        app.native_captures = {"cap-discarded": discarded}
        assert CassiniApplication.follower_capture_directories(app) == ([], [])
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 25. Review regression (round 13): idle browser poll during Library recovery
#     preserves observed `saving` follower worker across handoff
# ---------------------------------------------------------------------------


def test_idle_browser_poll_during_recovery_saving_preserves_warm_worker_handoff(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    monkeypatch.setattr("cassini_talk.library_location.owned_directory", lambda d: Path(d).resolve())
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    sessions_dir = capture_parent(lib)
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=sessions_dir,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )

    session = RecordingSession(sessions_dir, "Recovery saving session", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    pcm = make_speech_pcm16(
        16000,
        1,
        [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0), (1.0, 520.0, 0.3), (0.8, 0.0, 0.0)],
    )
    session.append("microphone", pcm, 0.0)
    session.finish()

    app = SimpleNamespace(
        transcription_enabled=True,
        transcription_mode="transcribeWhileRecording",
        _quitting=False,
        _capture_exiting=False,
        _voice_library_keys=frozenset(),
        voice_backfill=None,
        voice_store=None,
        settings_dialog=None,
        transcriber=queue,
        native_captures={},
        _last_native=None,
        _browser_snapshot={},
        _permission_sources=set(),
        record_first=None,
        comfort=None,
        windowless_background=SimpleNamespace(refresh=lambda: None),
        build_refresh=None,
        get_windows=lambda: [],
        recording_active=lambda: False,
        discard_test_recording=lambda: None,
        _ensure_transcriber=lambda: queue,
        write_down=lambda meeting, resume=False: queue.enqueue(getattr(meeting, "directory", meeting), resume=resume),
    )
    app.follower_capture_directories = lambda: CassiniApplication.follower_capture_directories(app)
    app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
    app._sync_live_follower = lambda **kw: CassiniApplication._sync_live_follower(app, **kw)

    saving_meeting = SimpleNamespace(
        key="m-recovery",
        directory=session.directory,
        published_file=None,
        origin="own",
        metadata_editable=True,
        lifecycle="saving",
        capture_state="completed",
        transcript="none",
        audio_ready=True,
    )
    saved_meeting = SimpleNamespace(
        key="m-recovery",
        directory=session.directory,
        published_file=lib / "recovery.ogg",
        origin="own",
        metadata_editable=True,
        lifecycle="saved",
        capture_state="completed",
        transcript="none",
        audio_ready=True,
    )

    try:
        # 1. observe_library sees the recovery session in `saving` and starts the follower.
        CassiniApplication.observe_library(app, SimpleNamespace(meetings=[saving_meeting], unavailable=False), {})
        assert app._observed_saving_directories == (str(session.directory),)
        wait_on_queue(
            queue,
            lambda: len(tracker["pids"]) == 1 and cached_segment_count(session.directory, "whistle", bundle) >= 2,
            message="Recovery saving follower did not precompute segments",
        )

        # 2. Idle browser poll / _native_changed() calls _sync_live_follower() with saving=():
        #    it must NOT cancel the active recovery follower.
        CassiniApplication._native_changed(app)
        CassiniApplication._sync_live_follower(app)
        with queue.lock:
            assert queue._follow_directory is not None or queue._warm_client is not None, (
                "Idle browser poll cancelled the recovery saving follower prematurely"
            )

        # 3. Publication completes (`saving` -> `saved`): observe_library enqueues and hands off warm worker.
        monkeypatch.setattr("cassini_talk.library_publication.root_for", lambda _d: None)
        previous = {"m-recovery": ("saving", "none", False)}
        CassiniApplication.observe_library(app, SimpleNamespace(meetings=[saved_meeting], unavailable=False), previous)
        assert app._observed_saving_directories == ()
        finished.wait(1, bound=30)

        assert len(tracker["pids"]) == 1, f"Expected 1 warm worker PID, got {tracker['pids']}"
        assert tracker["completes"][0]["workerPid"] == tracker["pids"][0]
        assert tracker["planned"][0]["prefixesReused"] >= 1

        # 4. _on_publish_failed drops the failed directory from _observed_saving_directories on GTK.
        app._observed_saving_directories = (str(session.directory),)
        CassiniApplication._on_publish_failed(app, session.directory)
        from gi.repository import GLib

        if hasattr(GLib, "MainContext"):
            ctx = GLib.MainContext.default()
            while ctx.iteration(False):
                pass
        assert app._observed_saving_directories == ()
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 26. Review regression (round 14): windowless recovery completion hands off
#     warm worker on success and clears/releases follower on failure
# ---------------------------------------------------------------------------


def test_windowless_recovery_completion_handles_warm_handoff_and_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    monkeypatch.setattr("cassini_talk.library_location.owned_directory", lambda d: Path(d).resolve())
    from cassini_talk.library_recovery import LibraryRecovery
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    sessions_dir = capture_parent(lib)
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=sessions_dir,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.TRANSCRIBE_WHILE_RECORDING,
    )

    pcm = make_speech_pcm16(
        16000,
        1,
        [(1.0, 440.0, 0.3), (0.8, 0.0, 0.0), (1.0, 520.0, 0.3), (0.8, 0.0, 0.0)],
    )
    session_ok = RecordingSession(sessions_dir, "Windowless recovery success", source="gnome")
    session_ok.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session_ok.append("microphone", pcm, 0.0)
    session_ok.finish()

    session_fail = RecordingSession(sessions_dir, "Windowless recovery failure", source="gnome")
    session_fail.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session_fail.append("microphone", pcm, 0.0)
    session_fail.finish()

    transcription_changed_calls = []
    app = SimpleNamespace(
        transcription_enabled=True,
        transcription_mode="transcribeWhileRecording",
        _quitting=False,
        _capture_exiting=False,
        _voice_library_keys=frozenset(),
        voice_backfill=None,
        voice_store=None,
        settings_dialog=None,
        transcriber=queue,
        native_captures={},
        _last_native=None,
        _browser_snapshot={},
        _permission_sources=set(),
        record_first=None,
        comfort=None,
        windowless_background=SimpleNamespace(refresh=lambda: None),
        build_refresh=None,
        get_windows=lambda: [],  # Last Library window already closed!
        recording_active=lambda: False,
        discard_test_recording=lambda: None,
        _ensure_transcriber=lambda: queue,
        _transcription_changed=lambda: transcription_changed_calls.append(True) or False,
        write_down=lambda meeting, resume=False: queue.enqueue(getattr(meeting, "directory", meeting), resume=resume),
    )
    app.follower_capture_directories = lambda: CassiniApplication.follower_capture_directories(app)
    app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
    app._sync_live_follower = lambda **kw: CassiniApplication._sync_live_follower(app, **kw)
    app._on_recovery_result = lambda directory, error, **kw: CassiniApplication._on_recovery_result(
        app, directory, error, **kw
    )

    recovery_stub = SimpleNamespace(app=app, cancelled=threading.Event())

    try:
        # Part A: Recovery succeeds after the last Library window closes.
        saving_ok = SimpleNamespace(
            key="m-rec-ok",
            directory=session_ok.directory,
            published_file=None,
            origin="own",
            metadata_editable=True,
            lifecycle="saving",
            capture_state="completed",
            transcript="none",
            audio_ready=True,
        )
        CassiniApplication.observe_library(app, SimpleNamespace(meetings=[saving_ok], unavailable=False), {})
        assert app._observed_saving_directories == (str(session_ok.directory),)
        wait_on_queue(
            queue,
            lambda: len(tracker["pids"]) == 1 and cached_segment_count(session_ok.directory, "whistle", bundle) >= 2,
            message="Follower did not precompute recovery session segments",
        )

        # Idle browser poll while windowless recovery is still in flight preserves follower.
        CassiniApplication._sync_live_follower(app)
        with queue.lock:
            assert queue._follow_directory is not None or queue._warm_client is not None

        # Recovery finishes on GTK without any open window/monitor to fire observe_library().
        monkeypatch.setattr("cassini_talk.library_publication.root_for", lambda _d: None)
        LibraryRecovery._result_finished(recovery_stub, session_ok.directory, None)
        assert app._observed_saving_directories == ()
        finished.wait(1, bound=30)

        assert len(tracker["pids"]) == 1, f"Expected 1 warm worker PID, got {tracker['pids']}"
        assert tracker["completes"][0]["workerPid"] == tracker["pids"][0]
        assert tracker["planned"][0]["prefixesReused"] >= 1

        # Part B: Recovery fails after the last Library window closes -> clears state & releases follower.
        saving_fail = SimpleNamespace(
            key="m-rec-fail",
            directory=session_fail.directory,
            published_file=None,
            origin="own",
            metadata_editable=True,
            lifecycle="saving",
            capture_state="completed",
            transcript="none",
            audio_ready=True,
        )
        CassiniApplication.observe_library(app, SimpleNamespace(meetings=[saving_fail], unavailable=False), {})
        assert app._observed_saving_directories == (str(session_fail.directory),)
        wait_on_queue(
            queue,
            lambda: queue._follow_directory == session_fail.directory.resolve(),
            message="Follower did not start for failing recovery session",
        )

        LibraryRecovery._result_finished(recovery_stub, session_fail.directory, OSError("encode failed"))
        assert app._observed_saving_directories == ()
        wait_on_queue(
            queue,
            lambda: queue._follow_directory is None and queue._warm_client is None,
            message="Failed windowless recovery did not release follower",
        )

        # Subsequent idle browser poll must NOT restart the failed recovery follower.
        CassiniApplication._sync_live_follower(app)
        with queue.lock:
            assert queue._follow_directory is None
            assert queue._warm_client is None
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 27. Review regressions (round 16): journaled-checkpoint windowless recovery,
#     recordOnly explicit-intent resume on recovery, and completed-manifest drain
# ---------------------------------------------------------------------------


def _stage_journaled_publish(lib: Path, session_dir: Path, title: str = "Journaled recovery"):
    from cassini_talk.library_v2 import journal

    layout = journal.Layout(lib)
    layout.ensure()
    origin, sid = journal.identity_of(session_dir)
    op = "11111111-2222-3333-4444-555555555555"
    stage_rel = f"{journal.META}/staging/{op}.opus.stage"
    (lib / stage_rel).write_bytes(b"OggS-staged-opus-payload")
    entry = journal.new_entry(
        "publish",
        origin=origin,
        session_id=sid,
        op=op,
        phase="staged",
        session=f"{journal.META}/sessions/{origin}-{sid}",
        stage=stage_rel,
        baseRevision=0,
        newRevision=1,
        fileRevision=None,
        meetingUpdate=dict(published=True),
        name=dict(prefix="2026-11-19--10.00-", title=title, untitled="Untitled"),
        oldPath=None,
        newPath=None,
    )
    journal.write(layout, entry)
    return entry


def test_journaled_checkpoint_recovery_delivers_result_without_open_window(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    from gi.repository import GLib

    from cassini_talk.library_recovery import LibraryRecovery
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    sessions_dir = capture_parent(lib)
    session = RecordingSession(sessions_dir, "Journaled crash recovery", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(0.6, 440.0, 0.3), (0.4, 0.0, 0.0)]), 0.0)
    session.finish()
    _stage_journaled_publish(lib, session.directory, title="Journaled crash recovery")

    enqueued = []
    synced = []
    app = SimpleNamespace(
        transcription_enabled=True,
        transcription_mode="transcribeWhileRecording",
        _quitting=False,
        _capture_exiting=False,
        _observed_saving_directories=(str(session.directory),),
        transcriber=None,
        native_captures={},
        _browser_snapshot={},
        get_windows=lambda: [],
        _transcription_changed=lambda: False,
        _sync_live_follower=lambda **kw: synced.append(kw),
        write_down=lambda directory, resume=False: enqueued.append((Path(directory).resolve(), resume)),
    )
    app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
    app._on_recovery_result = lambda directory, error, *, transcript_status=None: (
        CassiniApplication._on_recovery_result(app, directory, error, transcript_status=transcript_status)
    )

    recovery = SimpleNamespace(
        app=app,
        cancelled=threading.Event(),
        lock=threading.Lock(),
        _activity={},
        failures={},
        attempted=set(),
        started_revision=0,
        _requested={},
        _retry_paths=lambda _roots: None,
        _phase=lambda _path, _saving: None,
    )
    recovery._result_finished = lambda d, e, s=None: LibraryRecovery._result_finished(recovery, d, e, s)
    monkeypatch.setattr("cassini_talk.library_location.roots", lambda: [lib])

    LibraryRecovery._sweep(recovery)
    if hasattr(GLib, "MainContext"):
        ctx = GLib.MainContext.default()
        while ctx.iteration(False):
            pass

    assert list(lib.glob("*.opus")), "Journal replay did not publish the staged Opus file"
    assert read_metadata(session.directory)["published"] is True
    assert app._observed_saving_directories == ()
    assert enqueued == [(session.directory.resolve(), True)]
    assert synced, "_sync_live_follower was not called after journaled recovery completed"


def test_windowless_recovery_record_only_resumes_explicit_intent_only(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    monkeypatch.setattr("cassini_talk.library_location.owned_directory", lambda d: Path(d).resolve())
    from gi.repository import GLib

    from cassini_talk.library_recovery import LibraryRecovery
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    sessions_dir = capture_parent(lib)

    session_queued = RecordingSession(sessions_dir, "RecordOnly queued", source="gnome")
    session_queued.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session_queued.append("microphone", make_speech_pcm16(16000, 1, [(0.5, 440.0, 0.3)]), 0.0)
    session_queued.finish()
    from cassini_talk.meeting_store import update_metadata

    update_metadata(
        session_queued.directory,
        transcript=dict(read_metadata(session_queued.directory)["transcript"], status="queued"),
    )
    _stage_journaled_publish(lib, session_queued.directory, title="RecordOnly queued")

    enqueued = []
    app = SimpleNamespace(
        transcription_enabled=True,
        transcription_mode="recordOnly",
        _quitting=False,
        _capture_exiting=False,
        _observed_saving_directories=(str(session_queued.directory),),
        transcriber=None,
        native_captures={},
        _browser_snapshot={},
        get_windows=lambda: [],
        _transcription_changed=lambda: False,
        _sync_live_follower=lambda **_kw: None,
        write_down=lambda directory, resume=False: enqueued.append((Path(directory).resolve(), resume)),
    )
    app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
    app._on_recovery_result = lambda directory, error, *, transcript_status=None: (
        CassiniApplication._on_recovery_result(app, directory, error, transcript_status=transcript_status)
    )

    recovery = SimpleNamespace(
        app=app,
        cancelled=threading.Event(),
        lock=threading.Lock(),
        _activity={},
        failures={},
        attempted=set(),
        started_revision=0,
        _requested={},
        _retry_paths=lambda _roots: None,
        _phase=lambda _path, _saving: None,
    )
    recovery._result_finished = lambda d, e, s=None: LibraryRecovery._result_finished(recovery, d, e, s)
    monkeypatch.setattr("cassini_talk.library_location.roots", lambda: [lib])

    # Persisted `queued` intent MUST resume even in recordOnly mode.
    LibraryRecovery._sweep(recovery)
    if hasattr(GLib, "MainContext"):
        ctx = GLib.MainContext.default()
        while ctx.iteration(False):
            pass
    assert enqueued == [(session_queued.directory.resolve(), True)]
    assert app._observed_saving_directories == ()

    # Persisted `running` intent also resumes; `none` does NOT auto-start in recordOnly mode.
    enqueued.clear()
    CassiniApplication._on_recovery_result(app, session_queued.directory, None, transcript_status="running")
    assert enqueued == [(session_queued.directory.resolve(), True)]

    enqueued.clear()
    CassiniApplication._on_recovery_result(app, session_queued.directory, None, transcript_status="none")
    assert enqueued == []


def test_session_follower_completed_manifest_without_per_track_ended_drains(tmp_path):
    session = RecordingSession(tmp_path, "Completed without track ended", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(0.8, 440.0, 0.3), (0.6, 0.0, 0.0)]), 0.0)
    session.finish()

    # Strip per-track `ended` flags from session.json (matching `simple_session` fixtures).
    manifest_path = session.directory / "session.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "completed"
    for track in manifest["tracks"]:
        track.pop("ended", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    emitted = []
    follower = SessionFollower(
        session.directory,
        DeterministicRecognizer("whistle"),
        "whistle@" + ("a" * 64),
        DeterministicDetector(),
        on_cache_entry=emitted.append,
    )
    assert follower.refresh()
    assert follower.session_ended is True
    while follower.step():
        pass
    assert follower.all_drained() is True
    assert len(emitted) == 1


# ---------------------------------------------------------------------------
# 28. Review regressions (round 17): Trash maintenance journal replay before
#     LibraryRecovery, and zero Path.resolve() on GTK in enqueue/_set_progress
# ---------------------------------------------------------------------------


def test_trash_maintenance_before_recovery_resumes_queued_transcript_once_without_window(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    monkeypatch.setattr("cassini_talk.library_location.owned_directory", lambda d: Path(d).resolve())
    from gi.repository import GLib

    from cassini_talk.library_recovery import LibraryRecovery
    from cassini_talk.main import CassiniApplication
    from cassini_talk.meeting_store import update_metadata
    from cassini_talk.meeting_trash import MeetingTrash

    lib = tmp_path / "lib"
    lib.mkdir()
    sessions_dir = capture_parent(lib)
    session = RecordingSession(sessions_dir, "Trash replay before recovery", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(0.5, 440.0, 0.3)]), 0.0)
    session.finish()
    update_metadata(
        session.directory,
        transcript=dict(read_metadata(session.directory)["transcript"], status="queued"),
    )
    _stage_journaled_publish(lib, session.directory, title="Trash replay before recovery")

    enqueued = []
    app = SimpleNamespace(
        transcription_enabled=True,
        transcription_mode="recordOnly",
        _quitting=False,
        _capture_exiting=False,
        _observed_saving_directories=(str(session.directory),),
        transcriber=None,
        voice_store=None,
        native_captures={},
        _browser_snapshot={},
        get_windows=lambda: [],
        _transcription_changed=lambda: False,
        _sync_live_follower=lambda **_kw: None,
        write_down=lambda directory, resume=False: enqueued.append((Path(directory).resolve(), resume)),
    )
    app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
    app._on_recovery_result = lambda directory, error, *, transcript_status=None: (
        CassiniApplication._on_recovery_result(app, directory, error, transcript_status=transcript_status)
    )

    trash_stub = SimpleNamespace(app=app)
    trash_stub._published_recovered = lambda d, s: MeetingTrash._published_recovered(trash_stub, d, s)

    recovery_stub = SimpleNamespace(
        app=app,
        cancelled=threading.Event(),
        lock=threading.Lock(),
        _activity={},
        failures={session.directory: OSError("stale failure")},
        attempted=set(),
        started_revision=0,
        _requested={},
        _retry_paths=lambda _roots: None,
        _phase=lambda _path, _saving: None,
    )
    app.library_recovery = recovery_stub
    recovery_stub._result_finished = lambda d, e, s=None: LibraryRecovery._result_finished(recovery_stub, d, e, s)
    monkeypatch.setattr("cassini_talk.library_location.roots", lambda: [lib])

    def drain_glib():
        if hasattr(GLib, "MainContext"):
            ctx = GLib.MainContext.default()
            while ctx.iteration(False):
                pass

    # 1. Trash maintenance runs FIRST and replays the staged publish entry.
    assert MeetingTrash._maintain(trash_stub) is True
    drain_glib()

    # 2. LibraryRecovery runs SECOND (journal entry already retired) -> must not double-enqueue.
    LibraryRecovery._sweep(recovery_stub)
    drain_glib()

    assert read_metadata(session.directory)["published"] is True
    assert session.directory not in recovery_stub.failures
    assert app._observed_saving_directories == ()
    assert enqueued == [(session.directory.resolve(), True)]


def test_enqueue_from_recovery_does_not_call_path_resolve_on_gtk_thread(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _ensure_gi_for_main(monkeypatch)
    monkeypatch.setattr("cassini_talk.library_location.owned_directory", lambda d: Path(d).resolve())
    from cassini_talk.main import CassiniApplication

    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    tracker = dict(pids=[], clients=[], planned=[], completes=[])
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(
            b, engine="whistle", timeout=timeout, tracker=tracker
        ),
        mode=TranscriptionMode.RECORD_ONLY,
    )
    session = RecordingSession(lib, "GTK no-resolve enqueue", source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session.append("microphone", make_speech_pcm16(16000, 1, [(0.8, 440.0, 0.3), (0.6, 0.0, 0.0)]), 0.0)
    session.finish()

    app = SimpleNamespace(
        transcription_enabled=True,
        transcription_mode="recordOnly",
        _quitting=False,
        _capture_exiting=False,
        _observed_saving_directories=(str(session.directory),),
        _transcription_stops={str((lib / "unrelated-stopping-meeting").absolute())},
        transcriber=queue,
        native_captures={},
        _browser_snapshot={},
        get_windows=lambda: [],
        _ensure_transcriber=lambda: queue,
        write_down=lambda directory, resume=False: queue.enqueue(directory, resume=resume),
    )
    app.follower_capture_directories = lambda: CassiniApplication.follower_capture_directories(app)
    app._effective_transcription_mode = lambda: CassiniApplication._effective_transcription_mode(app)
    app._sync_live_follower = lambda **kw: CassiniApplication._sync_live_follower(app, **kw)

    gtk_tid = threading.get_ident()
    orig_resolve = Path.resolve
    gtk_resolves = []

    def poisoned_gtk_resolve(self, *args, **kwargs):
        if threading.get_ident() == gtk_tid:
            gtk_resolves.append(str(self))
        return orig_resolve(self, *args, **kwargs)

    try:
        monkeypatch.setattr(Path, "resolve", poisoned_gtk_resolve)
        CassiniApplication._on_recovery_result(app, session.directory, None, transcript_status="queued")
        assert not gtk_resolves, f"Path.resolve() called on GTK thread during recovery enqueue: {gtk_resolves}"
        assert app._observed_saving_directories == ()
        monkeypatch.setattr(Path, "resolve", orig_resolve)
        finished.wait(1, bound=30)
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
    finally:
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)


def test_stop_queued_meeting_cancellation_does_not_call_path_resolve_on_gtk_thread(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    bundle = FakeBundle.create(tmp_path)
    queue = TranscriptionQueue(
        lambda _: None,
        lambda _: None,
        bundle=bundle,
        root=lib,
        client_factory=lambda b, *, timeout=120: SubprocessTestClient(b, engine="whistle", timeout=timeout),
        mode=TranscriptionMode.AFTER_STOP,
    )
    session_a = RecordingSession(lib, "Blocking preparer meeting", source="gnome")
    session_a.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session_a.append("microphone", make_speech_pcm16(16000, 1, [(0.5, 440.0, 0.3)]), 0.0)
    session_a.finish()

    session_b = RecordingSession(lib, "Queued cancelled meeting", source="gnome")
    session_b.add_track("microphone", None, "microphone", 16000, 1, filename="track-microphone.wav")
    session_b.append("microphone", make_speech_pcm16(16000, 1, [(0.5, 440.0, 0.3)]), 0.0)
    session_b.finish()

    prep_entered = threading.Event()
    release_prep = threading.Event()
    orig_mark_queued = queue._mark_queued

    def gated_mark_queued(directory, resume):
        if directory == session_a.directory.absolute():
            prep_entered.set()
            durable(release_prep, "Preparer gate was not released")
        return orig_mark_queued(directory, resume)

    monkeypatch.setattr(queue, "_mark_queued", gated_mark_queued)

    gtk_tid = threading.get_ident()
    orig_resolve = Path.resolve
    gtk_resolves = []

    def poisoned_gtk_resolve(self, *args, **kwargs):
        if threading.get_ident() == gtk_tid:
            gtk_resolves.append(str(self))
        return orig_resolve(self, *args, **kwargs)

    stop_done = threading.Event()
    try:
        assert queue.enqueue(session_a.directory)
        durable(prep_entered, "Meeting A did not hold the preparer worker")

        monkeypatch.setattr(Path, "resolve", poisoned_gtk_resolve)
        assert queue.enqueue(session_b.directory)
        # Future.cancel() on queued session_b runs _prepared -> _completed synchronously on this (GTK) thread!
        assert queue.stop(session_b.directory, done=stop_done.set)
        assert not gtk_resolves, f"Path.resolve() called on GTK thread during Stop cancellation: {gtk_resolves}"
        assert session_b.directory.absolute() not in queue.pending
        assert session_b.directory.absolute() not in queue._progress

        monkeypatch.setattr(Path, "resolve", orig_resolve)
        durable(stop_done, "Stop did not complete for cancelled queued meeting")
    finally:
        release_prep.set()
        queue.close()
        queue.executor.shutdown(wait=True)
        if getattr(queue, "_close_thread", None) is not None:
            queue._close_thread.join(timeout=5.0)
