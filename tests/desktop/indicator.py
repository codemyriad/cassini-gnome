#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real GNOME AppIndicator host, installed packages and native menu actions."""

import base64
import json
import math
import os
import struct
import subprocess
import time
import uuid
import wave
from pathlib import Path

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
    pointer_click,
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
    expression = f"{actor(appid)}?.menu._getMenuItems().find(i=>i.label?.text==={json.dumps(label)} && i.visible)"

    def row_shown():
        # Open the indicator's menu as its own button would, again if something
        # (a banner, the overview) closed it, until the row is laid out.
        return shell(
            f"(() => {{const a={actor(appid)};if(!a)return false;if(!a.menu.isOpen)a.menu.open();"
            f"const i={expression};return !!i&&i.mapped&&i.width>0&&i.height>0;}})()"
        )

    until(row_shown, "SNI menu row " + label)
    # Click the real menu row through the compositor's virtual pointer. The
    # extension receives a real Clutter event and forwards its dbusmenu Event
    # to Cassini. (Virtual Return on a focused row was not delivered once the
    # pointer had clicked inside a client window.)
    previous = shell("global._cassiniQaActivations?.length??0")
    # The menu slides open; aim only once the compositor's own pick at the
    # row's centre is that row, so the click cannot land beside the menu.
    centre = until(
        lambda: shell(
            "(() => {const i=" + expression + ";if(!i||!" + actor(appid) + ".menu.isOpen)return null;"
            "const [x,y]=i.get_transformed_position();const [w,h]=i.get_transformed_size();"
            "const hit=global.stage.get_actor_at_pos(imports.gi.Clutter.PickMode.REACTIVE,x+w/2,y+h/2);"
            "return hit&&(hit===i||i.contains(hit))?[x+w/2,y+h/2]:null;})()"
        ),
        "SNI menu row settled " + label,
    )
    assert shell(
        "(() => {const i=" + expression + ";if(!i)return false;global._cassiniQaActivations??=[];"
        "i.connect('activate',()=>global._cassiniQaActivations.push(i.label.text));return true;})()"
    )
    pointer_click(centre[0], centre[1], "SNI menu row " + label)
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


def tail_rms(manifest, kind, frames):
    """RMS of the newest whole frames of one live WAV track (S16LE mono)."""
    value = json.loads(manifest.read_text())
    # A source change ends one span and starts a new track: use the newest.
    track = [t for t in value["tracks"] if t["kind"] == kind][-1]
    data = (manifest.parent / track["file"]).read_bytes()
    count = min(frames, (len(data) - 44) // 2)
    samples = struct.unpack(f"<{count}h", data[len(data) - 2 * count :])
    return round(math.sqrt(sum(v * v for v in samples) / max(1, count)), 1)


def track_frames(manifest, kind):
    value = json.loads(manifest.read_text())
    return ([t["frames"] for t in value["tracks"] if t["kind"] == kind] or [0])[-1]


def muted_lesson(manifest, appid):
    """D18: a lesson recorded through system audio while the speakers are muted."""
    reference = tail_rms(manifest, "system", 12000)
    subprocess.run(["pactl", "set-sink-mute", "speakers", "1"], check=True)
    try:
        until(
            lambda: "yes" in subprocess.check_output(["pactl", "get-sink-mute", "speakers"], text=True),
            "speakers muted",
        )
        start = track_frames(manifest, "system")
        until(lambda: track_frames(manifest, "system") >= start + 48000, "one muted second recorded")
        muted = tail_rms(manifest, "system", 12000)
        indicator = measured_privacy(["speakers.monitor"])
        screenshot("privacy-muted-lesson-" + appid + ".png")
    finally:
        subprocess.run(["pactl", "set-sink-mute", "speakers", "0"], check=True)
    return dict(
        path="muted lesson (system audio, speakers muted)",
        inputs=capture_inputs(),
        indicator=indicator,
        toneRmsBeforeMute=reference,
        toneRmsWhileMuted=muted,
    )


def browser_only(appid, microphone):
    """Browser companion only: real broker and recording session, PCM from a stand-in tab.

    The extension's in-page tracks arrive over the companion's audio protocol,
    exactly as Chrome's native-messaging child forwards them. No browser runs
    here, so a meeting page's own getUserMedia stream is not part of this row.
    """
    import sys

    sys.path.insert(0, "/usr/share/cassini-viewer")
    from cassini_viewer.browser_audio_proxy import BrowserAudioProxy

    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    socket = (runtime / "app" / appid if appid.endswith(".Devel") else runtime) / "cassini" / "browser-v2.sock"
    until(socket.exists, "browser broker socket")
    proxy = BrowserAudioProxy(path=socket)
    try:
        capture = str(uuid.uuid4())
        proxy.handle(
            dict(
                type="start",
                version=1,
                independent=True,
                captureId=capture,
                microphone=microphone,
                timelineOriginMs=int(time.time() * 1000),
                title="D41.1 browser-only privacy measurement",
            )
        )
        proxy.handle(dict(type="track", id="remote", label="Other", kind="remote", sampleRate=48000, channels=1))
        tone = b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(48000))
        for sequence in range(3):
            proxy.handle(
                dict(
                    type="audio",
                    id="remote",
                    sequence=sequence,
                    offsetMs=1000 * sequence,
                    pcm=base64.b64encode(tone).decode(),
                )
            )
        live = until(
            lambda: indicator(appid) if (indicator(appid) or {}).get("title", "").startswith("Recording,") else None,
            "browser-only recording in the SNI",
        )
        expected = ["mic"] if microphone else []
        measured = measured_privacy(expected)
        name = "browser-mic" if microphone else "browser-others"
        screenshot("privacy-" + name + "-" + appid + ".png")
        stopped = proxy.handle(dict(type="stop"))
        until(lambda: capture_inputs() == [], "browser capture streams released")
    finally:
        proxy.close()
    return dict(
        path="browser companion only, " + ("Me + The others" if microphone else "The others"),
        inputs=expected,
        indicator=measured,
        sniTitle=live["title"],
        stopped=stopped.get("type"),
    )


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
    # D41.1: GNOME's own top-bar microphone privacy indicator, per capture path.
    # Each row records the actual Pulse capture streams GNOME saw. Nothing here
    # opens a capture stream of its own.
    both = ["mic", "speakers.monitor"]
    privacy.append(dict(path="mic+system", inputs=both, indicator=measured_privacy(both)))
    for label, me, others, expected in [
        ("Me", True, False, ["mic"]),
        ("The others", False, True, ["speakers.monitor"]),
    ]:
        choose_recording_sources(manifest, label, me, others, appid)
        until(lambda expected=expected: capture_inputs() == expected, "actual source streams " + label)
        measured = measured_privacy(expected)
        privacy.append(dict(path="mic" if me else "system", inputs=capture_inputs(), indicator=measured))
        screenshot("privacy-" + ("mic" if me else "system") + "-" + appid + ".png")
    privacy.append(muted_lesson(manifest, appid))
    menu_click(appid, "Stop Recording")
    until(lambda manifest=manifest: kept(manifest), "SNI Stop keeps WAVs")
    assert owner(appid)
    until(lambda appid=appid: not (indicator(appid) or {}).get("visible"), "SNI passive after stop")
    for microphone in (False, True):
        privacy.append(browser_only(appid, microphone))
        until(lambda appid=appid: not (indicator(appid) or {}).get("visible"), "SNI passive after browser-only Stop")
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
