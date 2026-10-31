#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real pre-rename D42 build defers its re-exec until the recording is saved."""

import atexit
import json
import os
import subprocess
import sys
from pathlib import Path

from live import Audio, Live, until

prefix, build, output = map(Path, sys.argv[1:])
output.mkdir()
os.environ["CASSINI_LIBRARY_DIR"] = str(output / "Library")
old, new = "com.gocassini.Viewer.Devel", "io.codemyriad.CassiniTalk.Devel"
audio = Audio(output)
live = Live(output)
atexit.register(audio.close)
atexit.register(live.close)
app = live.launch(prefix / "bin/cassini-viewer", "d42-legacy-app")
until(lambda: live.owner(old) and live.visible(app), "pre-rename D42 app window")
live.action(old, "record")
folder = until(lambda: live.recording(output / "Library"), "pre-rename app records audio")
subprocess.run(["meson", "install", "-C", str(build)], check=True)
subprocess.run(["glib-compile-schemas", "--strict", str(prefix / "share/glib-2.0/schemas")], check=True)
client = live.launch(prefix / "bin/cassini-talk", "d42-forwarded-client")
until(lambda: client.poll() is not None or live.owner(new), "new launch is forwarded or exposes a writer")
assert not live.owner(new), "The new identity started while the legacy capture still owns the Library"
assert client.wait(timeout=8) == 0
assert live.owner(old) and not live.owner(new), "The new identity must not open the Library during capture"
frames = live.samples(folder)
until(lambda: live.samples(folder) >= frames + 48000, "D42 capture continues while takeover is deferred")
live.action(old, "stop-recording")
until(lambda: live.owner(new) and not live.owner(old), "D42 safely re-execs into the new identity", timeout=35)
assert live.saved(folder)
saved_frames = live.samples(folder)
assert saved_frames >= frames + 48000
until(lambda: live.visible(app), "the re-exec preserves the visible window")
live.action(new, "quit")
assert app.wait(timeout=15) == 0
(output / "handoff.json").write_text(
    json.dumps(
        dict(
            oldId=old,
            newId=new,
            noConcurrentWriter=True,
            captureKept=True,
            frames=saved_frames,
            safeD42Restart=True,
            savedState=json.loads((folder / "session.json").read_text())["state"],
        ),
        indent=2,
    )
    + "\n"
)
print("PASS installed D42 legacy recording → deferred new-ID activation → saved audio → safe restart")
