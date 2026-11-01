#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare public historical speech with both pinned models, without recognition."""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
import wave
from array import array
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("nemotron", type=Path)
parser.add_argument("voices", type=Path)
parser.add_argument("audio", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--modules", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from cassini_talk.diarization_models import DiarizationModels  # noqa: E402
from cassini_talk.meeting_store import atomic_json, update_metadata  # noqa: E402
from cassini_talk.model_download import ModelStore  # noqa: E402
from cassini_talk.nemotron_model import MODEL_TAG  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402
from cassini_talk.transcription import REFERENCE, TranscriptionQueue  # noqa: E402
from cassini_talk.voice_models import VoiceModels  # noqa: E402
from cassini_talk.voice_store import VoiceStore  # noqa: E402
from cassini_talk.worker_diarization import FORMAT, PARAMS  # noqa: E402


def until(check):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    assert check(), "Historical print preparation did not finish"


args.output.mkdir(parents=True, exist_ok=True)
golden = json.loads((Path(__file__).resolve().parents[1] / "ux/diarization/golden.json").read_text())
assert hashlib.sha256(args.audio.read_bytes()).hexdigest() == golden["input"]["sha256"]
models = DiarizationModels(
    args.bundle, lambda: None, store=ModelStore(args.nemotron, user_agent="CassiniTalk/test (GNOME; Linux; source)")
)
voices = VoiceModels(
    args.bundle,
    lambda: None,
    enabled=lambda: models.snapshot()["ready"],
    store=ModelStore(args.voices, user_agent="CassiniTalk/test (GNOME; Linux; source)"),
)
store = VoiceStore(args.output / "private")
foreground, completed = [], []
results = []
try:
    until(lambda: not models.snapshot()["busy"] and not voices.snapshot()["busy"])
    assert models.snapshot()["ready"] and voices.snapshot()["ready"]
    with tempfile.TemporaryDirectory(dir=args.output) as temporary:
        library = Path(temporary)
        queue = TranscriptionQueue(
            lambda _: None,
            foreground.append,
            root=library,
            bundle=args.bundle,
            voice_models=voices,
            diarization_models=models,
            voice_store=store,
        )
        try:
            for index, divided in enumerate((False, True), 1):
                session = RecordingSession(library, source="gnome", title="Public historical speech")
                with wave.open(str(args.audio)) as audio:
                    session.add_track("system", "The others", "system", 16000, 1)
                    frames = 0
                    while chunk := audio.readframes(16000):
                        session.append("system", chunk, frames / 16)
                        frames += len(chunk) // 2
                session.finish()
                if divided:
                    digest = hashlib.sha256()
                    with wave.open(str(args.audio)) as audio:
                        while chunk := audio.readframes(16000):
                            samples = array("h")
                            samples.frombytes(chunk)
                            digest.update(array("f", (sample / 32768 for sample in samples)).tobytes())
                    atomic_json(
                        session.directory / "speaker-turns.json",
                        dict(
                            format=FORMAT,
                            model=MODEL_TAG,
                            params=PARAMS,
                            groups=[
                                dict(
                                    id="others",
                                    durationMs=40740,
                                    inputSHA256=digest.hexdigest(),
                                    turns=golden["expected"]["turns"],
                                )
                            ],
                        ),
                    )
                ids = ["others~1", "others~2"] if divided else ["others"]
                words = [
                    dict(
                        text=w["text"],
                        speaker=w["speakerId"] if divided else "others",
                        startMs=w["startMs"],
                        endMs=w["endMs"],
                    )
                    for w in golden["expected"]["words"]
                ]
                update_metadata(
                    session.directory,
                    transcript=dict(status="done", reference=REFERENCE),
                    speakers=[
                        dict(id=s, name="Typed before models", provenance="user", colourIndex=i)
                        for i, s in enumerate(ids)
                    ],
                )
                atomic_json(
                    session.directory / "transcript.json", dict(format="cassini.words.v1", language="en", items=words)
                )
                untouched = {p.name: p.read_bytes() for p in session.directory.iterdir()}
                assert queue.backfill_voices(session.directory, lambda *value: completed.append(value))
                until(lambda index=index: len(completed) == index)
                private, prepared = store.preparation(session.directory)
                assert prepared and set(private["speakers"]) == set(ids)
                assert all(
                    p["seconds"] >= 4 and len(p["embedding"]) == 512 and p["match"]["state"] == "none"
                    for p in private["speakers"].values()
                )
                assert untouched == {p.name: p.read_bytes() for p in session.directory.iterdir()}
                assert not foreground
                assert store.remember(session.directory, ids[0], f"Prepared later {index}")
                assert len(store.snapshot()["voices"]) == index
                results.append(dict(divided=divided, printCount=len(ids), folderUnchanged=True, enrolled=True))
        finally:
            queue.close()
            until(lambda: queue.client is None)
    assert "libsherpa-onnx-c-api" not in Path("/proc/self/maps").read_text()
    (args.output / "backfill.json").write_text(json.dumps(dict(cases=results, parentEngineFree=True), indent=2) + "\n")
finally:
    models.close()
    voices.close()
    shutil.rmtree(store.root, ignore_errors=True)
print(
    "PASS pinned historical preparation: before both models, previously divided, real inference, names/words/capture unchanged, enrol works"
)
