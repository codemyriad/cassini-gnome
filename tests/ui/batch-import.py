#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 actual multi-file delivery, separate workers, counts and button cancellation."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from dispatch_wait import dispatch_until  # noqa: E402
from gi.repository import Gio, Graphene  # noqa: E402
from import_bindings import check_import_bindings  # noqa: E402
from painted import painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk import archive_import, portable  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.portable_writer import rewrite_tags  # noqa: E402
from cassini_talk.strings import plural, text  # noqa: E402


def until(check):
    dispatch_until(check, "D96 import GUI did not complete")


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    root = tmp / "Library"
    root.mkdir()
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(root))
    app = CassiniApplication("com.gocassini.BatchImportTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    check_import_bindings()
    messages = []
    toast = window.toast
    window.toast = lambda message: (messages.append(message), toast(message))
    one = ROOT / "tests/conformance/opus/001-minimal-v1.opus"
    two = ROOT / "tests/conformance/opus/002-plain-audio.opus"
    archive = tmp / "Several English meetings.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in [
            ("First.opus", one.read_bytes()),
            ("Second.opus", two.read_bytes()),
            ("Duplicate.opus", one.read_bytes()),
            ("Read me.txt", b"Synthetic English notes."),
        ]:
            output.writestr(name, data)
    entered, release, scanned = threading.Event(), threading.Event(), threading.Event()
    gtk_thread = threading.get_ident()
    preflight = archive_import.preflight
    scan = window.monitor.scanner.scan

    def held_preflight(stream):
        assert threading.get_ident() != gtk_thread, "ZIP parsed on the GTK thread"
        entered.set()
        assert release.wait(30), "Archive gate was never released"
        return preflight(stream)

    def observed_scan(*args, **kwargs):
        result = scan(*args, **kwargs)
        scanned.set()
        return result

    files = Gio.ListStore.new(Gio.File)
    for path in [one, archive]:
        files.append(Gio.File.new_for_path(str(path)))
    picker = SimpleNamespace(open_multiple_finish=lambda _result: files)
    with patch.object(archive_import, "preflight", held_preflight):
        app._on_open_chosen(picker, None)
        try:
            until(entered.is_set)
            with patch.object(window.monitor.scanner, "scan", observed_scan):
                window.monitor.refresh_now()
                until(scanned.is_set)
        finally:
            release.set()
        until(lambda: not window._opening_files and not window._pending_selection)
    assert len(window.snapshot.meetings) == 2 and not window._import_dialogs
    assert messages[-1] == " · ".join(
        [
            plural("import.summary.imported", 2),
            plural("import.summary.duplicates", 2),
            plural("import.summary.unsupported", 1),
        ]
    )
    # The second operation commits a new file before its ZIP is cancelled.
    third = tmp / "Third English meeting.opus"
    tags = dict(portable.read_meeting(str(two)).tags, TITLE="Third synthetic meeting")
    with two.open("rb") as incoming, third.open("wb") as outgoing:
        rewrite_tags(incoming, outgoing, tags)
    entered.clear()
    release.clear()
    with patch.object(archive_import, "preflight", held_preflight):
        window.open_files([Gio.File.new_for_path(str(third)), Gio.File.new_for_path(str(archive))])
        try:
            until(entered.is_set)
            active = next(iter(window._import_dialogs))
            until(lambda: active.get_mapped())
            node = painted_node(window)
            viewport = Graphene.Rect()
            viewport.init(0, 0, window.get_width(), window.get_height())
            texture = window.get_renderer().render_texture(node, viewport)
            assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
            active.cancel_button.emit("clicked")
            assert active.cancelled.is_set() and active.closed
        finally:
            release.set()
        until(lambda: not window._opening_files and not window._pending_selection)
    assert len(window.snapshot.meetings) == 3
    assert messages[-1] == " · ".join([plural("import.summary.imported", 1), text("import.cancelled")])
    assert not list(root.glob(".received-*"))
    window.close()
    app.comfort.close()
print("PASS D96 picker batching, actual ZIP import/counts, independent monitor and cancellation")
