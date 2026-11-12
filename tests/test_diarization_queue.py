# SPDX-License-Identifier: GPL-3.0-or-later
"""Metadata publication and completed-recognition salvage after child failure."""

import hashlib
import json
import threading

import pytest
from durable import Finished, durable

from cassini_talk.diarization import voice_source
from cassini_talk.library import LibraryScanner
from cassini_talk.meeting_store import read_metadata, update_metadata
from cassini_talk.nemotron_model import MODEL_TAG
from cassini_talk.recording_session import RecordingSession
from cassini_talk.transcription import TranscriptionQueue
from cassini_talk.worker_diarization import FORMAT, PARAMS


class Models:
    def request(self, directory, *, origin):
        return dict(modelPath="test-model", runtimePath="test-runtime", origin=origin)

    def measured(self, _factor):
        pass


@pytest.mark.parametrize("failure", [None, "native-crash", "model-error"])
def test_voice_publication_and_diarization_failures_preserve_words_and_capture(tmp_path, failure):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    session.finish()
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.iterdir()}
    turns = [dict(startMs=0, endMs=900, speaker=0), dict(startMs=1000, endMs=2000, speaker=1)]
    # G12: the name was typed on this exact input and these turns, so it stays.
    source = voice_source("a" * 64, turns)
    update_metadata(
        session.directory,
        speakers=[dict(id="others~1", name="Alice", provenance="user", colourIndex=0, voiceSource=source)],
        notes=[dict(atMs=100, text="Keep this")],
        privateField="preserved",
    )
    words = [
        dict(text="One", startMs=10, endMs=800, lowConfidenceSpeaker=True),
        dict(text="Two", startMs=1010, endMs=1800),
    ]
    finished = Finished()

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            assert self.diarization["modelPath"] == "test-model"
            notify(
                dict(type="progress", trackId=tracks[0]["id"], trackOrder=0, language="en", etaMinutes=1, words=words)
            )
            notify(dict(type="recognised", summary=dict(language="en", diagnostics=[]), etaMinutes=1))
            if failure == "native-crash":
                raise RuntimeError("Worker exited in native diarization")
            if failure == "model-error":
                return dict(diagnostics=[dict(code="diarization-kept-d11")])
            notify(
                dict(
                    type="diarized",
                    groupId="others",
                    durationMs=2000,
                    inputSHA256="a" * 64,
                    turns=turns,
                    first=True,
                    final=True,
                    etaMinutes=0,
                )
            )
            return dict(
                diagnostics=[],
                diarization=dict(
                    model=MODEL_TAG, params=PARAMS, groupIds=["others"], realtimeFactor=0.01, diagnostics=[]
                ),
            )

    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=tmp_path,
        root=tmp_path,
        client_factory=Client,
        diarization_models=Models(),
    )
    try:
        assert queue.enqueue(session.directory)
        finished.wait(1, exactly=True)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "done" and metadata["privateField"] == "preserved"
        assert metadata["notes"] == [dict(atMs=100, text="Keep this")]
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert [{k: v for k, v in word.items() if k != "speaker"} for word in body["items"]] == words
        assert before == {name: hashlib.sha256((session.directory / name).read_bytes()).hexdigest() for name in before}
        if failure is None:
            assert [w["speaker"] for w in body["items"]] == ["others~1", "others~2"]
            cache = json.loads((session.directory / "speaker-turns.json").read_bytes())
            assert cache["format"] == FORMAT and cache["model"] == MODEL_TAG and cache["groups"][0]["turns"] == turns
            (meeting,) = LibraryScanner(tmp_path).scan().meetings
            assert meeting.speaker_names == ("Alice", "Speaker 2")
            assert meeting.speakers[0].provenance == "user"
            assert read_metadata(session.directory)["speakers"][-1]["voiceSource"] == source
        else:
            assert [w["speaker"] for w in body["items"]] == ["others", "others"]
            assert not (session.directory / "speaker-turns.json").exists()
    finally:
        queue.close()


def test_actual_child_crash_after_recognition_finishes_with_d11(tmp_path, monkeypatch):
    from test_whistle_spike import child_client

    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    session.finish()
    client = child_client(
        tmp_path,
        monkeypatch,
        """import json,os,sys
print(json.dumps(dict(type='ready')))
request=json.loads(sys.stdin.readline())
track=request['tracks'][0]
print(json.dumps(dict(type='progress',trackId=track['id'],trackOrder=0,
    language='en',etaMinutes=1,words=[dict(text='Kept',startMs=10,endMs=800)])))
print(json.dumps(dict(type='recognised',summary=dict(language='en',diagnostics=[]),etaMinutes=1)))
os._exit(77)
""",
    )
    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        root=tmp_path,
        bundle=tmp_path,
        client_factory=lambda *_args, **_kwargs: client,
        diarization_models=Models(),
    )
    try:
        assert queue.enqueue(session.directory)
        finished.wait(1, exactly=True)
        assert read_metadata(session.directory)["transcript"]["status"] == "done"
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert body["items"] == [dict(text="Kept", startMs=10, endMs=800, speaker="others")]
        assert not (session.directory / "speaker-turns.json").exists()
        assert client.process is None and queue.client is None
    finally:
        queue.close()


class CompletedClient:
    """Deterministic child messages, with one unpublished buffered word."""

    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        return self

    def close(self):
        pass

    def before_return(self):
        pass

    def process_tracks(self, tracks, notify):
        for text, start in (("One", 10), ("Two", 1010)):
            notify(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=1,
                    words=[dict(text=text, startMs=start, endMs=start + 700)],
                )
            )
        notify(dict(type="recognised", summary=dict(language="en", diagnostics=[]), etaMinutes=1))
        notify(
            dict(
                type="diarized",
                groupId="others",
                durationMs=2000,
                inputSHA256="a" * 64,
                turns=[dict(startMs=0, endMs=900, speaker=0), dict(startMs=1000, endMs=2000, speaker=1)],
                first=True,
                final=True,
                etaMinutes=0,
            )
        )
        self.before_return()
        return dict(
            diagnostics=[], diarization=dict(model=MODEL_TAG, params=PARAMS, groupIds=["others"], diagnostics=[])
        )


@pytest.fixture
def system_capture(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    session.finish()
    return session


def file_snapshot(directory):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}


def test_close_after_worker_return_does_not_flush_buffered_words(system_capture, tmp_path):
    session = system_capture
    snapshots, finished = [], Finished()

    class Client(CompletedClient):
        def before_return(self):
            queue.close()
            snapshots.append(file_snapshot(session.directory))

    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        root=tmp_path,
        bundle=tmp_path,
        client_factory=Client,
        diarization_models=Models(),
    )
    try:
        assert queue.enqueue(session.directory)
        finished.wait(1, exactly=True)
        assert len(snapshots) == 1
        assert file_snapshot(session.directory) == snapshots[0], "Queue committed files after close"
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert [w["text"] for w in body["items"]] == ["One"]
        assert read_metadata(session.directory)["transcript"]["status"] == "running"
        assert not queue.pending
    finally:
        queue.close()


def test_cancelled_attribution_remains_running_and_restart_redoes_missing_turns(system_capture, tmp_path, monkeypatch):
    from cassini_talk import diarization, transcript_publish

    session = system_capture
    turns = [dict(startMs=0, endMs=900, speaker=0), dict(startMs=1000, endMs=2000, speaker=1)]
    # G12: the redone turns equal the ones Alice was named on, so the name stays.
    source = voice_source("a" * 64, turns)
    update_metadata(
        session.directory,
        speakers=[dict(id="others~1", name="Alice", provenance="user", colourIndex=0, voiceSource=source)],
    )
    entered, release = threading.Event(), threading.Event()
    completed_flushes, finished = [], Finished()
    apply = diarization.apply_transcript
    flush = transcript_publish.TranscriptPublisher.flush

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return apply(*args, **kwargs)

    def observed(publisher, **kwargs):
        if kwargs.get("complete"):
            completed_flushes.append(1)
        return flush(publisher, **kwargs)

    monkeypatch.setattr(diarization, "apply_transcript", paused)
    monkeypatch.setattr(transcript_publish.TranscriptPublisher, "flush", observed)
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        root=tmp_path,
        bundle=tmp_path,
        client_factory=CompletedClient,
        diarization_models=Models(),
    )
    try:
        assert queue.enqueue(session.directory)
        durable(entered, "the job did not reach diarization")
        assert not completed_flushes, "Final flush happened before speaker assignment"
        assert read_metadata(session.directory)["transcript"]["status"] == "running"
        assert not (session.directory / "speaker-turns.json").exists()
        queue.close()
        before = file_snapshot(session.directory)
        release.set()
        finished.wait(1, exactly=True)
        assert file_snapshot(session.directory) == before
        assert not completed_flushes
    finally:
        release.set()
        queue.close()
    monkeypatch.setattr(diarization, "apply_transcript", apply)
    resumed = TranscriptionQueue(
        lambda _: None,
        finished,
        root=tmp_path,
        bundle=tmp_path,
        client_factory=CompletedClient,
        diarization_models=Models(),
    )
    try:
        assert resumed.enqueue(session.directory, resume=True)
        finished.wait(2, exactly=True)
        metadata = read_metadata(session.directory)
        assert metadata["transcript"]["status"] == "done"
        body = json.loads((session.directory / "transcript.json").read_bytes())
        assert [w["speaker"] for w in body["items"]] == ["others~1", "others~2"]
        assert json.loads((session.directory / "speaker-turns.json").read_bytes())["groups"]
        assert next(s for s in metadata["speakers"] if s["id"] == "others~1")["name"] == "Alice"
        assert len(completed_flushes) == 1
    finally:
        resumed.close()


def test_transcribing_reports_a_queued_or_running_job_until_it_finishes(tmp_path):
    """G12: the Speakers dialog holds Remember while a job may re-diarize the meeting."""
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("room", "The others", "system", 16000, 1)
    session.append("room", b"\x01\x00" * 32000, 0)
    session.finish()
    entered, release, finished = threading.Event(), threading.Event(), Finished()

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            entered.set()
            assert release.wait(10)
            notify(dict(type="recognised", summary=dict(language="en", diagnostics=[]), etaMinutes=0))
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _: None, finished, bundle=tmp_path, root=tmp_path, client_factory=Client)
    try:
        assert not queue.transcribing(session.directory)
        assert queue.enqueue(session.directory)
        assert queue.transcribing(session.directory)
        assert entered.wait(10)
        assert queue.transcribing(session.directory)
        release.set()
        finished.wait(1, exactly=True)
        assert not queue.transcribing(session.directory)
    finally:
        release.set()
        queue.close()


@pytest.mark.parametrize("third_speaker", [True, False], ids=["two-remote-voices", "single-remote-voice"])
def test_mixed_browser_remote_track_separates_saved_voice_from_third_speaker(tmp_path, monkeypatch, third_speaker):
    """SFU remote tracks with 2+ voices split before voice matching; 1-voice remote tracks stay D11."""
    np = pytest.importorskip("numpy")  # Core/Whistle-light profiles need no PCM worker dependencies.
    from cassini_talk import campplus, nemotron, worker_diarization, worker_voices
    from cassini_talk.diarization import group_tracks, normalize_turns
    from cassini_talk.nemotron_model import MODEL_ID, REVISION
    from cassini_talk.voice_store import VoiceStore
    from cassini_talk.voices_logic import DIMENSION, MIN_SECONDS, normalised

    library_root = tmp_path / "library"
    scratch = tmp_path / "scratch"
    store = VoiceStore(tmp_path / "private" / "voices")
    store.refresh()
    chris_vec = [1.0] + [0.0] * (DIMENSION - 1)
    prior = "Recording-12345678-1234-4234-8234-123456789abc"
    store.commit_job(prior, {"others~1": dict(embedding=chris_vec, seconds=8.0)}, store.snapshot()["generation"])
    store.remember(prior, "others~1", "Chris")

    rate = 48000
    session = RecordingSession(library_root, "Talking Trademarks", source="browser")
    session.add_track("local-1", "local: MediaStreamAudioDestinationNode", "local", rate, 1)
    session.add_track("remote-1", "remote: remote audio", "remote", rate, 1, source_track_id="teams-recv-1")

    def append_tone(identifier, hz, start_ms, duration_ms):
        for offset in range(0, duration_ms, 1000):
            chunk_ms = min(1000, duration_ms - offset)
            frames = rate * chunk_ms // 1000
            phase = (np.arange(frames, dtype="<f8") + rate * offset // 1000) * (2.0 * np.pi * hz / rate)
            pcm = (np.sin(phase) * 8192).astype("<i2").tobytes() if hz else b"\x00\x00" * frames
            session.append(identifier, pcm, start_ms + offset)

    append_tone("local-1", 440, 0, 2000)
    append_tone("remote-1", 0, 0, 2000)
    append_tone("remote-1", 220, 2000, 7000)
    remote_words = [
        dict(text="Updates", startMs=2500, endMs=4800),
        dict(text="ready", startMs=5200, endMs=8200),
    ]
    if third_speaker:
        append_tone("remote-1", 0, 9000, 1000)
        append_tone("remote-1", 660, 10000, 5500)
        remote_words.extend(
            [
                dict(text="Sounds", startMs=10500, endMs=12600),
                dict(text="great", startMs=13000, endMs=15000),
            ]
        )
    session.finish()

    diarized_calls = []

    class ToneNemotron:
        def __init__(self, *_args):
            pass

        def infer(self, samples, notify=lambda *_: None):
            diarized_calls.append(len(samples))
            notify(1, 1)
            step = 1600  # 100 ms at 16 kHz
            idx = np.arange(step, dtype="<f8")
            basis_chris = np.exp(-2j * np.pi * 220.0 * idx / 16000.0)
            basis_third = np.exp(-2j * np.pi * 660.0 * idx / 16000.0)
            raw = []
            for start in range(0, len(samples) - step + 1, step):
                window = samples[start : start + step]
                amp_chris = 2.0 * abs(np.vdot(basis_chris, window)) / step
                amp_third = 2.0 * abs(np.vdot(basis_third, window)) / step
                speaker = 0 if amp_chris > 0.05 else 1 if amp_third > 0.05 else None
                if speaker is None:
                    continue
                sec_start, sec_end = start / 16000.0, (start + step) / 16000.0
                if raw and raw[-1]["speaker"] == speaker and abs(raw[-1]["end"] - sec_start) < 1e-6:
                    raw[-1]["end"] = sec_end
                else:
                    raw.append(dict(start=sec_start, end=sec_end, speaker=speaker))
            duration_ms = len(samples) * 1000 // 16000
            turns = normalize_turns(raw, duration_ms)
            return dict(turns=turns, speakerCount=len({t["speaker"] for t in turns}), inferenceSeconds=0.01)

        def close(self):
            pass

    class ToneExtractor:
        def __init__(self, *_args):
            pass

        def embed(self, chunks):
            frames = sum(len(chunk) for chunk in chunks)
            if frames < MIN_SECONDS * 16000:
                return None
            energy_chris = energy_third = 0.0
            digest = hashlib.sha256()
            for chunk in chunks:
                chunk = np.ascontiguousarray(chunk, dtype="<f4")
                digest.update(chunk.tobytes())
                idx = np.arange(len(chunk), dtype="<f8")
                amp_chris = 2.0 * abs(np.vdot(np.exp(-2j * np.pi * 220.0 * idx / 16000.0), chunk)) / len(chunk)
                amp_third = 2.0 * abs(np.vdot(np.exp(-2j * np.pi * 660.0 * idx / 16000.0), chunk)) / len(chunk)
                energy_chris += (amp_chris**2) * (len(chunk) / 16000.0)
                energy_third += (amp_third**2) * (len(chunk) / 16000.0)
            vector = normalised([energy_chris, energy_third] + [0.0] * (DIMENSION - 2))
            if vector is None:
                return None
            return dict(
                embedding=vector,
                seconds=frames / 16000.0,
                sampleCount=frames,
                inputSHA256=digest.hexdigest(),
                realtimeFactor=0.01,
            )

        def close(self):
            pass

    monkeypatch.setattr(nemotron, "Nemotron", ToneNemotron)
    monkeypatch.setattr(campplus, "SpeakerExtractor", ToneExtractor)

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.diarization = None
            self.keep_for_voices = False

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            groups = {g["id"]: g for g in group_tracks(tracks)}
            assert groups["me"]["eligible"] is False
            assert groups["track-teams-recv-1"]["eligible"] is True
            for order, track in enumerate(tracks):
                words = [dict(text="Hello", startMs=100, endMs=700)] if track["speaker"] == "me" else remote_words
                notify(
                    dict(
                        type="progress",
                        trackId=track["id"],
                        trackOrder=order,
                        language="en",
                        etaMinutes=0,
                        words=words,
                    )
                )
            summary = dict(segments=len(tracks), language="en", diagnostics=[])
            options = dict(
                self.diarization,
                scratch=str(scratch),
                storedTurnsPath=str(session.directory / "speaker-turns.json"),
            )
            return worker_diarization.complete(
                summary,
                tracks,
                options,
                lambda kind, **fields: notify(dict(type=kind, **fields)),
            )

        def voiceprints(self, tracks, options, heartbeat):
            prints = {}

            def voice_notify(kind, **fields):
                heartbeat()
                if kind == "voice-print":
                    prints[fields["speaker"]] = {k: fields[k] for k in ("embedding", "seconds", "windows")}
                elif kind == "voices-error":
                    raise RuntimeError(fields["message"])

            worker_voices.run(tracks, dict(options, scratch=str(scratch)), voice_notify)
            return prints

    finished = Finished()
    queue = TranscriptionQueue(
        lambda _: None,
        finished,
        bundle=tmp_path,
        root=library_root,
        client_factory=Client,
        diarization_models=Models(),
        voice_models=Models(),
        voice_store=store,
    )
    try:
        assert queue.enqueue(session.directory)
        finished.wait(1, exactly=True)
    finally:
        queue.close()

    assert len(diarized_calls) == 1, "only the remote group is diarized; me stays ineligible"
    metadata = read_metadata(session.directory)
    assert metadata["transcript"]["status"] == "done"
    separation = metadata["recordingDetails"]["writingDown"]["speakerSeparation"]
    assert separation == dict(
        status="completed",
        modelId=MODEL_ID,
        modelRevision=REVISION,
        speakersFound=2 if third_speaker else 1,
        reused=False,
    )
    words = json.loads((session.directory / "transcript.json").read_bytes())["items"]
    (meeting,) = LibraryScanner(library_root).scan().meetings
    by_id = {s["id"]: s for s in metadata["speakers"]}
    if third_speaker:
        assert [w["speaker"] for w in words] == [
            "me",
            "track-teams-recv-1~1",
            "track-teams-recv-1~1",
            "track-teams-recv-1~2",
            "track-teams-recv-1~2",
        ]
        assert by_id["track-teams-recv-1~1"]["name"] == "Chris"
        assert by_id["track-teams-recv-1~1"]["provenance"] == "recognised"
        assert by_id["track-teams-recv-1~2"]["name"] is None
        assert by_id["track-teams-recv-1~2"]["provenance"] == "unnamed"
        assert meeting.speaker_names == ("Me", "Chris", "Speaker 3")
    else:
        assert [w["speaker"] for w in words] == ["me", "track-teams-recv-1", "track-teams-recv-1"]
        assert by_id["track-teams-recv-1"]["name"] == "Chris"
        assert by_id["track-teams-recv-1"]["provenance"] == "recognised"
        assert meeting.speaker_names == ("Me", "Chris")
