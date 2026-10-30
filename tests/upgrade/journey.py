#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Upgrade the actual installed 0.3.0 package; exercise the new app's startup."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

from gi.repository import Gio, GLib

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
old.set_string("speech-model", "parakeet-ultra-0.6b-int8")
old.set_string("export-folder-uri", (home / "Exports").as_uri())
old.set_boolean("skip-record-first", True)
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

subprocess.run(["sudo", "dpkg", "-i", "/packages/new.deb", "/packages/transition.deb"], check=True)
assert installed("cassini-talk").startswith("install ok installed 0.3.0+")
assert installed("cassini-viewer").startswith("install ok installed 0.3.0+")
assert not Path("/usr/bin/cassini-viewer").is_symlink()
assert Path("/usr/bin/cassini-viewer").read_bytes() == Path("/usr/bin/cassini-talk").read_bytes()
assert not Path(f"/usr/share/applications/{OLD}.desktop").exists()
assert not Path(f"/usr/share/dbus-1/services/{OLD}.service").exists()
desktop = Path(f"/usr/share/applications/{NEW}.desktop").read_text()
assert "Name=Cassini talk\n" in desktop and "Exec=cassini-talk" in desktop

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
            if owns() and current.get_boolean("legacy-settings-migrated"):
                break
            assert app.poll() is None, "The upgraded app exited during startup"
            time.sleep(0.05)
        else:
            raise AssertionError("The upgraded app did not migrate settings on startup")
        for key in ("speech-model", "export-folder-uri", "skip-record-first"):
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
        ),
        indent=2,
    )
    + "\n"
)
print("Published 0.3.0 → Cassini talk: installed, settings migrated, user data unchanged")
