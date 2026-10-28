#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed, network-none Whistle -> Nemotron -> S-11 conformance and reuse."""

import argparse
import hashlib
import importlib.util
import json
import os
import socket
import sys
import wave
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("model", type=Path)
parser.add_argument("audio", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--modules", type=Path, required=True)
parser.add_argument("--parakeet-model", type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from cassini_viewer.diarization import apply_group  # noqa: E402
from cassini_viewer.diarization_models import DiarizationProbe  # noqa: E402
from cassini_viewer.sherpa_runtime import runtime_path  # noqa: E402
from cassini_viewer.whistle_client import WhistleClient  # noqa: E402

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
assert {name for _, name in socket.if_nameindex()} == {"lo"}
assert (
    hashlib.sha256(args.audio.read_bytes()).hexdigest()
    == "df49da0c9455ed507c04af0ad3819ad9619d42b7d08b7b016607687ced03f184"
)
root = Path(__file__).parents[1] / "ux/diarization"
spec = importlib.util
loader = spec.spec_from_file_location("s11_check", root / "verify.py")
checker = spec.module_from_spec(loader)
loader.loader.exec_module(checker)
_vectors, golden = checker.integrity()
args.output.mkdir(parents=True, exist_ok=True)
report = dict(network="none", engine="parakeet" if args.parakeet_model else "whistle", cases=[])
with DiarizationProbe(args.bundle, args.model, runtime_path()) as probe:
    assert probe.ready["sampleRate"] == 16000
    assert probe.ready["pid"] != os.getpid()
    assert "libsherpa-onnx-c-api" in Path(f"/proc/{probe.process.pid}/maps").read_text()
    report["runtime"] = probe.ready
assert "libsherpa-onnx-c-api" not in Path("/proc/self/maps").read_text()
with wave.open(str(args.audio)) as audio:
    tracks = [
        dict(
            id="golden",
            path=str(args.audio),
            offsetMs=0,
            speaker="others",
            kind="system",
            frames=audio.getnframes(),
            sampleRate=audio.getframerate(),
        )
    ]
cache = args.output / "speaker-turns.json"
cache.unlink(missing_ok=True)  # Each invocation proves a fresh inference before reuse.
options = dict(
    modelPath=str(args.model),
    runtimePath=str(runtime_path()),
    storedTurnsPath=str(cache),
    scratch=str(args.output / "scratch"),
)
for reused in (False, True):
    events = []
    if args.parakeet_model:
        from cassini_viewer.parakeet_client import ParakeetClient

        client = ParakeetClient(args.bundle, args.parakeet_model, runtime_path(), timeout=120)
    else:
        client = WhistleClient(args.bundle, timeout=120)
    with client:
        client.diarization = options
        result = client.process_tracks(tracks, events.append)
    assert result["vad"]["version"] == "1.31.0"
    assert result["diarization"]["groupIds"] == ["others"], result
    assert not result["diarization"]["diagnostics"], result
    batches = [e for e in events if e["type"] == "diarized"]
    assert batches and batches[0]["first"] and batches[-1]["final"]
    assert all(e["reused"] is reused for e in batches)
    turns = [turn for batch in batches for turn in batch["turns"]]
    chosen = apply_group("others", golden["expected"]["words"], turns)
    # Native labels are arbitrary. Use the same first-turn ordinal rule as
    # the assignment function, independently of the C API speaker count.
    from cassini_viewer.diarization import voice_ordinals

    voice_ids = {s: f"others~{n}" for s, n in voice_ordinals(turns).items()}
    actual = dict(
        inputSHA256=golden["input"]["sha256"],
        modelSourceSHA256=golden["model"]["source_sha256"],
        speakerCount=len(chosen["voiceIds"]),
        turns=[dict(t, speakerId=voice_ids[t["speaker"]]) for t in turns],
        wordSpeakerIds=chosen["wordSpeakerIds"],
    )
    assessment = checker.assess(actual, golden)
    recognition = [word for event in events if event["type"] == "progress" for word in event["words"]]
    assert recognition, "Diarization must follow actual recognition"
    phases = [event["type"] for event in events]
    assert phases.index("recognised") > max(i for i, phase in enumerate(phases) if phase == "progress")
    assert phases.index("diarized") > phases.index("recognised")
    group = dict(id="others", durationMs=batches[-1]["durationMs"], inputSHA256=batches[-1]["inputSHA256"], turns=turns)
    cache.write_text(
        json.dumps(
            dict(
                format="cassini.speaker-turns.v1",
                model=result["diarization"]["model"],
                params=result["diarization"]["params"],
                groups=[group],
            )
        )
    )
    report["cases"].append(
        dict(
            reused=reused,
            assessment=assessment,
            recognitionWords=len(recognition),
            realtimeFactor=result["diarization"]["realtimeFactor"],
            turns=turns,
        )
    )
    (args.output / f"report-{int(reused)}.json").write_text(json.dumps(actual, indent=2) + "\n")
assert "libneedle3.so" not in Path("/proc/self/maps").read_text()
assert "libsherpa-onnx-c-api" not in Path("/proc/self/maps").read_text()
(args.output / "pipeline.json").write_text(json.dumps(report, indent=2) + "\n")
print(f"PASS installed {report['engine']}/Nemotron, two-voice S-11 golden, persisted turns reuse and parent isolation")
