#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A derived live board fixture: silent mic and four separate remote PCM voices.

The shared main fixture/expected outputs are never edited. This private root
represents the drawn Recording condition, using the main fixture's title/notes.
"""

import json
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
source = root / "main/root/Recording-a0000000-0000-4000-8000-000000000003"
target = root / "recording/root" / source.name
target.parent.mkdir(parents=True)
shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.wav"))
manifest = json.loads((target / "session.json").read_text())
first = manifest["tracks"][0]


def silent_copy(origin, destination):
    # Preserve the canonical PCM header/length but keep long silence sparse.
    # This board must not allocate twelve minutes of PCM in RAM or /tmp.
    with origin.open("rb") as original, destination.open("wb") as output:
        output.write(original.read(44))
        output.truncate(origin.stat().st_size)


for track in manifest["tracks"]:
    silent_copy(source / track["file"], target / track["file"])
# Preserve an audible PCM source for each independent remote voice.
for number in range(4):
    track = dict(first, id=f"remote-{number}", file=f"track-remote-{number}.wav", kind="remote")
    remote = target / track["file"]
    silent_copy(source / first["file"], remote)
    # The shared long clip is silent after its opening tone. Give this derived
    # live condition an audible final second for the production meter reader.
    count = min(track["frames"], track["sampleRate"])
    pcm = b"".join(
        (5000 if (i * (440 + number * 110) * 2 // track["sampleRate"]) % 2 else -5000).to_bytes(
            2, "little", signed=True
        )
        * track["channels"]
        for i in range(count)
    )
    with remote.open("r+b") as stream:
        stream.seek(44 + (track["frames"] - count) * track["channels"] * 2)
        stream.write(pcm)
    manifest["tracks"].append(track)
(target / "session.json").write_text(json.dumps(manifest))
(root / "recording/activity.json").write_text(json.dumps({manifest["id"]: "recording"}))
