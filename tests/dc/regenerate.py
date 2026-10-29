#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate D26.1 shared goldens using pinned, network-isolated workers.

Writes proposals to --output. Adoption and digest updates are separate reviewable
steps. Existing D11.5's hash-bound CPU alternative is kept canonical, unchanged.
"""

import argparse
import hashlib
import json
import socket
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
for name in ("bundle", "modules", "runtime", "parakeet-model", "output"):
    parser.add_argument("--" + name, required=True, type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from cassini_viewer.parakeet_client import ParakeetClient  # noqa: E402
from cassini_viewer.whistle_client import WhistleClient  # noqa: E402

assert {name for _, name in socket.if_nameindex()} == {"lo"}
root = Path(__file__).resolve().parents[2]
inputs = root / "tests/whistle/pipeline"
args.output.mkdir(parents=True, exist_ok=True)
policy = json.loads((root / "tests/whistle/whistle-text-exceptions.json").read_text())
reports = []
for engine, paths in [
    ("whistle", [inputs / "whistle-golden.json", inputs / "pipeline-golden.json"]),
    ("parakeet", [root / "tests/parakeet/parakeet-golden.json", root / "tests/parakeet/parakeet-pipeline-golden.json"]),
]:
    for path in paths:
        original = path.read_bytes()
        golden = json.loads(original)
        events = []
        client = (
            WhistleClient(args.bundle, timeout=180)
            if engine == "whistle"
            else ParakeetClient(args.bundle, args.parakeet_model, args.runtime, timeout=180)
        )
        with client:
            summary = client.process_tracks(
                [dict(id="golden", path=str(inputs / golden["input"]), offsetMs=0)], events.append
            )
        progress = [event for event in events if event["type"] == "progress"]
        assert len(progress) == len(golden["segments"]) == 8
        old_segments = [dict(s) for s in golden["segments"]]
        segments = [dict(event["segment"]) for event in progress]
        hashes = [event["inputSha256"] for event in progress]
        assert segments[0]["startSample"] == 13824
        for index, (old, new) in enumerate(zip(old_segments, segments, strict=True)):
            assert new["endSample"] == old["endSample"] and new["reason"] == old["reason"]
            assert new["startSample"] == (13824 if index == 0 else old["startSample"])
            if engine == "parakeet":
                new["inputSha256"] = hashes[index]
                if index:
                    assert hashes[index] == old["inputSha256"]
        words = [dict(word) for event in progress for word in event["words"]]
        normalized = []
        if engine == "whistle":
            for rule in policy["exceptions"]:
                if path.name == rule["golden"] and hashlib.sha256(original).hexdigest() == rule["goldenSha256"]:
                    i, w = rule["segmentIndex"], rule["wordIndex"]
                    assert hashes[i] == rule["segmentInputSha256"]
                    assert words[w]["text"] in rule["allowedWords"]
                    assert segments[i]["text"] in (rule["expectedSegmentText"], rule["actualSegmentText"])
                    if words[w]["text"] != rule["expectedWord"]:
                        normalized.append(dict(word=w, actual=words[w]["text"], expected=rule["expectedWord"]))
                        words[w]["text"] = rule["expectedWord"]
                        segments[i]["text"] = rule["expectedSegmentText"]
        golden["segments"], golden["words"] = segments, words
        golden["vadInputStage"] = summary["vad"]["inputStage"]
        golden["note"] += " D26.1: silero-input-hpf-butter2-100hz-v1 filters VAD only; recognition PCM is unchanged."
        destination = args.output / path.name
        destination.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + "\n")
        (args.output / (path.stem + "-events.json")).write_text(json.dumps(events, indent=2) + "\n")
        reports.append(
            dict(
                file=str(path.relative_to(root)),
                oldSHA256=hashlib.sha256(original).hexdigest(),
                newSHA256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                normalized=normalized,
                segmentInputSHA256=hashes,
                words=len(words),
                summary=summary,
            )
        )
(args.output / "report.json").write_text(json.dumps(reports, indent=2) + "\n")
print(json.dumps(reports, indent=2))
