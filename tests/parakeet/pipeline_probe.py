#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Both D16 goldens through fresh native children in a networkless container."""

import argparse
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("bundle", "model", "runtime", "modules", "output"):
    parser.add_argument("--" + name, required=True, type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from golden_check import assess  # noqa: E402

from cassini_viewer.parakeet_client import ParakeetClient  # noqa: E402
from cassini_viewer.whistle_worker import onnxruntime_dir  # noqa: E402

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
assert {name for _, name in socket.if_nameindex()} == {"lo"}
root = Path(__file__).parents[1] / "whistle/pipeline"
for line in (root / "SHA256SUMS").read_text().splitlines():
    sha, name = line.split()
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == sha
report = dict(network="none", cases=[])
children = []
for filename in ("parakeet-golden.json", "parakeet-pipeline-golden.json"):
    raw = Path(__file__).with_name(filename).read_bytes()
    golden = json.loads(raw)
    assert hashlib.sha256((root / golden["input"]).read_bytes()).hexdigest() == golden["inputSha256"]
    events, maps = [], []
    with ParakeetClient(args.bundle, args.model, args.runtime, timeout=120) as client:
        child = client.process
        children.append(child.pid)

        def progress(event, maps=maps, child=child, events=events):
            if not maps:
                maps.append(Path(f"/proc/{child.pid}/maps").read_text())
            events.append(event)

        completed = client.process_tracks([dict(id="golden", path=str(root / golden["input"]), offsetMs=0)], progress)
        assert completed["vad"]["version"] == "1.31.0", completed["vad"]
        assert completed["vad"]["providers"] == ["CPUExecutionProvider"]
        assert completed["vad"]["intraThreads"] == completed["vad"]["interThreads"] == 1
        assert "libneedle3.so" not in maps[0]
        assert "libsherpa-onnx-c-api.so" in maps[0] and "libonnxruntime.so" in maps[0]
        private_ort = onnxruntime_dir()
        assert private_ort is not None and str(private_ort) in maps[0]
        assert "onnxruntime_pybind11_state" in maps[0]
        progress_events = [e for e in events if e["type"] == "progress"]
        assessment = assess(progress_events, raw, filename)
        result = dict(golden=filename, childPid=child.pid, ready=client.ready, completed=completed, **assessment)
        report["cases"].append(result)
    assert child.poll() == 0, child.returncode
assert len(set(children)) == 2
report["freshChildPerJob"] = report["childrenReaped"] = True
args.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2), flush=True)
assert all(case["accepted"] for case in report["cases"]), "D16 Parakeet golden mismatch"
