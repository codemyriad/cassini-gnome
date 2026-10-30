#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed same-version patches: idle restart, capture deferral and host repair."""

import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from fixture import package, patch_tree  # noqa: E402

assert os.environ.get("CASSINI_QA_UPGRADE") == "1" and not os.environ.get("WAYLAND_DISPLAY")
APP, DATA = map(Path, sys.argv[1:3])
OUT = Path("/out")
initial = json.loads((DATA / "build-info.json").read_text())
APPID = initial["applicationId"]
OBJECT = "/" + APPID.replace(".", "/")
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
log_path = OUT / "application.log"
log = log_path.open("a", buffering=1)
activation_log = (OUT / "activations.log").open("a", buffering=1)
report = dict(initial=initial["buildId"], version=initial["version"], fixturePackagesAreTestOnly=True)


def until(check, label, timeout=20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        try:
            value = check()
            if value:
                return value
        except (GLib.Error, OSError) as error:
            last = str(error)
        time.sleep(0.02)
    raise AssertionError(f"Timed out: {label}; last error={last}")


def owner():
    return bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (APPID,)),
        None,
        Gio.DBusCallFlags.NONE,
        1000,
        None,
    ).unpack()[0]


def action(name):
    bus.call_sync(
        APPID,
        OBJECT,
        "org.gtk.Actions",
        "Activate",
        GLib.Variant("(sava{sv})", (name, [], {})),
        None,
        Gio.DBusCallFlags.NONE,
        2000,
        None,
    )


def starts():
    return re.findall(r"build=([a-f0-9]{64}) start=([a-f0-9]{32}) pid=(\d+)", log_path.read_text())


def image(build_id):
    return next((value for value in starts() if value[0] == build_id), None)


def activate():
    subprocess.run([str(APP)], stdout=activation_log, stderr=activation_log, check=True, timeout=10)


def window_ids():
    events = starts()
    if not events:
        return []
    result = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--pid", events[-1][2]], capture_output=True, text=True
    )
    return result.stdout.split() if result.returncode == 0 else []


def close_window():
    window = until(lambda: next(iter(window_ids()), None), "mapped Library")
    subprocess.run(["xdotool", "windowfocus", "--sync", window], check=True, timeout=3)
    subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+w"], check=True, timeout=3)
    until(lambda: not window_ids(), "last window closed")


def walk(node):
    yield node
    for index in range(node.get_child_count()):
        child = node.get_child_at_index(index)
        if child:
            yield from walk(child)


def nodes(name, role=None):
    return [
        node
        for node in walk(Atspi.get_desktop(0))
        if (node.get_name() or "") == name
        and (not role or node.get_role_name() == role)
        and node.get_state_set().contains(Atspi.StateType.SHOWING)
    ]


def switch_on():
    action("preferences")

    def ready():
        candidates = []
        for node in nodes("Record every browser meeting automatically", "switch"):
            if not node.get_state_set().contains(Atspi.StateType.SENSITIVE):
                continue
            rectangle = node.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
            if rectangle.width > 0 and rectangle.height > 0:
                candidates.append((rectangle.width, node, rectangle))
        return min(candidates, key=lambda item: item[0]) if candidates else None

    _, switch, rectangle = until(ready, "real automatic-recording switch")
    print(
        "Automatic switch",
        rectangle.x,
        rectangle.y,
        rectangle.width,
        rectangle.height,
        "actions",
        switch.get_action_iface().get_n_actions(),
        flush=True,
    )
    if not switch.get_state_set().contains(Atspi.StateType.CHECKED):
        # Activate the actual inner GtkSwitch, not an Adw row's ancestor action.
        assert switch.get_action_iface().do_action(0), "GtkSwitch accessibility action refused"
    until(
        lambda: any(
            n.get_state_set().contains(Atspi.StateType.CHECKED)
            and n.get_state_set().contains(Atspi.StateType.SENSITIVE)
            for n in nodes("Record every browser meeting automatically", "switch")
        ),
        "automatic recording accepted",
    )

    def close_preferences():
        for node in nodes("Close", "button"):
            parent = node.get_parent()
            while parent and not (parent.get_name() == "Preferences" and parent.get_role_name() == "dialog"):
                parent = parent.get_parent()
            if parent is not None:
                return node.get_action_iface().do_action(0)
        return False

    until(close_preferences, "Preferences close button")
    until(lambda: not nodes("Record every browser meeting automatically", "switch"), "Preferences dismissed")


def install(marker, number):
    original = Path("/original.deb")
    if original.exists():
        fixture = OUT / f"upgrade-{number}.deb"
        value = package(original, fixture, marker)
        with (OUT / f"install-{number}.log").open("w") as output:
            subprocess.run(
                ["sudo", "-n", "dpkg", "-i", str(fixture)],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=60,
            )
    else:
        value = patch_tree(DATA, marker)
    assert value["version"] == initial["version"], "The test must cover same-version upgrades"
    return value


def microphone_ready(root):
    for path in root.glob("Recording-*/session.json"):
        value = json.loads(path.read_text())
        if value.get("state") == "recording" and any(t["frames"] >= 9600 for t in value["tracks"]):
            return path
    return None


def snapshot(name):
    subprocess.run(["import", "-window", "root", str(OUT / name)], check=True, timeout=3)


try:
    # The only audio inputs/outputs are this private null sink and its remap.
    subprocess.run(
        ["pulseaudio", "--daemonize=yes", "--exit-idle-time=-1", "--log-target=file:/out/pulse.log"], check=True
    )
    for arguments in (
        ["load-module", "module-null-sink", "sink_name=voice"],
        ["load-module", "module-null-sink", "sink_name=speakers"],
        ["load-module", "module-remap-source", "master=voice.monitor", "source_name=mic"],
        ["set-default-source", "mic"],
        ["set-default-sink", "speakers"],
    ):
        subprocess.run(["pactl", *arguments], check=True, capture_output=True)
    tone = OUT / "tone.wav"
    with wave.open(str(tone), "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(
            b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(48000))
        )
    subprocess.Popen(
        ["sh", "-c", 'while true; do paplay --device=voice "$1"; done', "cassini-private-tone", str(tone)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["gsettings", "set", "com.gocassini.Viewer", "skip-record-first", "true"], check=True)
    process = subprocess.Popen([str(APP)], stdout=log, stderr=log)
    until(lambda: owner() and image(initial["buildId"]) and window_ids(), "initial installed app")
    switch_on()
    close_window()
    assert owner(), "Automatic recording lost the windowless app"
    first = install("N1", 1)
    assert owner() and not image(first["buildId"]), "Installing alone must not rewrite the running identity"
    activate()
    next_image = until(
        lambda: image(first["buildId"]) and window_ids() and image(first["buildId"]), "new code after idle activation"
    )
    assert next_image[1] != image(initial["buildId"])[1]
    assert "CASSINI_TEST_IMAGE=N1" in log_path.read_text()
    report["idle"] = dict(build=first["buildId"], freshPython=True, windowReopened=True, automaticWasArmed=True)

    action("record")
    root = Path.home() / "Music/Cassini"
    manifest = until(lambda: microphone_ready(root), "actual microphone frames")
    second = install("N2", 2)
    activate()
    until(lambda: nodes("Cassini was updated. Restart it to use the new version."), "restart banner during capture")
    restart = until(lambda: next(iter(nodes("Restart", "button")), None), "Restart action")
    assert not restart.get_state_set().contains(Atspi.StateType.SENSITIVE)
    assert not image(second["buildId"]) and "CASSINI_TEST_IMAGE=N2" not in log_path.read_text()
    assert json.loads(manifest.read_text())["state"] == "recording"
    snapshot("restart-recording.png")
    action("stop-recording")
    until(lambda: image(second["buildId"]) and window_ids(), "restart after finalized Stop")
    assert "CASSINI_TEST_IMAGE=N2" in log_path.read_text()
    saved = json.loads(manifest.read_text())
    assert saved["state"] == "completed"
    report["savedTracks"] = []
    (OUT / "saved-session.json").write_text(json.dumps(saved, indent=2) + "\n")
    for track in saved["tracks"]:
        with wave.open(str(manifest.parent / track["file"]), "rb") as wav:
            assert wav.getnframes() == track["frames"] > 0
            report["savedTracks"].append(
                dict(kind=track["kind"], manifestFrames=track["frames"], wavFrames=wav.getnframes())
            )
    report["recording"] = dict(build=second["buildId"], deferred=True, restartDisabled=True, finalizedAudioKept=True)

    # Leave an executable relay behind but no installed Flatpak deployment.
    native = Path(os.environ["XDG_CONFIG_HOME"]) / "google-chrome/NativeMessagingHosts"
    paths = [native / (name + ".json") for name in ("io.codemyriad.cassini", "io.codemyriad.cassini.control")]
    until(lambda: all(p.exists() for p in paths), "real background registration")
    old = json.loads(paths[1].read_text())
    relay = Path.home() / ".local/share/cassini/native-hosts/com.gocassini.Viewer.Devel/cassini-browser-control-host"
    relay.parent.mkdir(parents=True)
    relay.write_text(
        "#!/bin/sh\nexec /usr/bin/flatpak run --arch=x86_64 --branch=master --command=cassini-browser-control-host com.gocassini.Viewer.Devel\n"
    )
    relay.chmod(0o700)
    paths[1].write_text(json.dumps(dict(old, path=str(relay))))
    until(lambda: json.loads(paths[1].read_text()) == old, "dead Flatpak relay repaired by status polling")
    report["deadRelayRepairedWithoutClick"] = True

    # Build a genuine source/Devel profile at another prefix; it must not take
    # ownership from the healthy release without an explicit setup request.
    source = Path("/source")
    devel = OUT / "devel"
    subprocess.run(
        ["meson", "setup", str(OUT / "devel-build"), str(source), "--prefix=" + str(devel), "-Dprofile=development"],
        stdout=activation_log,
        stderr=activation_log,
        check=True,
        timeout=30,
    )
    subprocess.run(
        ["meson", "install", "-C", str(OUT / "devel-build")],
        stdout=activation_log,
        stderr=activation_log,
        check=True,
        timeout=30,
    )
    before = {p: p.read_bytes() for p in paths}
    program = "import sys;sys.path.insert(0,sys.argv[1]);from cassini_viewer.companion import register;register()"
    subprocess.run(
        ["python3", "-I", "-c", program, str(devel / "share/cassini-viewer")],
        check=True,
        stdout=activation_log,
        stderr=activation_log,
        timeout=10,
    )
    assert before == {p: p.read_bytes() for p in paths}
    subprocess.run(
        [
            "python3",
            "-I",
            "-c",
            program.replace("register()", "register(force=True)"),
            str(devel / "share/cassini-viewer"),
        ],
        check=True,
        stdout=activation_log,
        stderr=activation_log,
        timeout=10,
    )
    assert all(json.loads(p.read_text())["path"].startswith(str(devel)) for p in paths)
    report["develPreservesRelease"] = True
    report["explicitSetupCanOwnRegistration"] = True
    assert not re.search(r"Gtk-(WARNING|CRITICAL)", log_path.read_text())
    (OUT / "upgrade.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
except Exception:
    snapshot("failure.png")
    visible = []
    for node in walk(Atspi.get_desktop(0)):
        if node.get_state_set().contains(Atspi.StateType.SHOWING):
            visible.append(
                dict(
                    name=(node.get_name() or "")[:200],
                    role=node.get_role_name(),
                    states=list(node.get_state_set().get_states()),
                )
            )
    (OUT / "failure-a11y.json").write_text(json.dumps(visible, indent=2))
    raise
finally:
    if owner():
        action("quit")
        until(lambda: not owner(), "clean final quit", timeout=5)
