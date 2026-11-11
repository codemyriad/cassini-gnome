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
from gi.repository import Gio, Graphene, Gtk  # noqa: E402
from import_bindings import check_import_bindings  # noqa: E402
from painted import painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk import archive_import, portable, zip_export  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.portable_writer import rewrite_tags  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402
from cassini_talk.strings import plural, text  # noqa: E402


def until(check):
    dispatch_until(check, "D96 import/export GUI did not complete")


def descendants(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def ready_chooser(folder=None):
    until(lambda: any(isinstance(w, Gtk.FileChooserDialog) for w in Gtk.Window.get_toplevels()))
    chooser = next(w for w in Gtk.Window.get_toplevels() if isinstance(w, Gtk.FileChooserDialog))
    bar = next((w for w in descendants(chooser) if w.get_css_name() == "pathbar"), None)
    assert bar is not None, "Chooser path bar not found"

    def shown():
        current = chooser.get_current_folder()
        active_label = None
        for button in descendants(bar):
            if isinstance(button, Gtk.ToggleButton) and button.get_active():
                active_label = next((w.get_text() for w in descendants(button) if isinstance(w, Gtk.Label)), None)
                break
        return (
            current is not None
            and (folder is None or current.get_path() == str(folder))
            and active_label == current.get_basename()
        )

    until(shown)
    return chooser


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
    assert not window.lookup_action("export-zip").get_enabled()
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

    # D96 bulk stored-.zip export: excludes live capture, prunes Trash live, exports selected
    # (including a real saved PCM RecordingSession) and all, cancels cleanly, and deduplicates on re-import.
    own_session = RecordingSession(root, title="Own PCM Standup")
    own_session.add_track("mic", "Me", "microphone", 48000, 1)
    own_session.append("mic", bytes(48000 * 2), 0)
    own_session.finish()
    live_session = RecordingSession(root, title="Live Active Call")
    live_session.add_track("mic", "Me", "microphone", 48000, 1)
    live_session.append("mic", bytes(4800 * 2), 0)
    window._activity = {live_session.manifest["id"]: "recording"}
    window.monitor.refresh_now()
    until(lambda: len(window.snapshot.meetings) == 5)
    assert window.lookup_action("export-zip").get_enabled()

    window.lookup_action("export-zip").activate(None)
    until(lambda: window.zip_export_dialog is not None and window.zip_export_dialog.get_mapped())
    zdialog = window.zip_export_dialog
    assert len(zdialog.choices) == 4 and len(zdialog.selected_keys) == 4
    assert all(m.title != "Live Active Call" for m in zdialog.choices)
    assert zdialog.export_selected_button.get_visible()

    # Moving a meeting to Trash while selector is open prunes it live; Undo restores it to choices.
    victim = next(m for m in zdialog.choices if m.origin == "received")
    app.meeting_trash.request(victim, window)
    until(lambda: len(zdialog.choices) == 3 and victim.key not in zdialog.selected_keys)
    operation = app.meeting_trash.operations[victim.directory.absolute()]
    app.meeting_trash.undo(operation)
    window.toasts.dismiss_all()
    until(lambda: len(zdialog.choices) == 4)

    # Uncheck everything -> Export Selected hides; select 2 (1 received + 1 own PCM) -> Export Selected shows.
    for check in zdialog.checks.values():
        check.set_active(False)
    assert not zdialog.export_selected_button.get_visible()
    own_meeting = next(m for m in zdialog.choices if m.origin == "own")
    zdialog.checks[victim.key].set_active(True)
    zdialog.checks[own_meeting.key].set_active(True)
    assert zdialog.export_selected_button.get_visible()

    exports_dir = tmp / "ZipExports"
    exports_dir.mkdir()
    stage_entered, stage_release = threading.Event(), threading.Event()
    real_stage = zip_export._stage_single_meeting

    def held_stage(meeting, directory, max_bytes, **kwargs):
        assert threading.get_ident() != gtk_thread, "ZIP export staged on the GTK thread"
        stage_entered.set()
        assert stage_release.wait(30), "ZIP export stage gate never released"
        return real_stage(meeting, directory, max_bytes, **kwargs)

    with patch.object(zip_export, "_stage_single_meeting", held_stage):
        zdialog.export_selected_button.emit("clicked")
        chooser = ready_chooser()
        chooser.set_current_folder(Gio.File.new_for_path(str(exports_dir)))
        ready_chooser(exports_dir)
        chooser.response(Gtk.ResponseType.ACCEPT)
        del chooser
        try:
            until(stage_entered.is_set)
            assert not window.lookup_action("export-zip").get_enabled()
            assert zdialog.stack.get_visible_child_name() == "progress"
            assert zdialog.progress_label.get_label() == text("export.zip.progress", done=1, total=2)
        finally:
            stage_release.set()
        until(lambda: zdialog.closed and zdialog.output == exports_dir / "Cassini.zip")

    assert zdialog.success_toast.get_title() == plural("export.zip.done", 2)
    assert zdialog.success_toast.get_button_label() == text("common.show")
    with zipfile.ZipFile(exports_dir / "Cassini.zip") as exported_zf:
        infos = exported_zf.infolist()
        assert len(infos) == 2 and all(i.compress_type == zipfile.ZIP_STORED for i in infos)
        assert any(i.filename == "Own PCM Standup.opus" for i in infos)

    # Cancel an Export All mid-flight: cleans up partial stage and emits no error toast.
    before_messages = list(messages)
    stage_entered.clear()
    stage_release.clear()
    window.lookup_action("export-zip").activate(None)
    until(lambda: window.zip_export_dialog is not zdialog and window.zip_export_dialog.get_mapped())
    zdialog = window.zip_export_dialog
    with patch.object(zip_export, "_stage_single_meeting", held_stage):
        zdialog.export_all_button.emit("clicked")
        chooser = ready_chooser(exports_dir)
        chooser.response(Gtk.ResponseType.ACCEPT)
        del chooser
        try:
            until(stage_entered.is_set)
            until(lambda: zdialog.stack.get_visible_child_name() == "progress")
            zdialog.progress_cancel_button.emit("clicked")
            assert zdialog.cancelled.is_set() and zdialog.closed
        finally:
            stage_release.set()
        until(lambda: zdialog.future.done() and not list(exports_dir.glob(".cassini-*")))
    assert not (exports_dir / "Cassini 2.zip").exists()
    assert messages == before_messages

    # Export the 3 unedited received meetings into Cassini 2.zip and re-import -> 3 duplicates, 0 imported.
    window.lookup_action("export-zip").activate(None)
    until(lambda: window.zip_export_dialog is not zdialog and window.zip_export_dialog.get_mapped())
    zdialog = window.zip_export_dialog
    zdialog.checks[own_meeting.key].set_active(False)
    assert len(zdialog.selected_keys) == 3
    zdialog.export_selected_button.emit("clicked")
    chooser = ready_chooser(exports_dir)
    chooser.response(Gtk.ResponseType.ACCEPT)
    del chooser
    until(lambda: zdialog.closed and zdialog.output == exports_dir / "Cassini 2.zip")
    window.open_file(Gio.File.new_for_path(str(exports_dir / "Cassini 2.zip")))
    until(lambda: not window._opening_files and not window._pending_selection)
    assert messages[-1] == plural("import.summary.duplicates", 3)

    window._activity = {}
    live_session.finish()
    window.close()
    app.comfort.close()
print("PASS D96 picker batching, actual ZIP import/export, Trash pruning, independent monitor and cancellation")
