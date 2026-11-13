#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Open/drop import, Library selection and the two-format Export dialog."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
# GTK writes structured warnings to stderr; a parent checks them so callbacks
# cannot swallow an assertion and leave CI green. No warning suppression.
if "--gtk-child" not in sys.argv:
    result = subprocess.run([sys.executable, __file__, "--gtk-child"], capture_output=True, text=True, timeout=90)
    print(result.stdout, end="")
    print(result.stderr, file=sys.stderr, end="")
    if result.returncode or re.search(r"Gtk-(WARNING|CRITICAL)", result.stderr):
        raise SystemExit(result.returncode or 1)
    raise SystemExit(0)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from dispatch_wait import dispatch_until  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Graphene, Gst, Gtk  # noqa: E402
from painted import apply_screenshot_scheme, painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk import portable  # noqa: E402
from cassini_talk.exporting import export_meeting  # noqa: E402
from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.open_meeting import load_open_meeting  # noqa: E402
from cassini_talk.player import Player  # noqa: E402


def until(check):
    dispatch_until(check, "Import/export GUI did not complete")


def labels(widget, *, visible=False):
    values = [widget.get_text()] if isinstance(widget, Gtk.Label) and (not visible or widget.get_mapped()) else []
    child = widget.get_first_child()
    while child:
        values += labels(child, visible=visible)
        child = child.get_next_sibling()
    return values


def each_dispatch(check, timed_out=lambda: "Import/export GUI condition timed out"):
    """until() at single-dispatch resolution, bounded by the same 12 s: no chooser state is skipped."""
    expired = []
    bound = GLib.timeout_add_seconds(12, lambda: expired.append(True))
    context = GLib.MainContext.default()
    while not check():
        assert not expired, timed_out()
        context.iteration(True)
    if not expired:
        GLib.source_remove(bound)


def descendants(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


# shown_folder() reads GTK's private path bar: GtkPathBar (CSS name "pathbar") holds
# one ToggleButton per folder, with the shown folder's button active and labelled.
# Nothing public says when the chooser has shown its folder. If GTK rebuilds that
# bar, ready_chooser() fails at once or names this structure in its timeout.
PATH_BAR_STRUCTURE = "GTK's private chooser path bar (CSS 'pathbar' > active Gtk.ToggleButton > Gtk.Label)"


def path_bar(chooser):
    return next((w for w in descendants(chooser) if w.get_css_name() == "pathbar"), None)


def shown_folder(chooser):
    """The folder name the chooser's path bar has finished showing, or None while it resolves."""
    bar = path_bar(chooser)
    for button in descendants(bar) if bar else ():
        if isinstance(button, Gtk.ToggleButton) and button.get_active():
            return next((w.get_text() for w in descendants(button) if isinstance(w, Gtk.Label)), None)
    return None


def ready_chooser(folder=None):
    """The open chooser once its path bar shows its folder, as a person first sees it.

    GTK's path bar resolves each ancestor of the folder with its own async query and
    only then shows its buttons. A chooser destroyed inside that walk makes GTK
    remove the half-built buttons from its already disposed box (GTK 4.22
    gtkpathbar.c gtk_path_bar_set_file_finish: "gtk_box_remove: assertion
    'GTK_IS_BOX (box)' failed"); a new folder set inside it makes GTK remove buttons
    it never added ("gtk_widget_get_parent (child) == (GtkWidget *)box"). No person
    can answer, close or redirect the chooser in those milliseconds, so the test
    acts on it only after this event, never mid-walk.
    """
    each_dispatch(lambda: any(isinstance(w, Gtk.FileChooserDialog) for w in Gtk.Window.get_toplevels()))
    chooser = next(w for w in Gtk.Window.get_toplevels() if isinstance(w, Gtk.FileChooserDialog))
    # The chooser builds its path bar with itself, so a missing one is a GTK change.
    assert path_bar(chooser) is not None, f"{PATH_BAR_STRUCTURE} not found: update shown_folder()"

    def shown():
        current = chooser.get_current_folder()
        return (
            current is not None
            and (folder is None or current.get_path() == str(folder))
            and shown_folder(chooser) == current.get_basename()
        )

    def timed_out():
        current = chooser.get_current_folder()
        return (
            f"Chooser never showed {folder or 'its folder'}: current folder "
            f"{current.get_path() if current else None!r}, path bar shows {shown_folder(chooser)!r}. "
            f"If {PATH_BAR_STRUCTURE} changed, update shown_folder()"
        )

    each_dispatch(shown, timed_out)
    return chooser


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(tmp / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(tmp / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    original = load_open_meeting(ROOT / "ux/library-vectors/assets/onboarding.opus")
    incoming = export_meeting(original, tmp / "Downloads")
    before = hashlib.sha256(incoming.read_bytes()).hexdigest()
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
    apply_screenshot_scheme()
    app = CassiniApplication("com.gocassini.ImportExportTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending)
    initial_received = sum(m.origin == "received" for m in window.snapshot.meetings)
    ogg = incoming.with_suffix(".ogg")
    shutil.copyfile(incoming, ogg)
    app.open_in_new_window(Gio.File.new_for_path(str(ogg)))
    until(
        lambda: (
            window.selected
            and window.selected.origin == "received"
            and sum(m.origin == "received" for m in window.snapshot.meetings) == initial_received + 1
        )
    )
    received = window.selected
    assert (
        window.meeting_page is not None
        and window.meeting_page.meeting.key == received.key
        and received.directory.parent == Path(os.environ["CASSINI_LIBRARY_DIR"])
    )
    assert received.audio_file.read_bytes() == incoming.read_bytes()
    # L-57: the persistent folder card is gone; a one-time notice sits above the player instead.
    page = window.meeting_page
    assert not any("Opened from" in value for value in labels(page.lines))
    assert page.imported_toast.get_visible()
    assert page.imported_toast_label.get_label() == "Imported from Downloads. A copy is kept in your Meeting Library."
    assert received.key not in window._fresh_imports, "the notice is one-time: shown, then consumed"
    # Dismissing the focused notice hands focus to Play, or else into the transcript.
    page.imported_toast_close.grab_focus()
    assert window.get_focus() is page.imported_toast_close
    play_ready = page.play.get_visible() and page.play.get_sensitive()
    page.imported_toast_close.emit("clicked")
    assert not page.imported_toast.get_visible()
    if play_ready:
        assert window.get_focus() is page.play, window.get_focus()
    page.update(received)
    assert not page.imported_toast.get_visible(), "a dismissed notice never returns for the same meeting"

    def in_transcript():
        focus = window.get_focus()
        return focus is not None and focus is not page.list and focus.is_ancestor(page.list)

    def show_focused_notice():
        page._imported_toast_key = received.key
        page._sync_imported_toast(received)
        assert page.imported_toast.get_visible()
        page.imported_toast_close.grab_focus()
        assert window.get_focus() is page.imported_toast_close

    page.play.set_sensitive(False)
    show_focused_notice()
    page.imported_toast_close.emit("clicked")
    # ExactTranscript focuses an unallocated row on its next allocation.
    until(in_transcript)
    # A state change that hides the focused notice uses the same handoff.
    show_focused_notice()
    page._imported_toast_key = "another-meeting"
    page._sync_imported_toast(received)
    assert not page.imported_toast.get_visible()
    until(in_transcript)
    page.update(received)
    # A collapsed window keeps the notice for when the meeting is actually opened.
    window.split.set_collapsed(True)
    window.split.set_show_content(False)
    window._fresh_imports.add(received.key)
    page._sync_imported_toast(received)
    assert received.key in window._fresh_imports and not page.imported_toast.get_visible()
    window.split.set_show_content(True)
    assert received.key not in window._fresh_imports and page.imported_toast.get_visible()
    page._dismiss_imported_toast()
    window.split.set_collapsed(False)
    assert not window.received_hint.get_visible()
    until(lambda: not window._pending_selection)
    # L-25 DropImport states: rest, enter, over, unsupported error, and transient Importing… card.
    from cassini_talk.strings import text

    assert window.drop_footer_label.get_label() == text("import.drop.rest")
    window._set_drop_state("enter")
    assert "drag-enter" in window.drop_frame.get_css_classes()
    assert window.drop_footer_label.get_label() == text("import.drop.enter")
    window._set_drop_state("over")
    assert "drag-over" in window.drop_frame.get_css_classes() and window.drop_overlay.get_visible()
    assert window.drop_overlay_title.get_label() == text("import.drop.over")
    assert window.drop_overlay_detail.get_label() == text("import.drop.formats.current")
    bad_file = tmp / "voice-note.m4a"
    bad_file.write_bytes(b"unsupported")
    bad_drop = Gdk.FileList.new_from_array([Gio.File.new_for_path(str(bad_file))])
    assert not window._drop_files(None, bad_drop, 0, 0)
    assert window.drop_overlay.get_visible() and "error" in window.drop_overlay.get_css_classes()
    assert window.drop_overlay_title.get_label() == text("import.drop.cantImport", file="voice-note.m4a")
    assert window.drop_overlay_detail.get_label() == text(
        "import.drop.cantImport.detail", formats=text("import.drop.formats.current")
    )
    window._set_drop_state("rest")
    assert not window.drop_overlay.get_visible()
    # Extensionless file validation runs asynchronously via GIO without blocking GTK:
    extless_bad = tmp / "extensionless-blob"
    extless_bad.write_bytes(b"not-a-directory")
    extless_drop = Gdk.FileList.new_from_array([Gio.File.new_for_path(str(extless_bad))])
    assert window._drop_files(None, extless_drop, 0, 0)
    until(
        lambda: (
            window.drop_overlay.get_visible()
            and "error" in window.drop_overlay.get_css_classes()
            and window.drop_overlay_title.get_label() == text("import.drop.cantImport", file="extensionless-blob")
        )
    )
    window._set_drop_state("rest")
    assert not window.drop_overlay.get_visible()
    cached_before_import = dict(window._row_by_key)
    token_obj = object()
    window._begin_import_entries(token_obj, (str(incoming),))
    assert text("states.badge.importing") in labels(window.rows)
    assert all(window._row_by_key[k] is row for k, row in cached_before_import.items())
    window._finish_import_entries(token_obj)
    assert text("states.badge.importing") not in labels(window.rows)
    assert all(window._row_by_key[k] is row for k, row in cached_before_import.items())
    drop = Gdk.FileList.new_from_array([Gio.File.new_for_path(str(incoming))])
    assert window._drop_files(None, drop, 0, 0)
    until(lambda: not window._opening_files and not window._pending_selection)
    assert all(window._row_by_key[k] is row for k, row in cached_before_import.items())
    assert sum(m.origin == "received" for m in window.snapshot.meetings) == initial_received + 1
    uppercase = incoming.with_suffix(".OPUS")
    shutil.copyfile(incoming, uppercase)
    window.open_file(Gio.File.new_for_path(str(uppercase)))
    until(lambda: not window._opening_files and not window._pending_selection)
    assert sum(m.origin == "received" for m in window.snapshot.meetings) == initial_received + 1
    failures = []
    toast = window.toast
    window.toast = lambda message: (failures.append(message), toast(message))
    broken = tmp / "incomplete.opus"
    broken.write_bytes(incoming.read_bytes()[:-10])
    window.open_file(Gio.File.new_for_path(str(broken)))
    until(lambda: not window._opening_files)
    assert failures and app.get_windows() == [window], "A failed import opened the legacy app"
    assert not list(Path(os.environ["CASSINI_LIBRARY_DIR"]).glob(".received-*"))
    window.open_meeting(received)
    until(lambda: window.meeting_page is not None and window.player._prerolled)
    window.meeting_page.play.emit("clicked")
    until(lambda: window.player.playing and window.player.position > 0)
    window.monitor.refresh_now()
    assert window.player.playing
    window.meeting_page.play.emit("clicked")
    from unittest.mock import patch

    assert received.audio_size_bytes == received.audio_file.stat().st_size
    original_stat, gtk_thread = Path.stat, threading.get_ident()

    def background_stat(path, *args, **kwargs):
        assert threading.get_ident() != gtk_thread, "Export dialog performed GTK-thread stat"
        return original_stat(path, *args, **kwargs)

    with patch.object(Path, "stat", background_stat):
        window.show_export()
    until(lambda: window.export_dialog is not None)
    dialog = window.export_dialog
    assert dialog.cassini.get_active() and not dialog.markdown.get_active()
    assert dialog.names.get_text() == "The file carries the names in this meeting. Voiceprints are never exported."
    from cassini_talk.meeting_store import update_metadata

    update_metadata(
        received.directory,
        title="Updated while Export is open",
        titleSource="user",
        notes=[dict(atMs=2, text="New note while Export is open")],
    )
    window.monitor.refresh_now()
    until(lambda: dialog.meeting.title == "Updated while Export is open")
    assert dialog.meeting.notes == (dict(atMs=2, text="New note while Export is open"),)
    assert "Updated while Export is open" in dialog.get_title()
    assert dialog.heading.get_text() == dialog.get_title()

    assert dialog.destination is None, "Documents must be a suggestion, never an implicit export grant"
    destination = tmp / "Exports"
    destination.mkdir()
    window.toasts.dismiss_all()  # Earlier deliberate failure feedback was already checked.
    dialog.export_button.emit("clicked")
    chooser = ready_chooser()
    assert dialog.choosing and not hasattr(dialog, "future"), "No export job before the chooser grants a destination"
    chooser.set_current_folder(Gio.File.new_for_path(str(destination)))
    ready_chooser(destination)
    chooser.response(Gtk.ResponseType.ACCEPT)
    until(lambda: dialog.closed and dialog.output is not None)
    assert app.recording_settings.get_string("export-folder-uri") == destination.as_uri()
    assert dialog.success_toast.get_title() == "Exported “Updated while Export is open” to Exports"
    assert dialog.success_toast.get_button_label() == "Show"
    until(lambda: not dialog.get_mapped())
    until(lambda: "Exported “Updated while Export is open” to Exports" in labels(window.toasts, visible=True))
    ticks = []
    window.add_tick_callback(lambda *_: (ticks.append(True), len(ticks) < 3)[1])
    window.queue_draw()
    until(lambda: len(ticks) >= 3)
    node = painted_node(window)
    viewport = Graphene.Rect()
    viewport.init(5, 5, 1120, 720)
    texture = window.get_renderer().render_texture(node, viewport)
    assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
    exported = portable.read_meeting(str(dialog.output))
    assert portable.verify_audio(str(dialog.output), exported).state == portable.State.OK
    assert len(portable.load_words(exported, exported.default_transcript_id)) == 2
    assert exported.manifest["meeting"]["title"] == "Updated while Export is open"
    assert exported.manifest["x"]["gocassini.com"]["notes"] == [dict(atMs=2, text="New note while Export is open")]
    # The same GAction serves the header, menu and Ctrl+Shift+E.
    window.lookup_action("export").activate(None)
    until(lambda: window.export_dialog is not dialog)
    dialog = window.export_dialog
    assert dialog.destination.get_uri() == destination.as_uri(), "A later export must remember the granted URI"
    dialog.markdown.set_active(True)
    dialog.choose.emit("clicked")
    chooser = ready_chooser(destination)
    selected = tmp / "Chosen export folder"
    selected.mkdir()
    chooser.set_current_folder(Gio.File.new_for_path(str(selected)))
    ready_chooser(selected)
    chooser.response(Gtk.ResponseType.ACCEPT)
    until(lambda: not dialog.choosing)
    assert dialog.folder.get_text() == "Save to Chosen export folder" and not list(selected.iterdir())
    dialog.export_button.emit("clicked")
    until(lambda: dialog.closed and dialog.output is not None)
    assert dialog.output.suffix == ".md" and "## Transcript" in dialog.output.read_text()
    assert not any(isinstance(w, Gtk.FileChooserDialog) for w in Gtk.Window.get_toplevels()), (
        "Choose must not add a second picker"
    )
    shown = []
    with patch("cassini_talk.export_dialog.Gtk.FileLauncher") as factory:
        factory.return_value.open_containing_folder.side_effect = lambda *_: shown.append(
            factory.call_args.kwargs["file"].get_path()
        )
        dialog.success_toast.emit("button-clicked")
    assert shown == [str(dialog.output)], "Show must reveal the published file"
    window.back_to_library()
    budget = next(m for m in window.snapshot.meetings if m.title == "Budget review")
    window.open_meeting(budget)
    window.show_export()
    dialog = window.export_dialog
    assert "<b>Dario</b>" in dialog.names.get_label() and dialog.names.get_label().count("<b>") == 1
    dialog.close()
    until(lambda: dialog.closed)
    many = replace(
        budget,
        speakers=tuple(
            replace(s, name=name, provenance="recognised")
            for s, name in zip(budget.speakers, ("Marta", "Dario & Co", "Ana"), strict=True)
        ),
    )
    window.meeting_page.update(many)
    window.show_export()
    dialog = window.export_dialog
    assert "Marta, Dario & Co and Ana, who were recognised automatically" in dialog.names.get_text()
    assert dialog.names.get_label().count("<b>") == 3 and "&amp;" in dialog.names.get_label()
    dialog.close()
    until(lambda: dialog.closed)
    # Dismissing a picker keeps Export usable, with no write or lost selection.
    window.show_export()
    dialog = window.export_dialog
    previous_uri = app.recording_settings.get_string("export-folder-uri")
    dialog.choose.emit("clicked")
    chooser = ready_chooser(selected)
    chooser.response(Gtk.ResponseType.CANCEL)
    del chooser  # GTK alone owns a closed chooser, as in the app.
    until(lambda: not dialog.choosing)
    assert not dialog.closed and dialog.export_button.get_sensitive() and not hasattr(dialog, "future")
    assert app.recording_settings.get_string("export-folder-uri") == previous_uri
    # Cancelling Export itself must also close its outstanding native chooser,
    # at each main-loop step after the chooser is shown. Closed inside GTK's own
    # path-bar walk instead (no ready_chooser), some step makes GTK print the
    # gtk_box_remove criticals the stderr check above fails on.
    for steps in range(10):
        if steps:
            window.show_export()
            dialog = window.export_dialog
        dialog.choose.emit("clicked")
        ready_chooser(selected)
        for _ in range(steps):
            GLib.MainContext.default().iteration(False)
        dialog.cancel()
        until(
            lambda dialog=dialog: (
                dialog.closed and not any(isinstance(w, Gtk.FileChooserDialog) for w in Gtk.Window.get_toplevels())
            )
        )
    # Cancel through the actual dialog close path while a real long PCM export
    # is running; no mocked encoder, output or user audio device.
    window.show_export()
    dialog = window.export_dialog
    cancelled_destination = tmp / "Cancelled export"
    dialog.destination = Gio.File.new_for_path(str(cancelled_destination))
    dialog.export_button.emit("clicked")
    until(lambda: bool(list(cancelled_destination.glob(".cassini-*"))))
    dialog.close()
    assert dialog.cancelled.is_set(), "Escape/close did not cancel until its animation finished"
    until(lambda: dialog.closed and not list(cancelled_destination.iterdir()))
    assert hashlib.sha256(incoming.read_bytes()).hexdigest() == before
    window.back_to_library()
    window._fixture_activity = window._activity = {}
    window.monitor.capture_activity = lambda: {}
    window.close()
    app.comfort.close()
print(
    "PASS Open/drop import and dedupe select Library; folder card; playback; Cassini/Markdown export; complete, escaped recognised-name disclosure"
)
