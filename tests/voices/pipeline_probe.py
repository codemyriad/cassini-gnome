#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""S-9 native extraction and matching of only the pinned public S-11 input."""

import argparse
import hashlib
import json
import math
import os
import runpy
import socket
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/ux/voices/golden.json"


def child(modules, model_path, runtime):
    sys.path.insert(0, modules)
    from cassini_viewer.parakeet_worker import block_network

    sys.addaudithook(block_network)
    from cassini_viewer.campplus import SpeakerExtractor
    from cassini_viewer.diarization import voice_ordinals
    from cassini_viewer.voices_logic import select_windows
    from cassini_viewer.whistle_worker import emit

    model = SpeakerExtractor(model_path, runtime)
    try:
        emit("ready", pid=os.getpid(), dimension=model.dimension, version=model.version)
        request = json.loads(sys.stdin.buffer.readline(4097))
        path = Path(request["path"])
        golden = json.loads(GOLDEN.read_bytes())
        assert path.stat().st_size <= 2 * 1024 * 1024
        assert hashlib.sha256(path.read_bytes()).hexdigest() == golden["input"]["sha256"]
        with wave.open(str(path)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (
                1,
                2,
                16000,
                651840,
            )
            pcm = wav.readframes(wav.getnframes())
        assert hashlib.sha256(pcm).hexdigest() == golden["input"]["pcmSHA256"]
        s11 = json.loads((ROOT / "tests/ux/diarization/golden.json").read_bytes())["expected"]
        windows = select_windows(s11["turns"], [dict(w, speaker=w["speakerId"]) for w in s11["words"]], 16000, 651840)
        assert len(voice_ordinals(s11["turns"])) == len(windows) == 2
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2").astype("<f4") / 32768
        output = []
        for print in golden["prints"]:
            parts = windows[print["voiceId"]]
            if "window-" in print["id"]:
                parts = [parts[int(print["id"].rsplit("-", 1)[1]) - 1]]
            value = model.embed(samples[a:b] for a, b in parts)
            assert value is not None
            output.append(dict(value, id=print["id"], windows=parts))
        model.close()
        from cassini_viewer.meeting_store import atomic_json, update_metadata
        from cassini_viewer.nemotron_model import MODEL_TAG as TURNS_MODEL
        from cassini_viewer.worker_diarization import FORMAT, PARAMS
        from cassini_viewer.worker_voices import run

        cache = Path(os.environ.get("XDG_CACHE_HOME", "/out/cache"))
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            directory = Path(temporary) / "Recording-12345678-1234-4234-8234-123456789abc"
            directory.mkdir()
            update_metadata(directory)
            atomic_json(
                directory / "transcript.json",
                dict(format="cassini.words.v1", items=[dict(w, speaker=w["speakerId"]) for w in s11["words"]]),
            )
            atomic_json(
                directory / "speaker-turns.json",
                dict(
                    format=FORMAT,
                    model=TURNS_MODEL,
                    params=PARAMS,
                    groups=[dict(id="others", durationMs=40740, inputSHA256="0" * 64, turns=s11["turns"])],
                ),
            )
            events = []
            run(
                [
                    dict(
                        id="public",
                        path=str(path),
                        kind="system",
                        speaker="others",
                        offsetMs=0,
                        frames=651840,
                        sampleRate=16000,
                    )
                ],
                dict(directory=str(directory), modelPath=model_path, runtimePath=runtime, scratch=str(cache)),
                lambda kind, **value: events.append(dict(type=kind, **value)),
            )
            assert events[-1]["type"] == "voices-complete"
            actual = {e["speaker"]: e for e in events if e["type"] == "voice-print"}
            for value in output:
                if value["id"].endswith("/d25-full"):
                    print = actual[value["id"].split("/")[0]]
                    assert print["embedding"] == value["embedding"]
                    assert print["inputSHA256"] == value["inputSHA256"]
        emit("result", prints=output, nativeMapped="libsherpa-onnx-c-api" in Path("/proc/self/maps").read_text())
    finally:
        model.close()


def parent():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--modules", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.modules))
    from cassini_viewer.parakeet_client import ParakeetClient
    from cassini_viewer.voice_models import VoiceProbe
    from cassini_viewer.voices_logic import REVISION, cosine, match

    assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
    assert {name for _, name in socket.if_nameindex()} == {"lo"}

    class Client(ParakeetClient):
        def worker_command(self):
            return [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                str(args.modules),
                str(self.model),
                str(self.runtime),
            ]

    with VoiceProbe(args.bundle, args.model, args.runtime) as probe:
        assert probe.ready["dimension"] == 512 and probe.ready["pid"] != os.getpid()
    golden = json.loads(GOLDEN.read_bytes())
    args.output.mkdir(parents=True, exist_ok=True)
    reports, hashes = [], []
    for index in range(3):
        started = time.monotonic()
        with Client(args.bundle, args.model, args.runtime) as client:
            result = client.transcribe(args.audio)
            assert result["nativeMapped"] and client.ready["dimension"] == 512
            process = client.process
        assert process.poll() == 0
        prints = {p["id"]: p for p in result["prints"]}
        report = dict(
            inputSHA256=golden["input"]["sha256"],
            modelSourceSHA256=REVISION,
            dimension=512,
            prints=[
                dict(
                    id=p["id"],
                    windows=p["windows"],
                    inputSHA256=p["inputSHA256"],
                    norm=math.sqrt(sum(x * x for x in p["embedding"])),
                )
                for p in result["prints"]
            ],
            comparisons=[
                dict(a=p["a"], b=p["b"], cosine=cosine(prints[p["a"]]["embedding"], prints[p["b"]]["embedding"]))
                for p in golden["comparisons"]
            ],
            matches={},
            workerPid=process.pid,
            startToReapedSeconds=time.monotonic() - started,
            realtimeFactors={key: p["realtimeFactor"] for key, p in prints.items()},
        )
        for case in golden["matches"]:
            people = [
                dict(id=key, name=key, model=REVISION, mean=prints[value]["embedding"])
                for key, value in case["voices"].items()
            ]
            matched = match({key: prints[value]["embedding"] for key, value in case["speakers"].items()}, people)
            report["matches"][case["id"]] = {
                key: dict(voiceId=v["voiceId"], state=v["state"]) for key, v in matched.items()
            }
        path = args.output / f"run-{index + 1}.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        # Verify our report with the unchanged shared checker. It needs no raw
        # embeddings; those stay in this process and the worker's private pipe.
        saved = sys.argv
        try:
            sys.argv = ["verify.py", "--actual", str(path), "--audio", str(args.audio)]
            runpy.run_path(str(ROOT / "tests/ux/voices/verify.py"), run_name="__main__")
        finally:
            sys.argv = saved
        hashes.append(
            {key: hashlib.sha256(struct.pack("<512f", *p["embedding"])).hexdigest() for key, p in prints.items()}
        )
        reports.append(report)
    assert all(h == hashes[0] for h in hashes) and len({r["workerPid"] for r in reports}) == 3
    assert "numpy" not in sys.modules and "cassini_viewer.campplus" not in sys.modules
    assert "libsherpa-onnx-c-api" not in Path("/proc/self/maps").read_text()
    print(
        "PASS S-9: production C API, 3 fresh offline workers, six exact inputs, 15 cosines, AUTO/SUGGESTED, no engine in parent"
    )


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        try:
            child(*sys.argv[2:])
        except Exception as error:
            print(json.dumps(dict(type="error", message=str(error))), flush=True)
            sys.exit(1)
    else:
        parent()
