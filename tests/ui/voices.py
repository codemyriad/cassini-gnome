#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private GTK: enrol/duplicate decisions, failures, Forget and future formats."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
from gi.repository import Adw, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import read_metadata  # noqa: E402
from cassini_viewer.settings_dialog import SettingsDialog  # noqa: E402
from cassini_viewer.voice_board import seed  # noqa: E402


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


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
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
    before_audio = {
        p: p.read_bytes() for p in meeting.directory.iterdir() if p.name in ("session.json", "transcript.json")
    }
    future = window.monitor._executor.submit(seed, app, meeting)
    until(future.done)
    future.result()
    window.open_meeting(meeting)
    page = window.meeting_page
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: dialog.voice_data is not None)
    assert dialog.remember.get_sensitive() and dialog.remember.get_active()

    def footer_visible():
        shown, bounds = dialog.save.compute_bounds(window)
        return shown and bounds.get_y() >= 0 and bounds.get_y() + bounds.get_height() <= window.get_height()

    until(footer_visible)
    dialog.entry.set_text("giulia")
    assert dialog.duplicate.get_visible() and not dialog.save.get_sensitive()
    assert dialog.candidate["name"] == "Giulia"
    dialog.same_person.emit("clicked")
    assert not dialog.duplicate.get_visible() and dialog.save.get_sensitive()
    selected = dialog.chosen_voice
    dialog.save.emit("clicked")
    until(lambda: dialog.closed and any(s.name == "Giulia" for s in page.meeting.speakers))
    cache_future = window.monitor._executor.submit(app.voice_store.meeting, meeting.directory)
    until(cache_future.done)
    assert cache_future.result()["speakers"]["guest"]["match"]["voiceId"] == selected
    # Editing a recognised/enrolled name without remembering it still saves the name.
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: dialog.voice_data is not None)
    dialog.entry.set_text("A different person")
    with patch.object(app.voice_store, "remember", side_effect=OSError("disk full")):
        dialog.save.emit("clicked")
        until(lambda: not dialog.busy and dialog.error.get_visible())
        assert not dialog.closed and dialog.save.get_label() == "Try again"
        assert dialog.error.get_label() == "Couldn’t change saved voices. Names in your meetings are kept."
        assert (
            next(s for s in read_metadata(meeting.directory)["speakers"] if s["id"] == "guest")["name"]
            == "A different person"
        )
    dialog.remember.set_active(False)
    assert dialog.retry is None and dialog.save.get_label() == "Save name"
    dialog.save.emit("clicked")
    until(lambda: dialog.closed)
    # A same-name person can explicitly become a separate private profile.
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: dialog.voice_data is not None)
    dialog.entry.set_text("Marta")
    assert dialog.duplicate.get_visible()
    dialog.someone_else.emit("clicked")
    assert dialog.save.get_sensitive()
    dialog.save.emit("clicked")
    until(lambda: dialog.closed)
    assert len([v for v in app.voice_store.snapshot()["voices"] if v["name"] == "Marta"]) == 2
    settings = app.settings_dialog = SettingsDialog(app)
    settings.present(window)
    settings.set_visible_page_name("transcription")
    settings._show_voices()
    voices = settings.saved_voices_page
    until(lambda: len(voices.rows) == 5 and not voices._loading)
    first = next(iter(voices.rows))
    assert app.voice_store.snapshot()["voices"][0]["name"] == "Marta"
    assert voices.rows[first][1].get_sensitive()
    from cassini_viewer.folder_player import FolderPlayer

    original_init = FolderPlayer.__init__

    def private_player(player, **_kwargs):
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", True)
        original_init(player, audio_sink=sink)

    with patch.object(FolderPlayer, "__init__", private_player):
        voices.rows[first][1].emit("clicked")
        until(lambda: voices.player is not None and voices.player.playing)
        assert voices.player._range_end <= voices.player.position + 10000
        voices._stop_sample()
        assert voices.player is None
    # Failed Forget retains the previous store, supports Retry, and keeps names.
    before = (app.voice_store.root / "people.json").read_bytes()
    with patch.object(app.voice_store, "forget", side_effect=OSError("unplugged")):
        voices.confirm(app.voice_store.snapshot()["voices"][0])
        voices.confirmation.emit("response", "forget")
        until(lambda: not voices.busy and voices.error.get_visible())
        assert (app.voice_store.root / "people.json").read_bytes() == before
    voices.again.emit("clicked")
    until(lambda: not voices.busy and len(voices.rows) == 4)
    assert first not in voices.rows
    saved_names = (meeting.directory / "meeting.json").read_bytes()
    voices.confirm(None)
    voices.confirmation.emit("response", "forget")
    until(lambda: not voices.busy and not voices.rows)
    assert voices.empty.get_visible() and not voices.everyone.get_sensitive()
    assert (meeting.directory / "meeting.json").read_bytes() == saved_names
    # A future store is byte-preserved and disables all writes and Remember.
    future_file = app.voice_store.root / "people.json"
    future_file.write_text(json.dumps(dict(version=2, generation=2, voices=[])))
    original = future_file.read_bytes()
    future = settings.executor.submit(app.voice_store.refresh)
    until(future.done)
    until(lambda: voices.newer.get_visible())
    assert not voices.everyone.get_sensitive()
    voices._submit(app.voice_store.forget_everyone)
    assert future_file.read_bytes() == original
    settings.close()
    page.name_speakers("guest")
    dialog = page.speakers_dialog
    until(lambda: dialog.voice_state["readOnly"])
    assert not dialog.remember.get_sensitive() and "newer version" in dialog.remember_description.get_label()
    dialog.entry.set_text("Name still editable")
    dialog.save.emit("clicked")
    until(lambda: dialog.closed)
    assert future_file.read_bytes() == original
    assert (
        next(s for s in read_metadata(meeting.directory)["speakers"] if s["id"] == "guest")["name"]
        == "Name still editable"
    )
    assert all(p.read_bytes() == data for p, data in before_audio.items())
    app.begin_exit()
    window.close()
    app.do_shutdown()
    print(
        "PASS UX17 GTK: Remember, duplicate decisions, private write failure/name saved, retry, sample availability, Forget, Forget everyone, future-store read-only"
    )
