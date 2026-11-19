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
    should_auto_write,
)
from cassini_talk.whistle_client import WhistleClient  # noqa: E402

WORKER_SCRIPT = Path(__file__).with_name("fake_follow_worker.py")


def wait_until(predicate, timeout=15.0, message="Condition not met"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), message


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
            wait_until(
                lambda: (
                    (session.directory / RECOGNITION_CACHE_FILE).is_file()
                    and len(json.loads((session.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {}))
                    >= (3 if two_tracks else 2)
                ),
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
            wait_until(
                lambda: (
                    (session.directory / RECOGNITION_CACHE_FILE).is_file()
                    and len(json.loads((session.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {}))
                    >= 3
                ),
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
            wait_until(lambda: len(tracker["pids"]) == 1, message="Follower worker did not start")
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
            wait_until(
                lambda: (
                    (session.directory / RECOGNITION_CACHE_FILE).is_file()
                    and len(json.loads((session.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {}))
                    >= 2
                ),
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
            wait_until(
                lambda: (
                    (session.directory / RECOGNITION_CACHE_FILE).is_file()
                    and len(json.loads((session.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {}))
                    >= 2
                ),
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
            wait_until(
                lambda: (
                    (session.directory / RECOGNITION_CACHE_FILE).is_file()
                    and len(json.loads((session.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {}))
                    >= 1
                ),
                message="Follower did not cache first segment before signal injection",
            )
            pid = tracker["pids"][0]
            os.kill(pid, inject_sig)
            if inject_sig == signal.SIGKILL:
                wait_until(lambda: queue._follow_directory is None, message="Queue did not clean up after SIGKILL")
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
        wait_until(
            lambda: (
                queue._follow_drained.is_set()
                and (session.directory / RECOGNITION_CACHE_FILE).is_file()
                and len(json.loads((session.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {})) == 2
            ),
            message="Tail planning/ASR did not overlap Opus publication",
        )
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
        wait_until(lambda: len(tracker["pids"]) == 1, message="Follower did not start")
        assert queue.enqueue(saved_session.directory)
        finished.wait(1, bound=30)
        assert queue._follow_directory is None
        assert read_metadata(saved_session.directory)["transcript"]["status"] == "done"

        # 2) Re-attach follower, then switch model -> model_changed() preempts follower!
        assert queue.follow_recording(live_session.directory)
        wait_until(lambda: queue._follow_client_model == "whistle", message="Follower did not reattach")
        assert sum(1 for c in tracker["clients"] if c.is_alive()) == 1
        models.select_for_test(PARAKEET_MODEL_ID)
        queue.model_changed()
        wait_until(lambda: queue._follow_directory is None, message="model_changed did not preempt follower")

        # 3) Re-attach follower on Parakeet, then Trash (cancel_meeting) cancels & waits cleanly!
        assert queue.follow_recording(live_session.directory)
        wait_until(lambda: queue._follow_client_model == PARAKEET_MODEL_ID, message="Parakeet follower did not start")
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
        packet_frames = 960  # 20 ms at 48 kHz
        speech_pkt = make_speech_pcm16(48000, 2, [(0.02, 440.0, 0.3)])
        silence_pkt = bytes(packet_frames * 4)
        expected_mic = bytearray()
        expected_sys = bytearray()
        for idx in range(400):
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
        wait_until(
            lambda: (
                (session.directory / RECOGNITION_CACHE_FILE).is_file()
                and len(json.loads((session.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {})) >= 4
            ),
            message="Stress follower did not cache segments from simultaneous 2-track capture",
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
        wait_until(
            lambda: (
                len(tracker["pids"]) == 1
                and (session.directory / RECOGNITION_CACHE_FILE).is_file()
                and len(json.loads((session.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {})) >= 1
            ),
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
        wait_until(
            lambda: (
                (live_2.directory / RECOGNITION_CACHE_FILE).is_file()
                and len(json.loads((live_2.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {})) >= 1
            ),
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
        wait_until(
            lambda: (
                queue._follow_client_model == PARAKEET_MODEL_ID
                and (live_3.directory / RECOGNITION_CACHE_FILE).is_file()
                and len(json.loads((live_3.directory / RECOGNITION_CACHE_FILE).read_text()).get("segments", {})) >= 1
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
