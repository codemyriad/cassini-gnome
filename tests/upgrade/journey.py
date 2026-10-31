#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Upgrade the actual installed 0.3.0 package; exercise the new app's startup."""

import atexit
import hashlib
import json
import subprocess
import time
from pathlib import Path

from gi.repository import Gio, GLib
from live import Audio, Live, until

OLD = "com.gocassini.Viewer"
NEW = "io.codemyriad.CassiniTalk"
OUT = Path("/out")
home = Path.home()


def installed(name):
    return subprocess.check_output(["dpkg-query", "-W", "-f=${Status} ${Version}", name], text=True)


assert installed("cassini-viewer") == "install ok installed 0.3.0"
assert not Path("/usr/bin/cassini-talk").exists()
old = Gio.Settings.new(OLD)
old.set_int("window-width", 935)
# The fixture has no optional model installed. Selecting a missing Parakeet
# would deliberately trigger the existing model fallback at app startup.
old.set_string("speech-model", "whistle")
old.set_string("record-shortcut-description", "Ctrl+Alt+F9")
old.set_boolean("skip-record-first", True)
old.set_boolean("follow-along", False)
Gio.Settings.sync()
# Private user data must not be moved or rewritten by an identity change.
sentinels = {}
for name in (
    "Music/Cassini/upgrade-sentinel.bin",
    ".local/share/cassini/models/upgrade-sentinel.bin",
    ".local/share/cassini/voices/upgrade-sentinel.bin",
):
    path = home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((name + "\n").encode())
    sentinels[name] = hashlib.sha256(path.read_bytes()).hexdigest()

audio = Audio(OUT)
live = Live(OUT)
atexit.register(audio.close)
atexit.register(live.close)
legacy_app = live.launch("/usr/bin/cassini-viewer", "released-legacy-app")
until(lambda: live.owner(OLD) and live.visible(legacy_app), "released app window")
live.action(OLD, "record")
recording = until(lambda: live.recording(home / "Music/Cassini"), "released app records real private audio")

# Use the end-user package manager: apt orders the transition and handles
# Breaks, whereas raw dpkg refuses to deconfigure the old installed package.
subprocess.run(
    [
        "sudo",
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "/packages/new.deb",
        "/packages/transition.deb",
    ],
    check=True,
)
assert installed("cassini-talk").startswith("install ok installed 0.3.0+")
assert installed("cassini-viewer").startswith("install ok installed 0.3.0+")
assert not Path("/usr/bin/cassini-viewer").is_symlink()
assert Path("/usr/bin/cassini-viewer").read_bytes() == Path("/usr/bin/cassini-talk").read_bytes()
assert not Path(f"/usr/share/applications/{OLD}.desktop").exists()
assert not Path(f"/usr/share/dbus-1/services/{OLD}.service").exists()
desktop = Path(f"/usr/share/applications/{NEW}.desktop").read_text()
assert "Name=Cassini talk\n" in desktop and "Exec=cassini-talk" in desktop

candidate = live.launch("/usr/bin/cassini-talk", "deferred-new-client")
until(lambda: candidate.poll() is not None or live.owner(NEW), "new launch is forwarded or exposes a writer")
assert not live.owner(NEW), "The new identity started while the legacy capture still owns the Library"
assert candidate.wait(timeout=8) == 0
assert live.owner(OLD) and not live.owner(NEW), "Only the legacy process may write during capture"
frames = live.samples(recording)
until(lambda: live.samples(recording) >= frames + 48000, "legacy capture continues after new-ID launch")
live.action(OLD, "stop-recording")
until(lambda: live.saved(recording), "released capture is kept")
live.quit_legacy(OLD, legacy_app)
until(lambda: not live.owner(OLD), "legacy writer releases its bus name")

# A fresh schema source sees the files installed during the same session.
source = Gio.SettingsSchemaSource.new_from_directory("/usr/share/glib-2.0/schemas", None, False)
current = Gio.Settings.new_full(source.lookup(NEW, False), None, None)
assert not current.get_boolean("legacy-settings-migrated")
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def owns():
    return bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (NEW,)),
        None,
        Gio.DBusCallFlags.NONE,
        1000,
        None,
    ).unpack()[0]


with (OUT / "application.log").open("w") as log:
    app = subprocess.Popen(["/usr/bin/cassini-talk"], stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            visible = (
                subprocess.run(
                    ["xdotool", "search", "--all", "--onlyvisible", "--pid", str(app.pid), "--name", "^Cassini talk$"],
                    capture_output=True,
                    timeout=2,
                ).returncode
                == 0
            )
            if owns() and visible and current.get_boolean("legacy-settings-migrated"):
                break
            assert app.poll() is None, "The upgraded app exited during startup"
            time.sleep(0.05)
        else:
            raise AssertionError("The upgraded app did not present its window and migrate settings")
        for key in ("speech-model", "record-shortcut-description", "skip-record-first", "follow-along", "window-width"):
            assert current.get_value(key) == old.get_value(key), key
        for name, digest in sentinels.items():
            assert hashlib.sha256((home / name).read_bytes()).hexdigest() == digest, name
        bus.call_sync(
            NEW,
            "/io/codemyriad/CassiniTalk",
            "org.gtk.Actions",
            "Activate",
            GLib.Variant("(sava{sv})", ("quit", [], {})),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        assert app.wait(timeout=15) == 0
    finally:
        if app.poll() is None:
            app.terminate()
            app.wait(timeout=10)

(OUT / "upgrade.json").write_text(
    json.dumps(
        dict(
            oldPackage="cassini-viewer 0.3.0",
            newPackage=installed("cassini-talk"),
            appId=NEW,
            settingsMigrated=True,
            legacyCommandKept=True,
            unchangedFiles=sentinels,
            activeLegacyCaptureKept=True,
            noConcurrentWriter=True,
        ),
        indent=2,
    )
    + "\n"
)
print("Published 0.3.0 → Cassini talk: installed, settings migrated, user data unchanged")
