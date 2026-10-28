#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run both immutable cassini-live goldens through the installed child pipeline."""

import argparse
import hashlib
import json
import os
import socket
import sys
import wave
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--modules", type=Path, required=True)
args = parser.parse_args()
from golden_check import assess_pipeline  # noqa: E402

sys.path.insert(0, str(args.modules))
import numpy as np  # noqa: E402

from cassini_viewer.speech_pipeline import resampled_chunks  # noqa: E402
from cassini_viewer.whistle_client import WhistleClient  # noqa: E402

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1"
# The interfaces of this process's own network namespace: /sys/class/net can
# show the host's in a Flatpak sandbox started with --unshare=network.
assert {name for _, name in socket.if_nameindex()} == {"lo"}
root = Path(__file__).with_name("pipeline")
for line in (root / "SHA256SUMS").read_text().splitlines():
    expected, name = line.split()
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
actual = np.concatenate(list(resampled_chunks(root / "pipeline-input-48k.wav")))
expected = np.fromfile(root / "pipeline-resampled-16k.f32", dtype="<f4")
error = float(np.max(np.abs(actual - expected)))
assert len(actual) == len(expected) and error <= 1e-6, error
cpu = {}
for line in Path("/proc/cpuinfo").read_text().splitlines():
    if ":" in line:
        key, value = line.split(":", 1)
        if key.strip() in ("model name", "vendor_id", "flags") and key.strip() not in cpu:
            cpu[key.strip()] = value.strip()
result = dict(network="none", firMaxError=error, cpu=cpu, timingPolicy="D11.3", timingToleranceMs=80, cases=[])
failures = []
with WhistleClient(args.bundle, timeout=120) as client:
    child = client.process
    for filename in ("whistle-golden.json", "pipeline-golden.json"):
        golden_bytes = (root / filename).read_bytes()
        golden = json.loads(golden_bytes)
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
        expected_words = golden["words"]
        differences = [
            dict(index=i, actual=a, expected=b)
            for i, (a, b) in enumerate(zip(words, expected_words, strict=False))
            if a != b
        ]
        segment_hashes = [e["inputSha256"] for e in progress]
        if filename == "pipeline-golden.json":
            reference = np.fromfile(root / "pipeline-resampled-16k.f32", dtype="<f4")
        else:
            with wave.open(str(root / golden["input"])) as audio:
                assert audio.getnchannels() == 1 and audio.getframerate() == 16000 and audio.getsampwidth() == 2
                reference = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype("<f4") / np.float32(
                    32768
                )
        expected_hashes = [
            hashlib.sha256(reference[segment["startSample"] : segment["endSample"]].tobytes()).hexdigest()
            for segment in golden["segments"]
        ]
        pins = json.loads((args.bundle / "pins.json").read_text())
        assessment = assess_pipeline(
            words,
            segments,
            golden_bytes,
            filename,
            segment_hashes,
            expected_hashes,
            dict(
                version=pins["engineVersion"],
                model=pins["installed"]["whistle.cact"],
                library=pins["installed"]["libneedle3.so"],
            ),
        )
        if not assessment["accepted"]:
            failures.append(filename)
        case_data = dict(
            differences=differences,
            actualWords=words,
            expectedWords=expected_words,
            segmentInputSha256=segment_hashes,
            referenceSegmentInputSha256=expected_hashes,
            **assessment,
        )
        result["cases"].append(
            dict(
                golden=filename,
                segments=len(segments),
                words=len(words),
                **case_data,
                language=complete["language"],
                childPid=child.pid,
            )
        )
assert child.poll() == 0
result["oneChildReused"] = True
result["childReaped"] = True
args.output.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
if failures:
    # Installed-package smoke checks may truncate stdout. Keep the actual text
    # mismatch and CPU on stderr as well, without relaxing the shared contract.
    print(
        json.dumps(
            dict(
                cpu=cpu,
                failures=[
                    dict(
                        golden=case["golden"],
                        exactSegments=case["exactSegments"],
                        wordCountMatches=case["wordCountMatches"],
                        segmentInputHashesMatch=case["segmentInputHashesMatch"],
                        maxBoundaryDeltaMs=case["maxBoundaryDeltaMs"],
                        textDifferences=[
                            d for d in case["differences"] if d["actual"]["text"] != d["expected"]["text"]
                        ],
                    )
                    for case in result["cases"]
                    if case["golden"] in failures
                ],
            ),
            indent=2,
        ),
        file=sys.stderr,
    )
assert not failures, "D11.3 golden mismatch: " + str(failures)
