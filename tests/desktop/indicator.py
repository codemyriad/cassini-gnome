#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real GNOME AppIndicator host, installed packages and native menu actions."""

import json
import subprocess
import wave

from gi.repository import GLib
from windowless import (
    HOME,
    OUT,
    action,
    broker_configure,
    call,
    capture_inputs,
    choose_recording_sources,
    click,
    close_window,
    idle_notifications,
    launch,
    measured_privacy,
    owner,
    pid,
    prepare_desktop,
    privacy_indicator,
    recording,
    screenshot,
    shell,
    until,
    windows,
)


def actor(appid):
    return f"Object.values(Main.panel.statusArea).find(a=>a?._indicator?.id==={json.dumps(appid)})"


def indicator(appid):
    return shell(
        "(() => {const a=" + actor(appid) + ";if(!a)return null;return {visible:a.visible,mapped:a.mapped,"
        "title:a._indicator.title,status:a._indicator.status,icon:!!a._icon,"
        "menu:a.menu._getMenuItems().filter(i=>i.visible).map(i=>i.label?.text??'')};})()"
    )


def menu_click(appid, label):
    until(
        lambda: shell(f"(() => {{const a={actor(appid)};if(!a)return false;a.menu.open();return true;}})()"), "SNI menu"
    )
    expression = f"{actor(appid)}?.menu._getMenuItems().find(i=>i.label?.text==={json.dumps(label)} && i.visible)"
    until(
        lambda: shell(f"(() => {{const i={expression};return !!i&&i.mapped&&i.width>0&&i.height>0;}})()"),
        "SNI menu row " + label,
    )
    # Give the real menu row focus and deliver Return through the private
    # compositor's virtual keyboard. The extension receives a real Clutter
    # event and forwards its dbusmenu Event to Cassini.
    previous = shell("global._cassiniQaActivations?.length??0")
    assert shell(
        "(() => {const i=" + expression + ";if(!i)return false;i.grab_key_focus();"
        "global._cassiniQaActivations??=[];i.connect('activate',()=>global._cassiniQaActivations.push(i.label.text));"
        "global._cassiniQaKeyboard??=imports.gi.Clutter.get_default_backend().get_default_seat().create_virtual_device("
        "imports.gi.Clutter.InputDeviceType.KEYBOARD_DEVICE);"
        "const t=imports.gi.GLib.get_monotonic_time();"
        "global._cassiniQaKeyboard.notify_key(t,28,imports.gi.Clutter.KeyState.PRESSED);"
        "imports.gi.GLib.idle_add(imports.gi.GLib.PRIORITY_DEFAULT_IDLE,()=>{"
        "global._cassiniQaKeyboard.notify_key(imports.gi.GLib.get_monotonic_time(),28,imports.gi.Clutter.KeyState.RELEASED);"
        "return imports.gi.GLib.SOURCE_REMOVE;});"
        "return global.stage.get_key_focus()===i;})()"
    )
    until(lambda: shell("global._cassiniQaActivations?.length??0") > previous, "real menu activation " + label)
    assert shell("global._cassiniQaActivations.at(-1)") == label


def kept(manifest):
    value = json.loads(manifest.read_text())
    if value["state"] != "completed":
        return False
    assert any(t["frames"] for t in value["tracks"])
    for track in value["tracks"]:
        with wave.open(str(manifest.parent / track["file"]), "rb") as audio:
            assert audio.getnframes() == track["frames"]
    return value


version = prepare_desktop()
until(
    lambda: shell("Main.extensionManager.lookup('ubuntu-appindicators@ubuntu.com')?.state===1"),
    "distro AppIndicator extension enabled",
)
until(lambda: capture_inputs() == [] and not privacy_indicator()["visible"], "clean privacy baseline")
results = dict(shell=version, baseline=privacy_indicator(), cases=[])
root = HOME / "Music/Cassini"
for appid in ("com.gocassini.Viewer", "com.gocassini.Viewer.Devel"):
    flatpak = appid.endswith(".Devel")
    command = ["flatpak", "run", "--user", "--command=gsettings", appid] if flatpak else ["gsettings"]
    subprocess.run([*command, "set", "com.gocassini.Viewer", "skip-record-first", "true"], check=True)
    subprocess.run([*command, "set", "com.gocassini.Viewer", "background-notification-shown", "false"], check=True)
    if flatpak:
        subprocess.run(["flatpak", "permission-set", "background", "background", appid, "yes"], check=True)
    launch(appid)
    initial_pid = pid(appid)
    broker_configure(appid)
    assert not (indicator(appid) or {}).get("visible"), "Idle with a visible Library should be passive"
    close_window(appid)
    try:
        until(lambda appid=appid: not windows(appid) and (indicator(appid) or {}).get("visible"), "idle SNI visible")
    except AssertionError:
        try:
            properties = call(
                appid,
                "/StatusNotifierItem",
                "org.freedesktop.DBus.Properties",
                "GetAll",
                GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)),
            )[0]
            properties = {key: properties[key] for key in ("Id", "Status", "Title")}
        except GLib.Error as error:
            properties = str(error)
        print(
            "SNI idle diagnostic",
            dict(
                app=appid, owner=owner(appid), windows=windows(appid), indicator=indicator(appid), properties=properties
            ),
            shell(
                "Object.values(Main.panel.statusArea).filter(a=>a?._indicator?.id).map(a=>({id:a._indicator.id,ready:a._indicator.isReady,status:a._indicator.status}))"
            ),
            flush=True,
        )
        screenshot("indicator-idle-failed.png")
        raise
    until(lambda appid=appid: idle_notifications(appid), "still-running notification")
    assert idle_notifications(appid) == 1
    idle = indicator(appid)
    assert idle["mapped"] and idle["title"] == "Ready to record browser meetings", idle
    assert "Stop Recording" not in idle["menu"], idle
    screenshot("indicator-idle-" + appid + ".png")
    menu_click(appid, "Open Cassini")
    until(lambda appid=appid: windows(appid), "SNI Open reopens Library")
    assert pid(appid) == initial_pid
    until(lambda appid=appid: not (indicator(appid) or {}).get("visible"), "idle SNI hidden with Library visible")
    action(appid, "record")
    manifest = until(lambda: recording(root), "recorded microphone frames")
    live = until(
        lambda appid=appid: indicator(appid) if (indicator(appid) or {}).get("visible") else None, "recording SNI"
    )
    assert live["mapped"] and live["title"].startswith("Recording,"), live
    screenshot("indicator-recording-" + appid + ".png")
    privacy = []
    for label, me, others, expected in [
        ("Me", True, False, ["mic"]),
        ("The others", False, True, ["speakers.monitor"]),
    ]:
        choose_recording_sources(manifest, label, me, others, appid)
        until(lambda expected=expected: capture_inputs() == expected, "actual source streams " + label)
        measured = measured_privacy(expected)
        privacy.append(dict(path="mic" if me else "system", inputs=capture_inputs(), indicator=measured))
        screenshot("privacy-" + ("mic" if me else "system") + "-" + appid + ".png")
    menu_click(appid, "Stop Recording")
    until(lambda manifest=manifest: kept(manifest), "SNI Stop keeps WAVs")
    assert owner(appid)
    until(lambda appid=appid: not (indicator(appid) or {}).get("visible"), "SNI passive after stop")
    # The windowless Quit route must stop and keep without an invisible prompt.
    action(appid, "record")
    second = until(lambda: recording(root), "second recording")
    assert second != manifest
    close_window(appid)
    click("Keep Recording in Background")
    until(lambda appid=appid: not windows(appid), "second recording hidden")
    menu_click(appid, "Quit Cassini")
    try:
        until(lambda appid=appid: not owner(appid), "SNI Quit stops and exits")
    except AssertionError:
        print(
            "SNI quit diagnostic",
            indicator(appid),
            shell("global._cassiniQaActivations"),
            "windows",
            windows(appid),
            "manifest",
            json.loads(second.read_text()),
            flush=True,
        )
        screenshot("indicator-quit-failed.png")
        raise
    until(lambda second=second: kept(second), "SNI Quit keeps WAVs")
    results["cases"].append(
        dict(app=appid, idle=idle, live=live, privacy=privacy, opened=True, stopped=True, quitKept=True)
    )
    (OUT / "status-notifier-desktop.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2))
