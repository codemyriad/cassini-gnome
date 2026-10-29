#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D26.1 coordinator vectors, asserts a-e, through installed Silero and gate."""

import argparse
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--modules", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from cassini_viewer.whistle_worker import PRIVACY, onnxruntime_dir, verify_onnxruntime  # noqa: E402

os.environ.update(PRIVACY)
private = onnxruntime_dir()
if private is not None:
    sys.path.insert(0, str(verify_onnxruntime(private)))
import numpy as np  # noqa: E402

from cassini_viewer.speech_pipeline import SpeechDetector, segment_ranges  # noqa: E402

assert {name for _, name in socket.if_nameindex()} == {"lo"}
root = Path(__file__).resolve().parent
EXPECTED = [
    [105984, 189440, "silence"],
    [347648, 367616, "silence"],
    [371200, 413184, "silence"],
    [416256, 458240, "silence"],
    [504320, 544256, "silence"],
    [547328, 589312, "silence"],
]
FILES = {
    "dc-quiet-jfk-v1.wav": (
        "071f051241d394587c85463272e9414f133ee18c4bb835d1382b662e4154efda",
        "eeaeee543bc328acf6f0c4557e8aba86652e0144d9763264e1e5d62a3d13f968",
    ),
    "dc-quiet-jfk-v1-control.wav": (
        "e31ed958621a39baf1d33dab6d10798695b8319e78fcaa1cb672cdb43c5f7830",
        "6ece4c5cacfc2456cb79d0aa1b16f4aa3912fef6697912e633886d7b0da545ab",
    ),
}
results = []
for name, enabled in [("dc-quiet-jfk-v1.wav", False), *((name, True) for name in FILES)]:
    path = root / name
    before = path.read_bytes()
    assert hashlib.sha256(before).hexdigest() == FILES[name][0]
    detector = SpeechDetector(args.bundle / "silero_vad.onnx")
    underlying = detector.session
    digest, first, count, probabilities = hashlib.sha256(), [], [0], []

    class Trace:
        def run(
            self,
            outputs,
            inputs,
            *,
            first=first,
            digest=digest,
            count=count,
            probabilities=probabilities,
            underlying=underlying,
        ):
            frame = inputs["input"][0, 64:]
            if not first:
                first.extend(frame[:3].tolist())
            digest.update(frame.astype("<f4").tobytes())
            count[0] += len(frame)
            result = underlying.run(outputs, inputs)
            probabilities.append(float(result[0][0, 0]))
            return result

    detector.session = Trace()
    if not enabled:
        # Characterise the previous unfixed input path with the same model/gate.
        def raw_probability(frame, detector=detector):
            samples = np.concatenate((detector.context, frame.reshape(1, -1)), axis=1)
            output, detector.state = detector.session.run(
                None, dict(input=samples, state=detector.state, sr=np.array(16000, dtype=np.int64))
            )
            detector.context = samples[:, -64:].copy()
            return float(output[0, 0])

        detector.probability = raw_probability
    segments = segment_ranges(path, detector)
    actual = [[s["startSample"], s["endSample"], s["reason"]] for s in segments]
    maximum = max(probabilities)
    if enabled:
        assert actual == EXPECTED, (name, actual)
        assert count[0] == 604672 and digest.hexdigest() == FILES[name][1], (count, digest.hexdigest())
        if name == "dc-quiet-jfk-v1.wav":
            assert first == [-0.039744045585393906, -0.06441963464021683, -0.05248776078224182]
            assert abs(sum(p >= 0.5 for p in probabilities) - 251) <= 2
            assert abs(maximum - 0.718) <= 0.01
    else:
        assert actual == [] and maximum < 0.1
    assert path.read_bytes() == before
    results.append(
        dict(file=name, filter=enabled, segments=actual, inputSHA256=digest.hexdigest(), maxProbability=maximum)
    )
args.output.mkdir(parents=True, exist_ok=True)
(args.output / "report.json").write_text(
    json.dumps(dict(stage=SpeechDetector.input_stage, cases=results), indent=2) + "\n"
)
print("D26.1 WebRTC/Silero: shared asserts a-e pass; recogniser PCM and WAVs untouched")
