#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed production queue, real model controller and unchanged-group retry."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("model_cache", type=Path)
parser.add_argument("audio", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--modules", type=Path, required=True)
parser.add_argument("--voices", type=Path)
parser.add_argument("--parakeet", type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from cassini_talk.diarization_models import DiarizationModels  # noqa: E402
from cassini_talk.library import LibraryScanner  # noqa: E402
from cassini_talk.meeting_store import read_metadata, update_metadata  # noqa: E402
from cassini_talk.model_download import ModelStore  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402
from cassini_talk.transcription import TranscriptionQueue  # noqa: E402

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
assert {name for _, name in socket.if_nameindex()} == {"lo"}
args.output.mkdir(parents=True, exist_ok=True)
library = Path(tempfile.mkdtemp(prefix="Library-", dir=args.output))
models = DiarizationModels(
    args.bundle, lambda: None, store=ModelStore(args.model_cache, user_agent="CassiniTalk/test (GNOME; Linux; source)")
)


def until(check, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("Queue condition did not settle")


until(lambda: not models.snapshot()["busy"])
assert models.snapshot()["ready"], models.snapshot()
session = RecordingSession(library, source="gnome", title="S-11 natural voices")
with wave.open(str(args.audio)) as audio:
    session.add_track("system", "The others", "system", audio.getframerate(), 1)
    frame = 0
    while chunk := audio.readframes(16000):
        session.append("system", chunk, frame * 1000 / audio.getframerate())
        frame += len(chunk) // 2
session.finish()
before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.iterdir()}
finished = []
voices = store = speech = None
if args.voices:
    from cassini_talk.voice_models import VoiceModels
    from cassini_talk.voice_store import VoiceStore

    store = VoiceStore(args.output / "private-voices")
    voices = VoiceModels(
        args.bundle,
        lambda: None,
        enabled=lambda: models.snapshot()["ready"],
        store=ModelStore(args.voices, user_agent="CassiniTalk/test (GNOME; Linux; source)"),
    )
    until(lambda: not voices.snapshot()["busy"])
    assert voices.snapshot()["ready"], voices.snapshot()
if args.parakeet:
    from cassini_talk.parakeet_client import ParakeetClient

    class SelectedSpeech:
        def snapshot(self):
            return dict(selected="parakeet-ultra-0.6b-int8")

        def start_parakeet(self, bundle, *, register):
            from cassini_talk.sherpa_runtime import runtime_path

            client = ParakeetClient(bundle, args.parakeet, runtime_path())
            register(client)
            client.start()
            return client

    speech = SelectedSpeech()
queue = TranscriptionQueue(
    lambda _: None,
    finished.append,
    root=library,
    bundle=args.bundle,
    diarization_models=models,
    voice_models=voices,
    voice_store=store,
    models=speech,
)
try:
    assert queue.enqueue(session.directory)
    until(lambda: len(finished) == 1)
    metadata = read_metadata(session.directory)
    assert metadata["transcript"]["status"] == "done", metadata
    (meeting,) = LibraryScanner(library).scan().meetings
    assert len(meeting.words) > 100 and {s.id for s in meeting.speakers} == {"others~1", "others~2"}
    cache_bytes = (session.directory / "speaker-turns.json").read_bytes()
    cache = json.loads(cache_bytes)
    assert cache["groups"][0]["id"] == "others" and len(cache["groups"][0]["turns"]) == 5
    voice = meeting.speakers[0].id
    if store is not None:
        prints = store.meeting(session.directory)["speakers"]
        assert set(prints) == {"others~1", "others~2"}
        assert all(len(p["embedding"]) == 512 and p["seconds"] >= 4 for p in prints.values())
        store.remember(session.directory, voice, "Alice")
        assert store.snapshot()["voices"][0]["count"] == 1
    speakers = [dict(s, name="Alice", provenance="user") if s["id"] == voice else s for s in metadata["speakers"]]
    update_metadata(session.directory, speakers=speakers, transcript=dict(metadata["transcript"], status="failed"))
    assert queue.enqueue(session.directory)
    until(lambda: len(finished) == 2)
    (retried,) = LibraryScanner(library).scan().meetings
    assert retried.transcript == "done" and retried.speakers[0].id == voice
    assert retried.speakers[0].name == "Alice" and retried.speakers[0].provenance == "user"
    assert (session.directory / "speaker-turns.json").read_bytes() == cache_bytes
    if store is not None:
        prints = store.meeting(session.directory)["speakers"]
        assert prints[voice]["match"]["state"] == "confirmed"
        assert store.snapshot()["voices"][0]["count"] == 1
        assert store.snapshot()["voices"][0]["meetings"] == [session.directory.name]
        assert "embedding" not in (session.directory / "meeting.json").read_text()
        assert "embedding" not in (session.directory / "transcript.json").read_text()
        # Public reports contain no biometric cache, even for public test speech.
        shutil.rmtree(store.root)
    assert before == {name: hashlib.sha256((session.directory / name).read_bytes()).hexdigest() for name in before}
    assert "libneedle3.so" not in Path("/proc/self/maps").read_text()
    assert "libsherpa-onnx-c-api" not in Path("/proc/self/maps").read_text()
    (args.output / "queue.json").write_text(
        json.dumps(
            dict(
                words=len(retried.words),
                voices=[s.id for s in retried.speakers],
                namedVoice=voice,
                cacheSHA256=hashlib.sha256(cache_bytes).hexdigest(),
                captureUnchanged=True,
                parentEngineFree=True,
                runtime=models.snapshot(),
            ),
            indent=2,
        )
        + "\n"
    )
finally:
    if store is not None:
        shutil.rmtree(store.root, ignore_errors=True)
    queue.close()
    models.close()
    if voices:
        voices.close()
    until(lambda: queue.client is None, timeout=5)
    until(
        lambda: not any(t.name.startswith("cassini-transcription") and t.is_alive() for t in threading.enumerate()),
        timeout=5,
    )
print("PASS installed production queue: two voices, atomic sidecar, stable typed name on retry and unchanged capture")
