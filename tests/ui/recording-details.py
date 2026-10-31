#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D28 real dialog, live refresh, clipboard and three 1120x720 review boards."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
# Preserve all stderr, but fail only on Gtk diagnostics in a dialog-open scope.
# A parent process observes C/structured GTK logging as well as Python output.
SCOPE_BEGIN, SCOPE_END = "CASSINI_DETAILS_OPEN", "CASSINI_DETAILS_CLOSED"
if os.environ.get("CASSINI_DETAILS_TEST_CHILD") != "1":
    result = subprocess.run(
        [sys.executable, __file__],
        env=dict(os.environ, CASSINI_DETAILS_TEST_CHILD="1"),
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    opened, scopes, warnings = 0, 0, []
    for line in result.stderr.splitlines():
        if line == SCOPE_BEGIN:
            opened += 1
            scopes += 1
        elif line == SCOPE_END:
            opened -= 1
        elif opened and re.search(r"Gtk-(?:WARNING|CRITICAL)\b", line):
            warnings.append(line)
    if result.returncode:
        raise SystemExit(result.returncode)
    if warnings:
        raise SystemExit("Gtk warning/critical while Recording details was open:\n" + "\n".join(warnings))
    assert opened == 0, "Unbalanced dialog warning scope"
    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_name("recording-details-checks.json")
    checks = json.loads(target.read_text())
    checks.update(warningScopes=scopes, gtkDiagnostics=[])
    target.write_text(json.dumps(checks, indent=2) + "\n")
    print(f"PASS Gtk warning/critical guard: no diagnostics during {scopes} dialog-open scopes")
    raise SystemExit(0)


def begin_details():
    print(SCOPE_BEGIN, file=sys.stderr, flush=True)


def end_details():
    print(SCOPE_END, file=sys.stderr, flush=True)


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkX11", "4.0")
from details_fixture import build_library, build_many_spans, write_json  # noqa: E402
from gi.repository import Adw, Gdk, GdkX11, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.library import LibraryScanner  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402


def until(predicate):
    end = time.monotonic() + 12
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Recording details condition timed out")


def shot(window, name):
    frames = []
    window.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 3)[1])
    window.queue_draw()
    until(lambda: len(frames) >= 2)
    paintable, snapshot = Gtk.WidgetPaintable.new(window), Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert (texture.get_width(), texture.get_height()) == (1120, 720)
    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_name(f"recording-details-{name}.png")
    assert texture.save_to_png(str(target))


def escape(dialog):
    # Real XTest events in this run's private Xvfb, through GTK's dialog shortcut.
    xid = GdkX11.X11Surface.get_xid(dialog.window.get_surface())
    dialog.copy_button.grab_focus()
    subprocess.run(["xdotool", "windowfocus", "--sync", str(xid)], check=True)
    subprocess.run(["xdotool", "key", "--clearmodifiers", "Escape"], check=True)
    until(lambda: dialog.closed)


refresh_checks = []
heights = {}


def check_stable_refreshes(dialog, *, changing):
    first, bindings, report = dialog.body.get_first_child(), dict(dialog._bindings), dialog.report
    value = bindings[("writing", 0)]
    value.grab_focus()
    until(lambda: dialog.window.get_focus() == value)
    adjustment = dialog.scroller.get_vadjustment()
    adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
    position = adjustment.get_value()
    with patch.object(dialog, "_loaded", wraps=dialog._loaded) as loaded:
        until(lambda: loaded.call_count >= 3 and not dialog.busy)
        reports = [call.args[1] for call in loaded.call_args_list]
    assert all(r is not None for r in reports)
    if changing:
        assert any(r != report for r in reports), "Real recording did not advance"
    else:
        assert all(r == report for r in reports), "Saved report changed without a file change"
    assert dialog.body.get_first_child() == first
    assert dialog._bindings == bindings
    assert dialog.window.get_focus() == value
    assert abs(adjustment.get_value() - position) < 1
    refresh_checks.append(
        dict(
            live=changing,
            refreshes=len(reports),
            changed=any(r != report for r in reports),
            bodyRebuilds=0,
            bindingsReused=True,
            focusPreserved=True,
            scrollDelta=adjustment.get_value() - position,
        )
    )


with tempfile.TemporaryDirectory() as temporary:
    private = Path(temporary)
    paths = build_library(private / "Library")
    live = json.loads((paths["fallback"] / "session.json").read_text())
    live["state"] = "recording"
    for track in live["tracks"]:
        if track["kind"] in ("microphone", "system"):
            track["ended"] = False
    write_json(paths["fallback"] / "session.json", live)
    # Current production writes down after capture ends. A live fixture must
    # not pretend it already has a finished transcript or selected ASR model.
    live_metadata = json.loads((paths["fallback"] / "meeting.json").read_text())
    live_metadata["transcript"] = None
    write_json(paths["fallback"] / "meeting.json", live_metadata)
    (paths["fallback"] / "transcript.json").unlink()
    write_json(private / "activity.json", {live["id"]: "recording"})
    schemas = private / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(private / "Library"),
        CASSINI_SCREENSHOT_ACTIVITY=str(private / "activity.json"),
        CASSINI_SCREENSHOT_NOW="1793267225000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Gtk.Settings.get_default().props.gtk_font_name = "Cantarell 11"
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    app = CassiniApplication("com.gocassini.RecordingDetailsTest", "draft", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending and len(window.snapshot.meetings) == 3)
    copied = []
    for key in ("fallback", "microphone", "no-events"):
        meeting = next(m for m in window.snapshot.meetings if m.directory == paths[key])
        window.open_meeting(meeting)
        window._update_live()
        begin_details()
        if key == "fallback":
            assert window.recording_page
            window.recording_page.details_button.emit("clicked")
        else:
            assert window.meeting_page
            menu = window.meeting_page.more_menu
            assert any(
                menu.get_item_attribute_value(i, "label", None).unpack() == "Recording details…"
                for i in range(menu.get_n_items())
            )
            window.lookup_action("recording-details").activate(None)
        dialog = window.recording_details_dialog
        until(lambda dialog=dialog: dialog.report is not None)
        assert dialog.report.folder == str(paths[key])
        if key != "fallback":
            # Details and Trash share the Meeting menu after the main merge.
            # Keep Trash in its last section, and never delete through a modal.
            section = window.meeting_page.more_menu.get_item_link(
                window.meeting_page.more_menu.get_n_items() - 1, Gio.MENU_LINK_SECTION
            )
            assert section.get_item_attribute_value(0, "action", None).unpack() == "win.move-to-trash"
            assert window.lookup_action("move-to-trash").get_enabled()
            assert not window._trash_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
            assert paths[key].is_dir() and not app.meeting_trash.hides(paths[key])
        shot(window, {"fallback": "browser-fallback", "microphone": "microphone", "no-events": "no-events"}[key])
        heights[key] = dialog.get_child().get_height()
        body = dialog.report.plain_text()
        if key == "fallback":
            assert dialog.report.live and "3 audio streams" in body
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            shot(window, "browser-fallback-dark")
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            live["problemKind"] = "io"
            write_json(paths[key] / "session.json", live)
            until(lambda dialog=dialog: dict(dialog.report.health)["Reported issue"] == "io")
        elif key == "microphone":
            assert "The microphone adds a steady offset (-2060)." in body and "Stopped by you" in body
            opened = []

            def launch(launcher, _parent, _cancel, _callback, opened=opened):
                opened.append(launcher.get_file().get_path())

            with patch.object(Gtk.FileLauncher, "launch", launch):
                dialog.open_button.emit("clicked")
            assert opened == [str(paths[key])]
        else:
            assert "Source-change events were not recorded" in body
            dialog.copy_button.emit("clicked")
            clipboard = dialog.get_display().get_clipboard()
            clipboard.read_text_async(None, lambda c, r: copied.append(c.read_text_finish(r)))
            until(lambda: bool(copied))
            assert copied[-1] == body
        if key in ("fallback", "microphone"):
            escape(dialog)
        else:
            dialog.close()
        until(lambda dialog=dialog: dialog.closed)
        assert not dialog.timer
        end_details()
        window.back_to_library()
    # An opened folder may share a UUID/basename with a Library copy. Details
    # must stay bound to the actual external folder when the Library refreshes.
    outside = private / "Outside" / paths["microphone"].name
    shutil.copytree(paths["microphone"], outside)
    outside_metadata = json.loads((outside / "meeting.json").read_text())
    outside_metadata["title"] = "Opened outside this Library"
    from cassini_talk.nemotron_model import MODEL_ID, REVISION

    outside_metadata["recordingDetails"]["writingDown"].update(
        wallDurationMs=15680,
        resumed=True,
        speakerSeparation=dict(
            status="completed", modelId=MODEL_ID, modelRevision=REVISION, speakersFound=2, reused=False
        ),
    )
    write_json(outside / "meeting.json", outside_metadata)
    external = LibraryScanner(outside.parent).scan().meetings[0]
    assert external.key in {m.key for m in window.snapshot.meetings}
    window.open_meeting(external, external=True)
    begin_details()
    window.show_recording_details()
    dialog = window.recording_details_dialog
    until(lambda: dialog.report is not None)
    assert dialog.report.folder == str(outside)
    assert dialog.report.title == outside_metadata["title"]
    assert REVISION in dialog.report.plain_text()
    assert dict(dialog.report.writing)["Time taken"] == "15.68 s (this run, after resuming)"
    shot(window, "speaker-separation")
    assert not window.lookup_action("move-to-trash").get_enabled()
    window._updated(window._source_snapshot)
    assert dialog._current().directory == outside
    dialog.close()
    until(lambda: dialog.closed)
    end_details()
    window.back_to_library()
    many = build_many_spans(private / "Library", paths["microphone"])
    window.monitor.refresh_now()
    until(lambda: any(m.directory == many for m in window.snapshot.meetings))
    meeting = next(m for m in window.snapshot.meetings if m.directory == many)
    window.open_meeting(meeting)
    begin_details()
    window.show_recording_details()
    dialog = window.recording_details_dialog
    until(lambda: dialog.report is not None)
    assert len(dialog.report.spans) >= 40
    check_stable_refreshes(dialog, changing=False)
    shot(window, "many-spans")
    heights["many-spans"] = dialog.get_child().get_height()
    assert heights["microphone"] < heights["many-spans"], heights
    dialog.close()
    until(lambda: dialog.closed)
    end_details()
    window.back_to_library()

    # Real appsink/WAV recording, generated sources only; never host devices.
    app.transcription_enabled = False
    app.start_native(test=True)
    native = app.recorder
    until(lambda: native.session is not None and sum(w.frames for w in native.session.writers.values()) > 96000)
    directory = native.session.directory
    until(lambda: any(m.directory == directory and m.lifecycle == "recording" for m in window.snapshot.meetings))
    meeting = next(m for m in window.snapshot.meetings if m.directory == directory)
    window.open_meeting(meeting)
    begin_details()
    window.show_recording_details()
    dialog = window.recording_details_dialog
    until(lambda: dialog.report is not None and dialog.report.live)
    check_stable_refreshes(dialog, changing=True)
    shot(window, "live-generated")
    escape(dialog)
    assert native.pipeline is not None and native.session.manifest["state"] == "recording"
    until(lambda: dialog.closed)
    end_details()
    app.stop_native()
    until(lambda: native.pipeline is None and not native._finishing)
    from cassini_talk.capture_details import read_details

    terminal = [event for event in native.session.manifest.get("events", []) if event["type"] == "end"]
    assert len(terminal) == 1 and terminal[0]["reason"] == "user" and terminal[0]["atMs"] > 0
    saved = next(m for m in LibraryScanner(directory.parent).scan().meetings if m.directory == directory)
    assert read_details(saved).ended.startswith("Stopped by you · ")
    window.back_to_library()

    # Closing during a slow read must not retain a timer, touch closed widgets,
    # block GTK or take an application hold.
    from cassini_talk import capture_details_dialog

    entered, release = threading.Event(), threading.Event()
    original = capture_details_dialog.read_details

    def blocked(meeting):
        entered.set()
        assert release.wait(4)
        return original(meeting)

    with patch.object(capture_details_dialog, "read_details", blocked), patch.object(app, "hold") as hold:
        begin_details()
        window.show_recording_details()
        dialog = window.recording_details_dialog
        until(entered.is_set)
        dialog.close()
        until(lambda: dialog.closed)
        assert dialog.timer == 0 and dialog.report is None
        assert not hold.called
        release.set()
        end_details()
    window.monitor.close()
    window.destroy()
    app.quit()
target = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_name("recording-details-checks.json")
target.write_text(
    json.dumps(dict(refreshes=refresh_checks, contentHeights=heights, escapeLiveAndSaved=True), indent=2) + "\n"
)
print("PASS Details: menu/link, read-only facts, many spans, live capture, stable refresh/focus/scroll and screenshots")
