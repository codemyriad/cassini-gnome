#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real native capture, move confirmation, retained watches, retry and Search in private Xvfb."""

import os
import shutil
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
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer import library_location as location  # noqa: E402
from cassini_viewer.folder_player import FolderPlayer  # noqa: E402
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import atomic_json, update_metadata  # noqa: E402
from cassini_viewer.player import Player  # noqa: E402
from cassini_viewer.recording_session import RecordingSession  # noqa: E402


def until(check):
    deadline = time.monotonic() + 15
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Library move GUI condition timed out"


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from descendants(child)
        child = child.get_next_sibling()


def saved(root, title):
    recording = RecordingSession(root, title, source="gnome")
    recording.add_track("mic", "Me", "microphone", 48000, 1)
    recording.append("mic", bytes(48000 * 2), 0)
    recording.finish()
    return recording.directory


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    old, new = tmp / "old", tmp / "new"
    os.environ["CASSINI_LIBRARY_DIR"] = str(old)
    new.mkdir()
    idle = saved(old, "Searchable idle meeting")
    writing = saved(old, "Writing down")
    collision = saved(old, "Collision")
    shutil.copytree(collision, new / collision.name)
    atomic_json(
        idle / "transcript.json",
        dict(
            format="cassini.words.v1",
            language="en",
            items=[
                dict(startMs=0, endMs=900, text="Relocation needle", speaker="speaker-1"),
            ],
        ),
    )
    update_metadata(
        idle,
        transcript=dict(
            status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        ),
    )
    update_metadata(writing, transcript=dict(status="running", reference=None))
    connect = LibraryWindow._connect_player

    def private_connect(window):
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", True)
        if isinstance(window.player, FolderPlayer):
            window.player.audio_sink = sink
        elif isinstance(window.player, Player):
            window.player._pipeline.set_property("audio-sink", sink)
        connect(window)

    LibraryWindow._connect_player = private_connect
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    import subprocess

    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    app = CassiniApplication("com.gocassini.LibraryMoveTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    app.start_native(test=True)
    until(
        lambda: (
            app.recorder.pipeline is not None and all(w.frames >= 9600 for w in app.recorder.session.writers.values())
        )
    )
    live = app.recorder.session.directory
    until(lambda: any(m.directory == live for m in window.snapshot.meetings))
    app.on_preferences()
    dialog = app.settings_dialog
    dialog.set_visible_page_name("library")
    chosen = []
    with patch.object(
        Gtk.FileDialog, "select_folder", lambda chooser, parent, cancel, callback: chosen.append((chooser, callback))
    ):
        dialog.move.emit("clicked")
    chooser, callback = chosen.pop()
    assert chooser.get_initial_folder().get_path() == str(old)
    with patch.object(Gtk.FileDialog, "select_folder_finish", lambda *_: Gio.File.new_for_path(str(new))):
        callback(chooser, None)
    app._move_confirmation.emit("response", "cancel")
    assert location.primary() == old
    app.move_library(new, dialog)
    app._move_confirmation.emit("response", "move")
    until(
        lambda: (
            location.primary() == new and app.library_relocator.state is not None and not app.library_relocator.running
        )
    )
    until(lambda: len(location.load()["pending"]) == 3 and not idle.exists())
    until(lambda: len(window.snapshot.meetings) == 5)
    assert live.exists() and writing.exists() and collision.exists()
    until(lambda: any(w.get_title() == "2 meetings move when they are finished." for w in dialog._move_rows))
    assert any("1 meeting could not be moved." in w.get_title() for w in dialog._move_rows)
    before = app.recorder.session.writers["microphone"].frames
    until(lambda: app.recorder.session.writers["microphone"].frames >= before + 9600)
    assert app.recorder.session.directory == live
    # A metadata change at a retained root is delivered by real GIO watches.
    update_metadata(writing, title="Retained row updated")
    until(lambda: any(m.title == "Retained row updated" for m in window.snapshot.meetings))
    shutil.rmtree(new / collision.name)
    retry = next(w for w in descendants(dialog) if isinstance(w, Gtk.Button) and w.get_label() == "Try again")
    retry.emit("clicked")
    until(lambda: not collision.exists() and len(location.load()["pending"]) == 2)
    dialog.force_close()
    app.stop_native()
    until(lambda: not app.recorder.stopping and app.recorder.pipeline is None)
    until(lambda: not live.exists())
    assert (new / live.name).is_dir() and app.recording_error is None
    update_metadata(writing, transcript=dict(status="none", reference=None))
    until(lambda: not location.load()["pending"])
    until(lambda: all(m.directory.parent == new for m in window.snapshot.meetings))
    window.toggle_search()
    window.search_page.entry.set_text("relocation needle")
    until(lambda: not window.search_page.pending and len(window.search_page.hits) == 1)
    hit = window.search_page.hits[0]
    assert window.search_page._meetings[hit.key].directory == new / idle.name
    assert window.search_page._index_root == new
    window.back_to_library()
    app.start_native(test=True)
    until(lambda: app.recorder.pipeline is not None)
    assert app.recorder.session.directory.parent == new
    app.stop_native()
    until(lambda: not app.recorder.stopping and app.recorder.pipeline is None)
    window.close()
    app.do_shutdown()
    assert not app.library_relocator._held
    print(
        "PASS Library move: chooser/confirmation, live PCM deferral, retained watches, failed-only retry, idle publication, new capture and rebuilt Search"
    )
