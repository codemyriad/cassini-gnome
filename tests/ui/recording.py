#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Production Recording page with real metadata edits in a private display."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, GLib  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
sys.path.insert(0, str(ROOT / "tests"))
from desktop_services import DesktopServices  # noqa: E402

from cassini_viewer.global_shortcut import GlobalShortcut  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.recording_details import SilenceClock  # noqa: E402


def until(predicate):
    deadline = time.monotonic() + 10
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Recording page condition timed out")


services = DesktopServices()
until(
    lambda: services.bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", ("org.gtk.Notifications",)),
        GLib.VariantType.new("(b)"),
        Gio.DBusCallFlags.NONE,
        1000,
        None,
    ).unpack()[0]
)
activations = []
shortcut = GlobalShortcut(lambda: activations.append(True), connection=services.bus)
until(lambda: shortcut.bound)
assert [c["method"] for c in services.calls] == ["CreateSession", "ListShortcuts", "BindShortcuts"]
assert services.calls[2]["values"][1][0][1]["preferred_trigger"] == "CTRL+ALT+r"
services.activate(session="/wrong/session")
services.activate(identifier="wrong")
services.activate()
until(lambda: activations)
assert activations == [True]
shortcut.close()
until(lambda: services.closed)
services.activate()
services.denied = True
denied = GlobalShortcut(lambda: activations.append(False), connection=services.bus)
until(lambda: denied.error)
assert not denied.bound and activations == [True]
denied.close()
services.denied = False

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
    os.environ["CASSINI_LIBRARY_DIR"] = str(tmp / "fixtures/main/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(tmp / "fixtures/main/activity.json")
    os.environ["CASSINI_SCREENSHOT_NOW"] = "1793107080000"
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    app = CassiniApplication("com.gocassini.RecordingPageTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    live = next(m for m in window.snapshot.meetings if m.lifecycle == "recording")
    before = {p.name: p.read_bytes() for p in live.directory.iterdir() if p.name != "meeting.json"}
    window.open_meeting(live)
    page = window.recording_page
    assert page and not window.meeting_page and page.timer.get_label() == "00:12:48"
    from unittest.mock import patch

    with patch.object(Gio.AppInfo, "create_from_commandline", side_effect=GLib.Error("No control centre")):
        page._check_sound()
    assert page.stop.get_sensitive()
    assert not window.lookup_action("export").get_enabled()
    window._play_selected()
    assert not window.player.playing
    page.title_entry.set_text("Renamed while recording")
    assert not hasattr(page, "notes_view") and not hasattr(page, "notes")
    until(lambda: json.loads((live.directory / "meeting.json").read_text())["title"] == "Renamed while recording")
    data = json.loads((live.directory / "meeting.json").read_text())
    assert data["notes"] == [dict(atMs=700000, text="Decision on the travel budget around here")]
    assert data["notes"][0] == dict(atMs=700000, text="Decision on the travel budget around here")
    assert page.headline.get_label() == "Renamed while recording"
    # GTK4 focuses the Entry's inner GtkText. A scanner refresh after the
    # debounce must not reset the user's caret or current selection.
    until(lambda: not page._dirty and not page._saving)
    page.title_entry.grab_focus()
    until(lambda: window.get_focus() is not None and window.get_focus().is_ancestor(page.title_entry))
    page.title_entry.set_position(-1)
    position = page.title_entry.get_position()
    page.update(page.meeting)
    assert page.title_entry.get_position() == position, "Refresh reset the focused title caret"
    page.title_entry.select_region(0, -1)
    selection = page.title_entry.get_selection_bounds()
    page.update(page.meeting)
    assert page.title_entry.get_selection_bounds() == selection, "Refresh cleared the title selection"
    from dataclasses import replace

    current = page.meeting
    page.update(replace(current, title="An earlier scanner title"))
    assert page.title_entry.get_text() == "Renamed while recording", "Refresh overwrote an active edit"
    page.meeting = current
    window.set_focus(None)
    changes = []
    handler = page.title_entry.connect("changed", lambda *_: changes.append(True))
    page.update(current)
    page.title_entry.disconnect(handler)
    assert not changes, "An unchanged unfocused title was rewritten"
    assert all((live.directory / name).read_bytes() == data for name, data in before.items())
    app.comfort.states[live.key] = dict(levels=[0.0, 0.6], voices=4, silent=False, line=None, minutes=50)
    app.comfort.clocks[live.key] = SilenceClock(GLib.get_monotonic_time() / 1e6 - 20)
    app.comfort.tick()
    assert page.check.get_visible() and page.mic_warning.get_visible()
    assert page.others_caption.get_label() == "Listening inside your browser · 4 voices"
    app.comfort.states[live.key].update(line="errors.diskLow.line", minutes=29)
    page.update_status()
    assert page.lines.get_visible()
    # Closing/switching flushes a pending keystroke edit before debounce.
    page.title_entry.set_text("Renamed just before closing")
    window.back_to_library()
    # Reopen the stale scanner object before its debounce; the pending draft
    # must preserve the pending title and the pre-existing notes.
    window.open_meeting(live)
    reopened = window.recording_page
    assert reopened.title_entry.get_text() == "Renamed just before closing"
    reopened.update(live)
    assert reopened.title_entry.get_text() == "Renamed just before closing"
    from concurrent.futures import Future

    failed = Future()
    failed.set_exception(OSError("Synthetic save failure"))
    reopened._saving += 1
    reopened._saved(failed, dict(title=reopened.title_entry.get_text()))
    assert reopened._dirty and reopened._title_dirty

    reopened.title_entry.set_text("Title after reopening")
    window.back_to_library()
    until(lambda: json.loads((live.directory / "meeting.json").read_text())["title"] == "Title after reopening")
    saved = json.loads((live.directory / "meeting.json").read_text())["notes"]
    assert saved == [dict(atMs=700000, text="Decision on the travel budget around here")]
    assert not window.recording_page
    # The sidebar and header share the native monotonic clock even when the
    # wall clock moves a full day forwards or backwards.
    from types import SimpleNamespace

    original_now = window._fixed_now
    original_badge = window._live_badges[live.key]
    app.recorder = SimpleNamespace(
        session=SimpleNamespace(directory=live.directory, manifest={"events": []}),
        pipeline=object(),
        _start_us=GLib.get_monotonic_time() - 65_500_000,
        levels=[0.2, 0.4],
        line=None,
        minutes_left=50,
        stopping=False,
    )
    for shift in (-86_400_000, 86_400_000):
        window._fixed_now = original_now + shift
        window._row(live)
        assert window._live_badges[live.key].get_label() == "Recording 1:05"
        window._update_live()
        assert window._live_badges[live.key].get_label() == "Recording 1:05"
        assert all(c["timer"].get_label() == "1:05" for c in window._capture_headers)
    app.native_captures.clear()  # Retire the injected clock-only fixture controller.
    app.recorder = None
    window._fixed_now = original_now
    window._live_badges[live.key] = original_badge
    window._update_live()
    # GNotification travels over the private bus, not a mocked Python method.
    window.open_meeting(live)
    window.set_visible(False)
    app.comfort.tick()
    until(lambda: "recording" in services.notifications)
    payload = services.notifications["recording"]
    assert payload["title"] == "Recording, 00:12:48"
    assert [b["label"] for b in payload["buttons"]] == ["Stop", "Open Cassini"]
    assert [b["action"] for b in payload["buttons"]] == ["app.stop-recording", "app.open-recording"]
    window._fixed_now += 1000
    app.comfort.tick()
    until(lambda: services.notifications["recording"]["title"] == "Recording, 00:12:49")
    app.activate_action("open-recording", None)
    until(lambda: window.get_visible() and "recording" not in services.notifications)
    window.back_to_library()
    window._fixture_activity = {}
    window._activity = {}
    window.monitor.capture_activity = lambda: {}
    window.monitor.refresh_now()
    until(lambda: not window._pending)
    window.close()
    app.comfort.close()
    # No Library window is required for the Browser window's controller state
    # to drive hidden notifications, meters, track changes and disk observation.
    from cassini_viewer.recording_session import RecordingSession

    browser_app = CassiniApplication("com.gocassini.BrowserOnlyComfortTest", "test", "development")
    browser_app.register(None)
    browser_window = Adw.ApplicationWindow(application=browser_app)
    session = RecordingSession(Path(os.environ["CASSINI_LIBRARY_DIR"]), "Browser-only recording", source="chromium")
    session.add_track("local", "Mic", "local", 48000, 1)
    session.add_track("one", "Remote", "remote", 48000, 1)
    session.append("local", bytes(96000), 0)
    # D26.1 meters measure AC: constant PCM is DC-only silence, not a voice.
    tone = (b"\x00\x20" * 60 + b"\x00\xe0" * 60) * 400  # 400 Hz, one second.
    session.append("one", tone, 0)
    key = session.directory.name
    browser_window._browser_snapshot = dict(statuses=[dict(recording=True, path=str(session.directory))])
    browser_app.comfort.tick()
    until(lambda: key in browser_app.comfort.states and browser_app.comfort.states[key]["voices"] == 1)
    until(
        lambda: (
            "recording" in services.notifications
            and "Browser-only recording" in services.notifications["recording"]["body"]
        )
    )
    assert len(browser_app.get_windows()) == 1 and not browser_window.get_visible()
    assert browser_app.comfort.states[key]["levels"][1] > 0
    session.add_track("two", "Joined later", "remote", 48000, 1)
    session.append("two", tone, 0)
    before = {p.name: p.read_bytes() for p in session.directory.iterdir()}
    browser_app.comfort.tick()
    until(lambda: browser_app.comfort.states[key]["voices"] == 2)
    assert all((session.directory / name).read_bytes() == data for name, data in before.items())
    browser_window._browser_snapshot = dict(statuses=[])
    browser_app.comfort.tick()
    until(lambda: "recording" not in services.notifications)
    session.finish()
    # A native controller also drives notifications without a Library window.
    from types import SimpleNamespace

    native_session = RecordingSession(Path(os.environ["CASSINI_LIBRARY_DIR"]), "Native-only recording", source="gnome")
    native_session.add_track("mic", "Me", "microphone", 48000, 1)
    native_session.append("mic", bytes(96000), 0)
    browser_app.recorder = SimpleNamespace(
        session=native_session,
        pipeline=object(),
        starting=False,
        stopping=False,
        _finishing=False,
        levels=[0.2, 0.5],
        line=None,
        minutes_left=50,
        _start_us=GLib.get_monotonic_time(),
    )
    browser_app.comfort.tick()
    until(
        lambda: (
            "recording" in services.notifications
            and "Native-only recording" in services.notifications["recording"]["body"]
        )
    )
    assert native_session.directory.name in browser_app.comfort.active()
    assert len(browser_app.get_windows()) == 1 and not browser_window.get_visible()
    browser_app.recorder.pipeline = None
    native_session.finish()
    browser_app.comfort.tick()
    until(lambda: "recording" not in services.notifications)
    browser_window.close()
    browser_app.comfort.close()

services.close()
print(
    "PASS portal binding/denial/activation, GNotification/Open, Recording page without notes, title edit/flush/retry, preserved old notes, silence and disk line"
)
