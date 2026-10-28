#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Materialise the S-6 fixture as sparse silent PCM; no audio megabytes in Git."""

import json
import struct
import sys
from pathlib import Path

source = Path(__file__).resolve().parent
manifest = json.loads((source / "session.json").read_text())
folder = Path(sys.argv[1]) / ("Recording-" + manifest["id"])
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
(folder / "session.json").write_bytes((source / "session.json").read_bytes())
print(folder)
