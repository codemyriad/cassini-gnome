#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Pulse capture and keyboard notes; private notification/shortcut wire services."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

if not os.environ.get("CASSINI_ISOLATED_AUDIO") or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use tests/recording/run.sh")
parser = argparse.ArgumentParser()
parser.add_argument("--build", type=Path, required=True)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GdkX11", "4.0")
from desktop_services import DesktopServices  # noqa: E402
from gi.repository import GdkX11, Gio, Gst  # noqa: E402
from readiness import PCMProgress, wait_until  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(args.build.resolve() / "src/cassini-viewer.gresource")))
from cassini_viewer import native_recorder  # noqa: E402
from cassini_viewer.global_shortcut import GlobalShortcut  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import read_metadata  # noqa: E402

artifacts = Path(os.environ["CASSINI_TEST_ARTIFACTS"])
errors, producers, probes, observers = [], [], [], []
report = {}


def callback_error(kind, value, tb):
    errors.append("".join(traceback.format_exception(kind, value, tb)))
    sys.__excepthook__(kind, value, tb)


sys.excepthook = callback_error
services = DesktopServices()
app = None


def diagnostics():
    native = app.recorder if app else None
    return dict(
        errors=errors,
        notifications=services.notifications,
        capture=dict(
            starting=native.starting,
            stopping=native.stopping,
            levels=native.levels,
            line=native.line,
            minutes=native.minutes_left,
        )
        if native
        else None,
    )


def until(condition, description):
    return wait_until(condition, description, diagnostics=diagnostics)


private = tempfile.TemporaryDirectory()
try:
    directory = Path(private.name)
    os.environ["CASSINI_LIBRARY_DIR"] = str(directory / "Library")
    schemas = directory / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    app = CassiniApplication("com.gocassini.RecordingComfortTest", "test", "development")
    app.transcription_enabled = False  # Offline ML integration owns the real inference proof.
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending, "empty Library ready")
    for device, frequency in (("cassini_microphone", 440), ("cassini_desktop", 880)):
        source = Gst.parse_launch(
            f"audiotestsrc name=tone is-live=true freq={frequency} volume=0.2 ! audioconvert ! audioresample ! pulsesink device={device}"
        )
        source.set_state(Gst.State.PLAYING)
        producers.append(source)
        probe = Gst.parse_launch(
            f"pulsesrc device={device}.monitor ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! fakesink name=observed sync=false"
        )
        observers.append(PCMProgress(probe.get_by_name("observed").get_static_pad("sink"), observe_audio=True))
        probes.append(probe)
        probe.set_state(Gst.State.PLAYING)
    until(lambda: all(o.reached(1, audible=True) for o in observers), "both private Pulse sources audible")
    for observer in observers:
        observer.close()
    for probe in probes:
        probe.set_state(Gst.State.NULL)
    app.recording_settings.set_boolean("skip-record-first", True)
    shortcut = app.global_shortcut = GlobalShortcut(app.on_global_record, connection=services.bus)
    until(lambda: shortcut.bound, "portal shortcut bound")
    services.activate()
    until(lambda: app.recorder and app.recorder.pipeline is not None, "portal activation starts real native recording")
    native = app.recorder
    until(lambda: all(w.frames >= 24000 for w in native.session.writers.values()), "both source WAVs grow")
    until(
        lambda: window.recording_page is not None and all(level > 0.1 for level in native.levels),
        "live Recording page and actual meters",
    )
    page = window.recording_page
    # Real key events: spaces belong to notes, not the global player action.
    page.notes_view.grab_focus()
    xid = GdkX11.X11Surface.get_xid(window.get_surface())
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True)
    before = page.elapsed()
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "1", "First decision"], check=True)
    until(lambda: (native.session.directory / "meeting.json").exists(), "note saved as typed")
    until(lambda: read_metadata(native.session.directory)["notes"], "timed note persisted")
    data = read_metadata(native.session.directory)
    note = data["notes"][0]
    assert note["text"] == "First decision" and before <= note["atMs"] <= page.elapsed(), dict(
        note=note, before=before, elapsed=page.elapsed(), playing=window.player.playing
    )
    assert data["title"] is None and data["titleSource"] == "meeting-window", "Notes renamed the meeting"
    assert not window.player.playing, "Typing a space started playback"
    page.title_entry.grab_focus()
    subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+a"], check=True)
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "1", "Team planning"], check=True)
    until(lambda: read_metadata(native.session.directory)["title"] == "Team planning", "title saved separately")
    assert native.session.manifest["title"] == "", "Renaming modified capture metadata"
    producers[0].get_by_name("tone").set_property("volume", 0)
    until(lambda: native.levels[0] == 0, "actual microphone PCM silent")
    app.comfort.clocks[native.session.directory.name].last_sound -= 20
    app.comfort.tick()
    assert page.check.get_visible()
    producers[0].get_by_name("tone").set_property("volume", 0.2)
    until(lambda: native.levels[0] > 0.1, "microphone sound resumes")
    app.comfort.tick()
    assert not page.check.get_visible()
    real_space = native_recorder.space_left
    space = [(29, False)]
    reads = []

    def simulated_space(*_args):
        reads.append(space[0])
        return space[0]

    native_recorder.space_left = simulated_space
    until(lambda: native.line == "errors.diskLow.line", "below thirty minutes warns")
    until(lambda: "recording-space" in services.notifications, "disk warning delivered over private D-Bus")
    assert services.notifications["recording-space"]["title"] == "Only 29 minutes of space left."
    assert not services.notifications["recording-space"].get("buttons")
    until(lambda: len(reads) >= 2, "repeated space sample")
    assert sum(n["id"] == "recording-space" for n in services.history) == 1
    app.on_quit()
    dialog = app._capture_confirmation
    dialog.emit("response", "keep")
    dialog.force_close()
    frames = min(w.frames for w in native.session.writers.values())
    until(
        lambda: all(w.frames >= frames + 9600 for w in native.session.writers.values()),
        "hidden app keeps capturing",
    )
    until(lambda: "recording" in services.notifications, "hidden GNotification delivered")
    payload = services.notifications["recording"]
    assert [b["label"] for b in payload["buttons"]] == ["Stop", "Open Cassini"]
    assert "browser" not in payload["body"], "Native notification claims browser capture"
    services.app_action(app, "open-recording")
    until(window.get_visible, "notification Open action restores Cassini")
    app.on_quit()
    dialog = app._capture_confirmation
    dialog.emit("response", "keep")
    dialog.force_close()
    until(lambda: "recording" in services.notifications, "hidden progress notification returns")
    services.app_action(app, "stop-recording")
    until(lambda: native.pipeline is None, "notification Stop finalizes capture")
    assert native.session.manifest["state"] == "completed"
    first_saved = native.session.directory
    services.app_action(app, "open-recording")
    until(lambda: any(w.get_visible() for w in app.get_windows()), "saved meeting can be reopened")
    window = app.get_active_window()
    space[0] = (9, False)
    services.activate()
    until(lambda: native.pipeline is not None, "global shortcut starts next recording")
    assert native.minutes_left == 9 and native.line == "errors.diskLow.line", "low-space start was blocked or unwarned"
    until(lambda: all(w.frames >= 9600 for w in native.session.writers.values()), "low-space capture is allowed")
    space[0] = (0, True)
    until(lambda: native.pipeline is None, "reserve automatically stops and keeps")
    assert native.session.manifest["state"] == "completed" and all(
        t["frames"] > 0 for t in native.session.manifest["tracks"]
    )
    until(
        lambda: window.meeting_page is not None and window.meeting_page.recording_notice.get_visible(),
        "disk-stopped line persists on saved Meeting",
    )
    assert window.meeting_page.meeting.directory != first_saved
    space[0] = (90, False)
    services.activate()
    until(lambda: native.pipeline is not None, "last-edit close capture starts")
    until(
        lambda: window.recording_page is not None and all(w.frames >= 9600 for w in native.session.writers.values()),
        "last-edit capture has audio",
    )
    services.app_action(app, "open-recording")
    until(window.get_visible, "open last-edit recording")
    final_directory = native.session.directory
    window.recording_page.notes_view.get_buffer().insert_at_cursor("Last keystroke before closing")
    app.on_quit()
    dialog = app._capture_confirmation
    dialog.emit("response", "stop")
    dialog.force_close()
    until(lambda: not app.get_windows() and native.pipeline is None, "Stop and save closes after finalization")
    app.comfort.close()
    until(
        lambda: (
            read_metadata(final_directory)["notes"]
            and read_metadata(final_directory)["notes"][-1]["text"] == "Last keystroke before closing"
        ),
        "last edit survives monitor close and editor shutdown",
    )
    native_recorder.space_left = real_space
    report.update(
        actualPulse=True,
        portalStarts=True,
        keyboardTimedNotes=True,
        notesPreserveTitle=True,
        silentMicAndRecovery=True,
        lowSpaceWarnsOnce=True,
        hiddenAudioGrows=True,
        notificationWireOpenAndStop=True,
        lowSpaceStartAllowed=True,
        reserveStopsAndKeeps=True,
        finalEditSurvivesClose=True,
    )
    app.on_quit()
    app.comfort.close()
    shortcut.close()
    until(lambda: services.closed, "portal session closed")
    assert not errors, errors
    report["callbackErrors"] = errors
    print("PASS Recording page: real Pulse/keyboard, portal, hidden notifications, disk warning/reserve")
finally:
    if app and app.recorder and app.recorder.pipeline is not None:
        app.stop_native()
        wait_until(lambda: app.recorder.pipeline is None, "failure cleanup finalizes capture", diagnostics=diagnostics)
    if app and app.comfort:
        app.comfort.close()
    for observer in observers:
        observer.close()
    for pipeline in probes + producers:
        pipeline.set_state(Gst.State.NULL)
    services.close()
    private.cleanup()
    (artifacts / "comfort.json").write_text(json.dumps(dict(report, diagnostics=diagnostics()), indent=2) + "\n")
