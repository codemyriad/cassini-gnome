#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real FIFO, pinned worker, atomic sidecar and recorded model identity, offline."""

import argparse
import hashlib
import json
import os
import sys
import threading
import wave
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("bundle", "model", "runtime", "modules", "output"):
    parser.add_argument("--" + name, required=True, type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from golden_check import assess, punctuation_match  # noqa: E402

from cassini_talk.meeting_store import read_metadata  # noqa: E402
from cassini_talk.parakeet_client import ParakeetClient  # noqa: E402
from cassini_talk.parakeet_model import MODEL_ID, MODEL_TAG  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402
from cassini_talk.transcription import TranscriptionQueue  # noqa: E402

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
root = args.output.parent / "queue-library"
root.mkdir()
os.environ["CASSINI_LIBRARY_DIR"] = str(root)
fixture = Path(__file__).parents[1] / "whistle/pipeline/segmentation-input.wav"
golden_bytes = Path(__file__).with_name("parakeet-golden.json").read_bytes()
golden = json.loads(golden_bytes)
assert hashlib.sha256(fixture.read_bytes()).hexdigest() == golden["inputSha256"]
sessions, before = [], []
for title in ("First", "Second"):
    session = RecordingSession(root, title, source="gnome")
    session.add_track("microphone", None, "microphone", 16000, 1)
    with wave.open(str(fixture)) as audio:
        frames = 0
        while data := audio.readframes(16000):
            session.append("microphone", data, 500 + frames * 1000 / 16000)
            frames += len(data) // 2
    session.finish()
    sessions.append(session)
    before.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.iterdir()})

children, clients, assessments = [], [], []


class CheckedClient(ParakeetClient):
    def process_tracks(self, tracks, notify):
        events = []

        def progress(event):
            if event["type"] == "progress":
                events.append(
                    dict(
                        event,
                        words=[
                            dict(word, startMs=word["startMs"] - 500, endMs=word["endMs"] - 500)
                            for word in event["words"]
                        ],
                    )
                )
            notify(event)

        result = super().process_tracks(tracks, progress)
        assessment = assess(events, golden_bytes, "parakeet-golden.json")
        assessments.append(assessment)
        assert assessment["accepted"], assessment
        return result


class Models:
    def snapshot(self):
        return dict(selected=MODEL_ID)

    def start_parakeet(self, bundle, *, register):
        assert all(client.process is None for client in clients), "Previous speech worker still alive"
        client = CheckedClient(bundle, args.model, args.runtime, timeout=120)
        register(client)
        client.start()
        children.append(client.process)
        clients.append(client)
        return client


finished = []
done = threading.Event()


def complete(directory):
    finished.append(directory)
    if len(finished) == 2:
        done.set()


queue = TranscriptionQueue(lambda _: None, complete, bundle=args.bundle, root=root, models=Models())
try:
    for session in sessions:
        assert queue.enqueue(session.directory)
    assert done.wait(120), "Queue did not finish both Parakeet jobs"
finally:
    queue.close()
    queue.executor.shutdown(wait=True)

checks = []
for session, original in zip(sessions, before, strict=True):
    metadata = read_metadata(session.directory)
    assert metadata["transcript"]["status"] == "done" and metadata["transcript"]["model"] == MODEL_TAG, metadata
    body = json.loads((session.directory / "transcript.json").read_text())
    expected = golden["words"]
    assert len(body["items"]) == len(expected) == 66
    for actual, word in zip(body["items"], expected, strict=True):
        assert punctuation_match(actual["text"], word["text"])
        assert abs(actual["startMs"] - word["startMs"] - 500) <= 80
        assert abs(actual["endMs"] - word["endMs"] - 500) <= 80
        assert actual["speaker"] == "me"
    assert all(
        hashlib.sha256((session.directory / name).read_bytes()).hexdigest() == digest
        for name, digest in original.items()
    )
    checks.append(
        dict(
            meeting=session.directory.name,
            words=len(body["items"]),
            model=metadata["transcript"]["model"],
            captureBytesUnchanged=True,
            trackOffsetMs=500,
        )
    )
assert len({child.pid for child in children}) == 2 and all(child.poll() == 0 for child in children)
assert "cassini_talk.parakeet" not in sys.modules and "onnxruntime" not in sys.modules
report = dict(
    meetings=checks,
    goldenAssessments=assessments,
    distinctReapedChildren=[child.pid for child in children],
    noEngineInParent=True,
)
args.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2), flush=True)
