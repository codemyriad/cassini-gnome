#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A pushed Meeting owns transport while background Library rows keep updating."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.player import Player  # noqa: E402


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
        time.sleep(0.01)
    assert check(), "Playback refresh condition timed out"


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    os.environ["CASSINI_LIBRARY_DIR"] = str(root / "fixtures/main/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(root / "fixtures/main/activity.json")
    os.environ["CASSINI_SCREENSHOT_NOW"] = "1793107080000"
    # All decoded audio goes to a private fakesink, including player replacements.
    connect = LibraryWindow._connect_player

    def private_connect(window):
        if isinstance(window.player, Player):
            sink = Gst.ElementFactory.make("fakesink", None)
            sink.set_property("sync", True)
            window.player._pipeline.set_property("audio-sink", sink)
        connect(window)

    LibraryWindow._connect_player = private_connect
    app = CassiniApplication("com.gocassini.RefreshTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    snapshot = window.snapshot
    own = next(m for m in snapshot.meetings if m.title == "Client call, Rossi & Partners")
    export_dir = window.monitor.scanner.root / "exports"
    export_dir.mkdir()
    opus = export_dir / "standalone.opus"
    pipeline = Gst.parse_launch(
        "audiotestsrc num-buffers=500 ! audioconvert ! audioresample ! "
        "audio/x-raw,rate=48000,channels=1 ! opusenc ! oggmux ! filesink name=output"
    )
    pipeline.get_by_name("output").set_property("location", str(opus))
    pipeline.set_state(Gst.State.PLAYING)
    message = pipeline.get_bus().timed_pop_filtered(8 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)
    assert message and message.type == Gst.MessageType.EOS
    outside = root / "outside.opus"
    shutil.copyfile(opus, outside)
    extra = own.directory / "extra.opus"
    shutil.copyfile(opus, extra)
    for path in (opus, outside, extra):
        app.open_in_new_window(Gio.File.new_for_path(str(path)))
        until(lambda path=path: window.meeting_page and window.meeting_page.meeting.audio_file == path)
        assert window._external_meeting and not window.meeting_page.meeting.metadata_editable
        page = window.meeting_page
        until(lambda: window.player._prerolled)
        page.play.emit("clicked")
        until(lambda: window.player.playing and window.player.position > 0)
        loaded = window._loaded_key
        # Empty and unavailable initial scans must not stop a separately opened file.
        for refreshed in (replace(snapshot, meetings=()), replace(snapshot, meetings=(), unavailable=True), snapshot):
            window._list_signature = None
            window._updated(refreshed)
            assert window.meeting_page is page and window.player.playing and window._loaded_key == loaded
        # Background selection may be a live row without playable audio.
        row = window.rows.get_first_child()
        while row:
            if hasattr(row, "meeting"):
                row.meeting = replace(row.meeting, has_audio=False)
                window._selected(window.rows, row)
                assert window.player.playing and window._loaded_key == loaded
                break
            row = row.get_next_sibling()
        # Actual asynchronous monitor delivery keeps both transport and rows live.
        previous = window.snapshot
        window.monitor.refresh()
        until(lambda previous=previous: window.snapshot is not previous)
        assert window.meeting_page is page and window.player.playing and window._loaded_key == loaded
        window.back_to_library()
    window.close()
print("PASS pushed external playback survives empty/offline/changed Library refreshes and silent sidebar rows")
