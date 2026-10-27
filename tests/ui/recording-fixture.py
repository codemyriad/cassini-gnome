#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A derived live board fixture: silent mic and four separate remote PCM voices.

The shared main fixture/expected outputs are never edited. This private root
represents the drawn Recording condition, using the main fixture's title/notes.
"""

import json
import shutil
import sys
import wave
from pathlib import Path

root = Path(sys.argv[1])
source = root / "main/root/Recording-a0000000-0000-4000-8000-000000000003"
target = root / "recording/root" / source.name
target.parent.mkdir(parents=True)
shutil.copytree(source, target)
manifest = json.loads((target / "session.json").read_text())
first = manifest["tracks"][0]
# Preserve an audible PCM source for each independent remote voice.
for number in range(4):
    track = dict(first, id=f"remote-{number}", file=f"track-remote-{number}.wav", kind="remote")
    shutil.copyfile(target / first["file"], target / track["file"])
    manifest["tracks"].append(track)
with wave.open(str(target / first["file"]), "wb") as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(first["sampleRate"])
    audio.writeframes(bytes(first["frames"] * 2))
(target / "session.json").write_text(json.dumps(manifest))
(root / "recording/activity.json").write_text(json.dumps({manifest["id"]: "recording"}))
