#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real JFK recognition in a networkless container, with repeated child requests."""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--modules", type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.modules))
from cassini_viewer.whistle_client import WhistleClient  # noqa: E402
from cassini_viewer.whistle_worker import PRIVACY  # noqa: E402

assert Path(sys.modules["cassini_viewer.whistle_client"].__file__).is_relative_to(args.modules)

assert os.environ.get("CASSINI_WHISTLE_NETWORK_NONE") == "1", "Use the networkless container harness"
assert set(os.listdir("/sys/class/net")) == {"lo"}, "Container must have only loopback"
fixture = Path(__file__).with_name("jfk.wav")
assert (
    hashlib.sha256(fixture.read_bytes()).hexdigest()
    == "59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e"
)
reference = Path(__file__).with_name("jfk.txt").read_text().strip()
mac = json.loads(Path(__file__).with_name("mac-golden.json").read_text())
parent = os.getpid()
with WhistleClient(args.bundle) as client:
    child = client.process
    one, two = client.transcribe(fixture), client.transcribe(fixture)
    assert one["pid"] == two["pid"] == child.pid != parent
    assert one["text"] == two["text"] == mac["text"], (one, two, mac)
    assert re.findall(r"\w+", one["text"].lower()) == re.findall(r"\w+", reference.lower())
    assert 1 <= client.ready["threads"] <= 3, client.ready
    result = dict(
        platform="linux-x86_64",
        engineVersion="3.3.0",
        text=one["text"],
        reference=reference,
        exactReferenceMatch=one["text"] == reference,
        wordReferenceMatch=True,
        exactMacMatch=True,
        twoRequestsIdentical=True,
        childPid=child.pid,
        parentPid=parent,
        threads=client.ready["threads"],
        constructorPrivacyGuard=True,
        telemetry=PRIVACY,
        network="none",
        seconds=[one["seconds"], two["seconds"]],
        bundleManifest=json.loads((args.bundle / "manifest.json").read_text()),
    )
assert child.poll() == 0
result["childReaped"] = True
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
