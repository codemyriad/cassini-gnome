#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D48 real publication Retry, missing-file action and no-x edit controls."""

import errno
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
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from library_v2_support import simple_session  # noqa: E402

from cassini_viewer import portable  # noqa: E402
from cassini_viewer.library_edits import commit_metadata, transaction  # noqa: E402
from cassini_viewer.library_recovery import LibraryRecovery  # noqa: E402
from cassini_viewer.library_v2 import edit, finalize, xschema  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_store import read_metadata  # noqa: E402
from cassini_viewer.portable_writer import MANIFEST_MIME, chunk_set  # noqa: E402
from cassini_viewer.strings import text  # noqa: E402


def until(predicate, description):
    loop = GLib.MainLoop()
    failures = []

    def check():
        try:
            if predicate():
                loop.quit()
                return False
        except BaseException as error:
            failures.append(error)
            loop.quit()
            return False
        return True

    def expired():
        failures.append(AssertionError(f"Timed out waiting for {description}"))
        loop.quit()
        return False

    tick, limit = GLib.timeout_add(10, check), GLib.timeout_add_seconds(15, expired)
    loop.run()
    for source in (tick, limit):
        if GLib.MainContext.default().find_source_by_id(source):
            GLib.source_remove(source)
    if failures:
        raise failures[0]


def walk(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from walk(child)
        child = child.get_next_sibling()


def button(widget, label):
    return next(w for w in walk(widget) if isinstance(w, Gtk.Button) and w.get_label() == label)


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    root = tmp / "Library"
    root.mkdir()
    os.environ["CASSINI_LIBRARY_DIR"] = str(root)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    sid, directory = simple_session(root, 1.0)
    with patch.object(finalize, "finalize", side_effect=OSError(errno.ENOSPC, "Synthetic disk full")):
        app = CassiniApplication("io.codemyriad.PublicationTest", "test", "development")
        app.register(None)
        app.activate()
        window = app.get_active_window()
        app.library_recovery = LibraryRecovery(app)
        until(
            lambda: app.library_recovery.future is None and bool(app.library_recovery.failures), "publication failure"
        )
        until(
            lambda: window.selected and window.selected.publication_notice == "library.publishDiskLow",
            "warning in Library",
        )
        assert text("library.publishDiskLow") in [
            w.get_label() for w in walk(window.preview) if isinstance(w, Gtk.Label)
        ]
        assert directory.is_dir() and not list(root.glob("*.opus"))
    button(window.preview, text("common.tryAgain")).emit("clicked")
    until(lambda: window.selected and window.selected.published_file is not None, "Retry publishes retained audio")
    assert window.selected.publication_notice is None and not app.library_recovery.failures
    with transaction(directory):
        commit_metadata(
            directory,
            read_metadata(directory),
            dict(
                speakers=[dict(id="a", name=None, provenance="unnamed", colourIndex=0)],
                transcript=dict(status="done", reference=None),
            ),
            words=dict(format=portable.WORDS_FORMAT, items=[dict(speaker="a", startMs=0, endMs=90, text="Hello")]),
        )
    path = window.selected.published_file
    saved_bytes = path.read_bytes()
    path.unlink()
    window.monitor.refresh()
    until(lambda: window.selected and window.selected.published_missing, "missing-file notice")
    assert window.selected.publication_notice == "library.publishedMissing"
    launched = []
    with patch.object(Gtk.FileLauncher, "launch", lambda launcher, *_: launched.append(launcher.get_file().get_path())):
        button(window.preview, text("meeting.showInFiles")).emit("clicked")
    assert launched == [str(root)] and not window.play_button.get_sensitive()
    # Restore a real file, remove only our x, then exercise its real window.
    path.write_bytes(saved_bytes)
    manifest = portable.read_meeting(str(path)).manifest
    manifest["x"].pop(xschema.DOMAIN)
    tags, _ = chunk_set(edit.MANIFEST_PREFIX, manifest, MANIFEST_MIME)
    with path.open("rb") as stream:
        vendor, fields = edit.comment_fields(stream)
    stage = path.with_suffix(".test-stage")
    edit.rewrite(path, stage, edit.edited_fields(fields, tags, chunk_prefixes=(edit.MANIFEST_PREFIX,)), vendor)
    edit.verify(stage)
    stage.replace(path)
    shutil.rmtree(directory)
    window.monitor.refresh()
    until(
        lambda: window.selected and window.selected.has_audio and not window.selected.metadata_editable,
        "read-only root file",
    )
    assert not window.lookup_action("rename").get_enabled()
    assert not window.lookup_action("move-to-trash").get_enabled()
    assert window.play_button.get_sensitive()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player._pipeline.set_property("audio-sink", sink)
    window.play_button.emit("clicked")
    until(lambda: window.player.playing, "read-only playback")
    window.player.pause()
    window.open_meeting(window.selected)
    until(lambda: window.meeting_page is not None, "read-only Meeting page")
    assert not window.lookup_action("rename").get_enabled()
    assert not window.lookup_action("move-to-trash").get_enabled()
    assert not button(window.meeting_page, text("meeting.nameSpeakers")).get_sensitive()
    window.close()
print("D48 GTK passed: disk warning, Retry, missing file, read-only root playback and edit controls")
