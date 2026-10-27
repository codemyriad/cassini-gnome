#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run both immutable cassini-live goldens through the installed child pipeline."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--modules", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
import numpy as np  # noqa: E402

from cassini_viewer.speech_pipeline import resampled_chunks  # noqa: E402
from cassini_viewer.whistle_client import WhistleClient  # noqa: E402

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
assert set(os.listdir("/sys/class/net")) == {"lo"}
root = Path(__file__).with_name("pipeline")
for line in (root / "SHA256SUMS").read_text().splitlines():
    expected, name = line.split()
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
actual = np.concatenate(list(resampled_chunks(root / "pipeline-input-48k.wav")))
expected = np.fromfile(root / "pipeline-resampled-16k.f32", dtype="<f4")
error = float(np.max(np.abs(actual - expected)))
assert len(actual) == len(expected) and error <= 1e-6, error
result = dict(network="none", firMaxError=error, cases=[])
with WhistleClient(args.bundle, timeout=120) as client:
    child = client.process
    for filename in ("whistle-golden.json", "pipeline-golden.json"):
        golden = json.loads((root / filename).read_text())
        events = []
        complete = client.process_tracks(
            [dict(id="golden", path=str(root / golden["input"]), offsetMs=0)], events.append
        )
        result["vad"] = complete["vad"]
        assert complete["vad"]["providers"] == ["CPUExecutionProvider"]
        assert complete["vad"]["intraThreads"] == complete["vad"]["interThreads"] == 1
        progress = [e for e in events if e["type"] == "progress"]
        segments = [e["segment"] for e in progress]
        words = [word for e in progress for word in e["words"]]
        assert segments == golden["segments"], (filename, segments, golden["segments"])
        assert words == golden["words"], (filename, words, golden["words"])
        result["cases"].append(
            dict(
                golden=filename,
                segments=len(segments),
                words=len(words),
                exactSegments=True,
                exactWords=True,
                language=complete["language"],
                childPid=child.pid,
            )
        )
assert child.poll() == 0
result["oneChildReused"] = True
result["childReaped"] = True
args.output.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
