#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D41 installed-package journeys on a fresh private GNOME 50 desktop.

Host SetStatus rejection is reported as an explicit platform limitation; it is
never substituted with a fake Background Apps entry or a tray implementation.
"""

import json
import math
import os
import socket
import subprocess
import time
import wave
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

assert os.environ.get("CASSINI_QA_DESKTOP") == "1" and not os.environ.get("DISPLAY")
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
OUT = Path("/out")
HOME = Path.home()


def until(check, label, timeout=30):
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
            last = error
        time.sleep(0.05)
    raise AssertionError(f"Timed out: {label}; last error={last}")


def call(dest, path, iface, method, params=None):
    return bus.call_sync(dest, path, iface, method, params, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()


def shell(code):
    ok, value = call("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval", GLib.Variant("(s)", (code,)))
    assert ok, value
    return json.loads(value) if value else None


def windows(appid):
    return shell(
        f"imports.gi.Shell.AppSystem.get_default().lookup_app({json.dumps(appid + '.desktop')})?.get_windows().length ?? 0"
    )


def owner(appid):
    return call(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (appid,)),
    )[0]


def pid(appid):
    return call(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetConnectionUnixProcessID",
        GLib.Variant("(s)", (appid,)),
    )[0]


def action(appid, name):
    return call(
        appid, "/" + appid.replace(".", "/"), "org.gtk.Actions", "Activate", GLib.Variant("(sava{sv})", (name, [], {}))
    )


def close_window(appid):
    shell(
        f"imports.gi.Shell.AppSystem.get_default().lookup_app({json.dumps(appid + '.desktop')}).get_windows()[0].delete(global.get_current_time()); true"
    )


def walk(node):
    yield node
    for index in range(node.get_child_count()):
        child = node.get_child_at_index(index)
        if child:
            yield from walk(child)


def click(label):
    def find():
        for node in walk(Atspi.get_desktop(0)):
            if (node.get_name() or "") != label:
                continue
            if not node.get_state_set().contains(Atspi.StateType.SHOWING):
                continue
            target = node
            while target:
                iface = target.get_action_iface()
                if iface and iface.get_n_actions():
                    assert iface.do_action(0)
                    return True
                target = target.get_parent()
        return False

    until(find, "click " + label)


def listed(appid):
    rows = call(
        "org.freedesktop.background.Monitor",
        "/org/freedesktop/background/monitor",
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", ("org.freedesktop.background.Monitor", "BackgroundApps")),
    )[0]
    return next((row for row in rows if row.get("app_id") == appid), None)


def menu_item(appid, operation):
    # Exercise GNOME's real BackgroundAppMenuItem, including the close button
    # that dispatches org.gtk.Actions.Activate('quit'), not our app action helper.
    return shell(
        "(() => {const t=Main.panel.statusArea.quickSettings._backgroundApps.quickSettingsItems[0];"
        "const item=t._appsSection._getMenuItems().find(i=>i.app?.get_id()===" + json.dumps(appid + ".desktop") + ");"
        "if(!item)return false;" + operation + ";return true;})()"
    )


def launch(appid):
    app = Gio.DesktopAppInfo.new(appid + ".desktop")
    assert app and app.launch([], None), appid
    until(lambda: owner(appid) and windows(appid), "app-grid activation " + appid)


def broker_configure(appid):
    folder = Path(os.environ["XDG_RUNTIME_DIR"])
    if appid.endswith(".Devel"):
        folder = folder / "app" / appid
    path = folder / "cassini/browser-v2.sock"
    until(path.exists, "real broker socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        sock.sendall(b'{"role":"app","action":"configure","autoRecord":true}\n')
        with sock.makefile("rb") as stream:
            response = json.loads(stream.readline())
        assert not response.get("error") and response.get("autoRecord") is True, response
    # Verify the live app has observed the real preference before closing it.
    action(appid, "preferences")

    def armed():
        for node in walk(Atspi.get_desktop(0)):
            if (node.get_name() or "") == "Record every browser meeting automatically":
                return node.get_state_set().contains(Atspi.StateType.CHECKED)
        return False

    until(armed, "automatic-recording preference reflected by the running app")
    # Close Preferences alone, keeping the Library.
    shell(
        "(() => {const C=imports.gi.Clutter; global._cassiniQAKeyboard ??= C.get_default_backend().get_default_seat().create_virtual_device(C.InputDeviceType.KEYBOARD_DEVICE); const d=global._cassiniQAKeyboard; d.notify_keyval(global.get_current_time()*1000,C.KEY_Escape,C.KeyState.PRESSED); d.notify_keyval(global.get_current_time()*1000+5000,C.KEY_Escape,C.KeyState.RELEASED); return true;})()"
    )

    def dismissed():
        return not any(
            (n.get_name() or "") == "Record every browser meeting automatically"
            and n.get_state_set().contains(Atspi.StateType.SHOWING)
            for n in walk(Atspi.get_desktop(0))
        )

    until(dismissed, "Preferences dismissed")


def recording(root):
    for manifest in sorted(root.glob("Recording-*/session.json")):
        value = json.loads(manifest.read_text())
        if any(track.get("frames", 0) > 0 for track in value.get("tracks", [])) and value.get("state") == "recording":
            return manifest
    return None


until(lambda: (Path(os.environ["XDG_RUNTIME_DIR"]) / "wayland-0").exists(), "private Wayland socket")
os.environ["WAYLAND_DISPLAY"] = "wayland-0"
until(lambda: shell("Main.overview.hide(); true"), "GNOME Shell ready")
version = subprocess.check_output(["gnome-shell", "--version"], text=True).strip()
assert version.startswith("GNOME Shell 50"), version
subprocess.Popen(["pipewire"], stdout=(OUT / "pipewire.log").open("w"), stderr=subprocess.STDOUT)
until(lambda: (Path(os.environ["XDG_RUNTIME_DIR"]) / "pipewire-0").exists(), "PipeWire")
subprocess.Popen(["wireplumber"], stdout=(OUT / "wireplumber.log").open("w"), stderr=subprocess.STDOUT)
subprocess.Popen(["pipewire-pulse"], stdout=(OUT / "pulse.log").open("w"), stderr=subprocess.STDOUT)
until(lambda: subprocess.run(["pactl", "info"], capture_output=True).returncode == 0, "private Pulse server")
for args in (
    ["load-module", "module-null-sink", "sink_name=voice"],
    ["load-module", "module-null-sink", "sink_name=speakers"],
    ["load-module", "module-remap-source", "master=voice.monitor", "source_name=mic"],
    ["set-default-source", "mic"],
    ["set-default-sink", "speakers"],
):
    subprocess.run(["pactl", *args], check=True, capture_output=True)
tone = OUT / "tone.wav"
with wave.open(str(tone), "wb") as wav:
    wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
    import struct

    wav.writeframes(
        b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(48000))
    )
subprocess.Popen(
    ["sh", "-c", 'while true; do paplay --device=voice "$1"; done', "cassini-qa-tone", str(tone)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

versions = subprocess.check_output(
    ["dpkg-query", "-W", "xdg-desktop-portal", "xdg-desktop-portal-gnome", "flatpak"], text=True
)
results = {"shell": version, "packages": versions, "cases": []}
root = HOME / "Music/Cassini"
for appid in ("com.gocassini.Viewer", "com.gocassini.Viewer.Devel"):
    flatpak = appid.endswith(".Devel")
    prefix = ["flatpak", "run", "--user", "--command=gsettings", appid] if flatpak else []
    subprocess.run(
        [*prefix, *([] if flatpak else ["gsettings"]), "set", "com.gocassini.Viewer", "skip-record-first", "true"],
        check=True,
    )
    # Explicit pre-consent isolates the Background Apps journey from the desktop
    # permission prompt. The real RequestBackground still runs in the app; its
    # denial/late-response behavior is covered by the private-bus GTK test.
    if flatpak:
        subprocess.run(["flatpak", "permission-set", "background", "background", appid, "yes"], check=True)
    launch(appid)
    original_pid = pid(appid)
    broker_configure(appid)
    close_window(appid)
    until(lambda appid=appid: not windows(appid), "last window closed")
    assert owner(appid), "Automatic recording did not keep the process alive"
    case = {"app": appid, "originalPID": original_pid}
    if flatpak:
        row = until(lambda appid=appid: listed(appid), "Flatpak in real Background Apps")
        assert row.get("message") == "Ready to record browser meetings", row
        case["idleStatus"] = row["message"]
        until(lambda appid=appid: menu_item(appid, "item.activate(null)"), "Background Apps Open")
        until(lambda appid=appid: windows(appid), "Library reopened from Background Apps")
        assert pid(appid) == original_pid
        case["backgroundOpen"] = True
    else:
        # Match the documented upstream rejection rather than fabricating an
        # entry. The installed package must still reopen via its real launcher.
        assert listed(appid) is None
        case["backgroundOpen"] = False
        case["limitation"] = "Host applications are excluded by xdg-desktop-portal Background (R48)"
        launch(appid)
        assert pid(appid) == original_pid
    case["appGridReusesProcess"] = True
    if flatpak:
        # Both native activation routes must work, even after the last window
        # has gone and only automatic recording keeps the process alive.
        close_window(appid)
        until(lambda appid=appid: not windows(appid), "second close")
        until(lambda appid=appid: listed(appid), "second Background Apps listing")
        launch(appid)
        assert pid(appid) == original_pid
    action(appid, "record")
    manifest = until(lambda: recording(root), "actual microphone frames")
    case["meeting"] = manifest.parent.name
    close_window(appid)
    click("Keep Recording in Background")
    until(lambda appid=appid: not windows(appid), "recording hidden")
    if flatpak:
        row = until(
            lambda appid=appid: (
                listed(appid) if (listed(appid) or {}).get("message", "").startswith("Recording,") else None
            ),
            "live recording Background Apps status",
        )
        case["recordingStatus"] = row["message"]
        until(
            lambda appid=appid: menu_item(appid, "item.get_last_child().emit('clicked', 1)"),
            "Background Apps close button",
        )
    else:
        action(appid, "quit")
    until(lambda appid=appid: not owner(appid), "stop and keep, then process exit")
    saved = json.loads(manifest.read_text())
    assert saved["state"] == "completed"
    assert any(track["frames"] > 0 for track in saved["tracks"])
    for track in saved["tracks"]:
        with wave.open(str(manifest.parent / track["file"]), "rb") as wav:
            assert wav.getnframes() == track["frames"]
    case["quitKeptAudio"] = True
    case["quitHadNoWindow"] = True
    results["cases"].append(case)
    (OUT / "background-apps.json").write_text(json.dumps(results, indent=2) + "\n")
assert len(results["cases"]) == 2
print(json.dumps(results, indent=2))
