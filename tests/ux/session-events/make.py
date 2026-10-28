#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Materialise the S-6 fixture as sparse silent PCM; no audio megabytes in Git."""

import argparse
import json
import struct
from pathlib import Path

source = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("library", type=Path)
parser.add_argument("--case", choices=("fallback", "browser-off-on"), default="fallback")
args = parser.parse_args()
filename = "session.json" if args.case == "fallback" else "browser-off-on.session.json"
manifest = json.loads((source / filename).read_text())
folder = args.library / ("Recording-" + manifest["id"])
folder.mkdir(mode=0o700, parents=True)
for track in manifest["tracks"]:
    rate, channels = track["sampleRate"], track["channels"]
    size = track["frames"] * channels * 2
    with (folder / track["file"]).open("xb") as audio:
        audio.write(
            struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF",
                size + 36,
                b"WAVE",
                b"fmt ",
                16,
                1,
                channels,
                rate,
                rate * channels * 2,
                channels * 2,
                16,
                b"data",
                size,
            )
        )
        audio.truncate(44 + size)
(folder / "session.json").write_bytes((source / filename).read_bytes())
print(folder)
