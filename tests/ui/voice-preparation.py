#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Missing models and historical prints have truthful, recoverable UI states."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.main import CassiniApplication  # noqa: E402


def until(check):
    deadline = time.monotonic() + 8
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    assert check(), "Saved voices GUI did not settle"


def labels(widget):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        values += labels(child)
        child = child.get_next_sibling()
    return values


def speaker_labels(page, identifier):
    index = next(i for i, s in enumerate(page.meeting.speakers) if s.id == identifier)
    child = page.speakers.get_first_child().get_next_sibling()
    for _ in range(index):
        child = child.get_next_sibling()
    return labels(child)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(root / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK
        if os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
        else Adw.ColorScheme.FORCE_LIGHT
    )
    app = CassiniApplication("com.gocassini.VoicesTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    window.open_meeting(meeting)
    page = window.meeting_page
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: dialog.voices_loaded)
    assert dialog.voice_data is None and not dialog.voice_prepared
    assert not dialog.remember.get_sensitive()
    assert (
        dialog.remember_description.get_label()
        == "To remember voices, download and turn on Tell speakers apart and Recognise saved voices in Settings."
    )
    assert dialog.voice_settings.get_visible()
    dialog.voice_settings.emit("clicked")
    until(lambda: dialog.closed and app.settings_dialog is not None)
    assert app.settings_dialog.get_visible_page_name() == "transcription"
    app.settings_dialog.close()

    model = SimpleNamespace(
        snapshot=lambda: dict(ready=True, downloaded=True, busy=False, downloading=False, percent=100, error=False),
        close=lambda: None,
    )
    app.voice_models = app.diarization_models = model
    app._models_changed()
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: dialog.voices_loaded)
    assert not dialog.voice_prepared and dialog.voice_data is None
    assert (
        dialog.remember_description.get_label()
        == "Cassini hasn’t prepared this meeting’s voices yet. You can still save the name."
    )
    assert not dialog.voice_settings.get_visible()
    dialog.entry.set_text("Remembered after preparation")
    entered = dialog.entry.get_text()
    # A completed, empty extraction is evidence of under-four-second speech;
    # mere absence of a print is not. Private store IO stays off GTK.
    future = window.monitor._executor.submit(app.voice_store.commit_job, meeting.directory, {}, 0)
    until(future.done)
    future.result()
    app._transcription_changed()
    until(lambda: dialog.voice_prepared)
    assert "at least 4 seconds" in dialog.remember_description.get_label()
    assert not dialog.remember.get_sensitive()
    # A late background result must refresh this same dialog and preserve input.
    printed = dict(
        embedding=[float(i == 0) for i in range(512)], seconds=8, windows=[dict(startMs=192000, endMs=200000)]
    )
    future = window.monitor._executor.submit(app.voice_store.commit_job, meeting.directory, {"guest": printed}, 0)
    until(future.done)
    future.result()
    app._transcription_changed()
    until(lambda: dialog.voice_data is not None and dialog.remember.get_sensitive())
    assert page.speakers_dialog is dialog and dialog.entry.get_text() == entered
    assert not dialog.voice_settings.get_visible()
    assert "Future meetings" in dialog.remember_description.get_label()
    dialog.save.emit("clicked")
    until(lambda: dialog.closed and app.voice_store.snapshot()["voices"])
    assert app.voice_store.snapshot()["voices"][0]["name"] == entered
    app.on_preferences()
    settings = app.settings_dialog
    settings.set_visible_page_name("transcription")
    until(lambda: entered in str(app.voice_store.snapshot()["voices"]))
    settings._show_voices()
    until(lambda: settings.saved_voices_page is not None and settings.saved_voices_page.rows)
    assert entered in labels(settings.saved_voices_page)
    settings.close()
    app.begin_exit()
    window.close()
    app.do_shutdown()
    print(
        "PASS voice preparation GTK: missing models, Settings link, unprepared vs short, late print refresh, preserved input, enrol and Saved voices visibility"
    )
