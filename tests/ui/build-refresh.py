#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real GApplication Open dispatch preserves one-shot Record and automatic holds."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.library_window import LibraryWindow  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    os.environ["CASSINI_LIBRARY_DIR"] = str(root / "Library")
    meeting = RecordingSession(root / "Library", source="gnome")
    meeting.add_track("mic", "Me", "microphone", 48000, 1)
    meeting.append("mic", bytes(9600), 0)
    meeting.finish()
    app = CassiniApplication("com.gocassini.RestartHandoffTest", "test", "development")
    app.transcription_enabled = False
    # This handoff test has no Background portal. Closing the last window
    # now publishes windowless state at once, and a refused request would
    # reopen the window before the check below; grant it as a desktop would.
    app.background_recording.request = lambda callback: callback(True)
    app._resume_automatic = app._resume_background = app._resume_record = True
    records, startups, errors, completed = [], [], [], []
    # Verify the real dispatch calls the production Record entry once, without
    # opening any capture device in this handoff-only test.
    app._record_current = lambda: records.append(True)
    app.connect("startup", lambda *_args: startups.append(app._auto_recording_held))
    opened_file = LibraryWindow._opened_file

    def callback_error(kind, value, tb):
        errors.append(str(value))
        sys.__excepthook__(kind, value, tb)
        app.quit()

    sys.excepthook = callback_error

    def check_background():
        assert not app.get_windows() and app._auto_recording_held and not app._quitting
        completed.append(True)
        app.finish_quit()
        return GLib.SOURCE_REMOVE

    def check_open(window):
        assert window.meeting_page and window.meeting_page.meeting.directory == meeting.directory
        assert records == [True] and startups == [True], "Open must consume the single Record intent"
        assert not app._resume_record and not app._resume_background and not app._resume_automatic
        app.activate()  # A later grid activation must not repeat Record.
        assert records == [True]
        app.connect("window-removed", lambda *_: GLib.idle_add(check_background))
        window.close()  # Automatic intent must keep the windowless app alive.
        return GLib.SOURCE_REMOVE

    def observe_open(window, future, file):
        result = opened_file(window, future, file)
        GLib.idle_add(check_open, window)
        return result

    def watchdog(*_args):
        raise AssertionError("GApplication restart handoff did not complete")

    guard = GLib.timeout_source_new_seconds(18)
    guard.set_callback(watchdog)
    guard.attach(GLib.MainContext.default())
    # File arguments make GApplication call Open rather than Activate.
    try:
        with patch.object(LibraryWindow, "_opened_file", observe_open):
            result = app.run([sys.argv[0], str(meeting.directory)])
    finally:
        guard.destroy()
    assert result == 0 and completed and not errors
    assert records == [True] and not app._auto_recording_held
print("PASS actual Open handoff: files + Record once; automatic held at startup and after immediate window close")
