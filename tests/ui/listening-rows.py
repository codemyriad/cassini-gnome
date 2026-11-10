#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private GTK row playback, read-only access, caption fit and focus."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
gi.require_version("GdkX11", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, GdkX11, Gio, GLib, Graphene, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from listening_input import key  # noqa: E402

from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.i18n import LANGUAGES, using_language  # noqa: E402
from cassini_talk.library import PreviewTurn  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_store import atomic_json, update_metadata  # noqa: E402
from cassini_talk.meeting_text import reading_turns, speaker_turns  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402
from cassini_talk.speaker_merges import apply_merges  # noqa: E402
from cassini_talk.strings import text  # noqa: E402


def until(window, check):
    if check():
        return
    loop = GLib.MainLoop()
    expired = False

    def frame(*_):
        if check():
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def timeout():
        nonlocal expired
        expired = True
        loop.quit()
        return GLib.SOURCE_REMOVE

    callback = window.add_tick_callback(frame)
    timer = GLib.timeout_add_seconds(8, timeout)
    window.queue_draw()
    loop.run()
    if expired:
        window.remove_tick_callback(callback)
    else:
        GLib.source_remove(timer)
    assert not expired and check(), "GTK row did not settle"


def focus_event(window, target, action):
    """Observe GTK focus entering the actual (possibly rebuilt) control."""
    loop = GLib.MainLoop()
    entered, controllers = [], []

    def attach(*_):
        widget = target()
        if widget is None or any(w is widget for w, _c in controllers):
            return
        controller = Gtk.EventControllerFocus()
        controller.connect("enter", lambda *_: (entered.append(widget), loop.quit()))
        widget.add_controller(controller)
        controllers.append((widget, controller))

    attach()
    rebuilt = window.meeting_page.connect("rows-rebuilt", attach)
    expired = []
    timer = GLib.timeout_add_seconds(8, lambda: (expired.append(True), loop.quit(), False)[-1])
    try:
        action()
        if not entered:
            loop.run()
        assert not expired and entered and target().has_focus(), "Focus did not return to the invoking control"
    finally:
        if not expired:
            GLib.source_remove(timer)
        window.meeting_page.disconnect(rebuilt)
        for widget, controller in controllers:
            widget.remove_controller(controller)


def dispatch_until(check):
    """Wait for a worker completion while the window is hidden, before mapping rows."""
    expired = []
    timer = GLib.timeout_add_seconds(8, lambda: expired.append(True) or False)
    try:
        while not check():
            assert not expired, "the actual direct Open completion did not arrive"
            GLib.MainContext.default().iteration(True)
    finally:
        if not expired:
            GLib.source_remove(timer)


def labels(widget):
    if isinstance(widget, Gtk.Label):
        yield widget
    child = widget.get_first_child()
    while child:
        yield from labels(child)
        child = child.get_next_sibling()


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    library = root / "Library"
    session = RecordingSession(library, source="gnome")
    session.add_track("system", None, "system", 16000, 1)
    for chunk in range(8):
        session.append("system", bytes(16000 * 2 * 5), chunk * 5000)
    session.finish()
    # One long voice and thirteen brief, indistinguishably named voices.
    items = []
    for index in range(127):
        items.extend(
            [
                dict(speaker="host", startMs=index * 150, endMs=index * 150 + 140, text="Planning."),
                dict(speaker="voice-0", startMs=index * 150 + 140, endMs=index * 150 + 141, text="Yes."),
            ]
        )
    # An interjection separates the final voice-0 turn for real EOS auto-advance.
    items.append(dict(speaker="voice-1", startMs=20500, endMs=20600, text="Ready."))
    items += [
        dict(speaker=f"voice-{i}", startMs=22000 + i * 1000, endMs=22100 + i * 1000, text="Yes.") for i in range(13)
    ]
    atomic_json(session.directory / "transcript.json", dict(format="cassini.words.v1", language="en", items=items))
    update_metadata(
        session.directory,
        title="Planning discussion",
        titleSource="user",
        speakers=[
            dict(id="host", name="Claire Lee", provenance="user", colourIndex=0),
            *[
                dict(id=f"voice-{i}", name="Unidentified voice", provenance="user", colourIndex=i + 1)
                for i in range(13)
            ],
        ],
        transcript=dict(
            status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        ),
    )
    other = RecordingSession(library, "Unrelated meeting", source="gnome")
    other.add_track("system", None, "system", 16000, 1)
    other.append("system", bytes(3200), 0)
    other.finish()
    atomic_json(other.directory / "transcript.json", dict(format="cassini.words.v1", items=[]))
    update_metadata(
        other.directory,
        transcript=dict(
            status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        ),
    )
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    activity = root / "activity.json"
    activity.write_text("{}")
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(library), CASSINI_SCREENSHOT_ACTIVITY=str(activity)
    )
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    app = CassiniApplication("com.gocassini.ListeningRowsTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(window, lambda: not window._pending)
    window.player.shutdown()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player = FolderPlayer(audio_sink=sink)
    window._connect_player()
    meeting = next(m for m in window.snapshot.meetings if m.id == session.manifest["id"])
    # Real on-disk changes: a future metadata schema is read-only, while its
    # capture title remains readable. Keep the real monitor running throughout.
    original_metadata = json.loads((meeting.directory / "meeting.json").read_text())
    original_session = json.loads((meeting.directory / "session.json").read_text())
    atomic_json(meeting.directory / "meeting.json", dict(original_metadata, version=2))
    atomic_json(meeting.directory / "session.json", dict(original_session, title="Fresh direct-open metadata"))
    window.set_visible(False)
    window.open_file(Gio.File.new_for_path(str(meeting.directory / "session.json")))
    dispatch_until(lambda: window._opening_files == 0)
    assert window.meeting_page and not window._external_meeting
    fresh = window.meeting_page.meeting
    assert not fresh.metadata_editable and fresh.title == "Fresh direct-open metadata"
    direct_page = window.meeting_page
    direct_ready = []
    direct_page.connect("rows-rebuilt", lambda *_: direct_ready.append(True))
    window.present()
    until(window, lambda: direct_ready and window.player._prerolled)
    until(
        window,
        lambda: (
            window.monitor._future is None
            and not window.monitor._timer
            and not window.monitor._pending_dirs
            and not window.monitor._pending_full
        ),
    )
    incremental = []
    scan = window.monitor.scanner.scan

    def observed_scan(**kwargs):
        snapshot = scan(**kwargs)
        if kwargs.get("changed_directories") == {other.directory}:
            incremental.append(snapshot)
        return snapshot

    window.monitor.scanner.scan = observed_scan
    window.monitor.refresh({other.directory})
    until(window, lambda: incremental and window.monitor._future is None)
    retained = next(m for m in incremental[0].meetings if m.id == meeting.id)
    assert retained.title == fresh.title and not retained.metadata_editable
    assert meeting.directory in window.monitor._directories
    assert window.meeting_page is direct_page
    direct_play = direct_page._speaker_rows["voice-0"][1]
    assert direct_play.activate()
    window._activity_tick()
    assert window.meeting_page is direct_page
    assert not direct_page.meeting.metadata_editable, "The tick reapplied metadata older than the direct Open result"
    assert direct_page.meeting.title == fresh.title
    assert direct_page._speaker_rows["voice-0"][1] is direct_play and direct_play.get_realized()
    until(window, lambda: window.listen.active)
    assert window.listen.speaker == "voice-0"
    window.player.pause()
    window.listen.stop()
    # Restore the editable fixture on disk and let the real scanner deliver it.
    atomic_json(meeting.directory / "meeting.json", original_metadata)
    atomic_json(meeting.directory / "session.json", original_session)
    window.monitor.refresh_now()
    until(window, lambda: next(m for m in window.snapshot.meetings if m.id == meeting.id).metadata_editable)
    meeting = next(m for m in window.snapshot.meetings if m.id == meeting.id)
    # Also cover activation before GTK realizes the row: audio/row data
    # can already be ready. Gtk.Button.activate() still returns True then, but
    # schedules no click at all (gtk_real_button_activate checks realized).
    window.set_visible(False)
    window.open_meeting(meeting)
    page = window.meeting_page
    unready_play = page._speaker_rows["voice-12"][1]
    unready_clicks = []
    unready_play.connect("clicked", lambda *_: unready_clicks.append(True))
    assert window.player.loaded and not unready_play.get_realized()
    assert unready_play.activate()
    assert not unready_clicks and not window.listen.active
    rebuilt = []
    page.connect("rows-rebuilt", lambda *_: rebuilt.append(dict(page._speaker_rows)))
    window.present()
    until(window, lambda: rebuilt and window.player._prerolled and len(page._speaker_rows) == 14)
    # Widget assertions belong in this initialized, display-backed journey.
    bar = page.listening_bar
    before_ticks = page.timeline.measure(Gtk.Orientation.VERTICAL, -1)
    assert window.listen.start("host", 0, play=False)
    assert len(page.timeline._target) == 127
    assert page.timeline.measure(Gtk.Orientation.VERTICAL, -1) == before_ticks
    marks = Gtk.Snapshot.new()
    page.timeline.draw_ticks(marks, page.seek.get_height())
    nodes = marks.to_node()
    assert nodes.get_n_children() == 127
    assert all(nodes.get_child(i).get_bounds().get_width() == 3 for i in range(127))
    assert all(nodes.get_child(i).get_bounds().get_height() == 8 for i in range(127))
    count = len(window.listen.turns_of("voice-0"))
    assert window.listen.start("voice-0", 0, play=False)
    until(window, lambda: bar.get_child_revealed())
    assert bar.get_reveal_child() and bar.name.get_label() == "Unidentified voice"
    assert not bar.buttons["previousTurn"].get_sensitive()
    assert bar.buttons["nextTurn"].get_sensitive()
    assert window.listen.start("voice-0", count - 1, play=False)
    assert bar.buttons["previousTurn"].get_sensitive()
    assert not bar.buttons["nextTurn"].get_sensitive()
    window.player.seek(window.listen.span_end(count - 1) - 50)
    window.player.play()
    until(window, lambda: window.listen.finished)
    assert bar.buttons["again"].get_visible()
    assert bar.buttons["again"].get_label() == text("listen.again")
    # Paused Stop has no playback-position notification to refresh controls.
    assert window.listen.start("voice-0", 1, play=False)
    assert not window.player.playing and bar.buttons["previousTurn"].get_sensitive()
    window.listen.stop()
    assert all(
        not bar.buttons[key].get_sensitive() for key in ("previousTurn", "nextTurn", "previousSpeaker", "nextSpeaker")
    )
    assert not bar.actions.lookup_action("previous-speaker").get_enabled()
    assert not bar.actions.lookup_action("next-speaker").get_enabled()
    until(window, lambda: not bar.get_child_revealed())
    assert not bar.actions.lookup_action("name").get_enabled()
    # Name from the bar pauses listening and focuses the sample, preserving
    # the pane's original entry-first dialog path.
    window.listen.start("voice-10", 0, play=False)
    page.listening_bar.name_speaker()
    dialog = page.speakers_dialog
    until(window, lambda: dialog.play.has_focus())
    assert not window.player.playing
    assert dialog.sample_from.get_text() == text("speakers.sampleFrom", position="00:00:32")
    focus_event(window, lambda: page.listening_bar.buttons["name"], dialog.close)
    window.listen.stop()

    # Hold the dialog's restoration request as a busy main loop can, then
    # deliver its actual idle callback after Stop and a new Play focus choice.
    def check_delayed_name_return(stop_first):
        window.listen.start("voice-10", 0, play=False)
        until(window, lambda: bar.get_child_revealed())
        deferred_focus = []
        queue_focus = page._queue_dialog_focus
        page._queue_dialog_focus = lambda *_: deferred_focus.append(True)
        try:
            page.listening_bar.name_speaker()
            deferred_dialog = page.speakers_dialog
            until(window, lambda: deferred_dialog.play.has_focus())
            deferred_dialog.close()
            until(
                window,
                lambda: (
                    deferred_focus
                    and deferred_dialog.closed
                    and not deferred_dialog.get_mapped()
                    and window.get_visible_dialog() is None
                ),
            )
            if stop_first:
                window.listen.stop()
            assert window.listen.start("voice-12", 0, play=False)
            until(window, lambda: bar.get_child_revealed())
            new_play = page._speaker_rows["voice-12"][1]
            assert new_play.grab_focus()
        finally:
            page._queue_dialog_focus = queue_focus
        delivered_focus = []

        def deliver_old_focus():
            page._restore_dialog_focus()
            delivered_focus.append(True)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver_old_focus)
        until(window, lambda: delivered_focus)
        assert window.get_focus() is new_play, (
            "An old naming dialog stole the new Play focus",
            window.get_focus() is bar.buttons["name"],
            window.get_focus().get_tooltip_text(),
            stop_first,
        )
        window.listen.stop()

    for stop_first in (True, False):
        check_delayed_name_return(stop_first)
    # Real merge, filesystem observation, paused next-unnamed cue and Undo.
    original_metadata = json.loads((meeting.directory / "meeting.json").read_text())
    updated_speakers = [
        dict(s, name=None, provenance="unnamed") if s["id"] in ("voice-10", "voice-11") else s
        for s in original_metadata["speakers"]
    ]
    update_metadata(meeting.directory, speakers=updated_speakers)
    window.monitor.refresh_now()
    until(
        window,
        lambda: (
            any(s.id == "voice-11" and s.provenance == "unnamed" for s in page.meeting.speakers)
            and not page._rows_rebuilt_tick
        ),
    )
    assert window.listen.start("voice-10", 0, play=False)
    page.listening_bar.name_speaker()
    dialog = page.speakers_dialog
    until(window, lambda: dialog.play.has_focus())
    focus_event(
        window,
        lambda: page._speaker_rows["host"][0].get_child(),
        lambda: dialog._merge("host"),
    )
    assert "voice-10" not in page._speaker_rows
    assert window.listen.speaker == "voice-11" and window.listen.cued and not window.player.playing
    page.undo_speaker_merge()
    until(window, lambda: "voice-10" in page._speaker_rows and page._speaker_future is None)
    # A collapsed sidebar must reopen before its replacement row can map.
    window.set_default_size(360, 650)
    until(window, lambda: page.split.get_collapsed() and page.listening_bar.get_width() == 360)
    page.split.set_show_content(True)
    assert window.listen.start("voice-10", 0, play=False)
    page.listening_bar.name_speaker()
    dialog = page.speakers_dialog
    until(window, lambda: dialog.play.has_focus())
    focus_event(window, lambda: page._speaker_rows["host"][0].get_child(), lambda: dialog._merge("host"))
    assert not page.split.get_show_content()
    assert window.listen.speaker == "voice-11" and window.listen.cued and not window.player.playing
    page.undo_speaker_merge()
    window.set_default_size(900, 650)
    until(
        window,
        lambda: "voice-10" in page._speaker_rows and page._speaker_future is None and not page.split.get_collapsed(),
    )
    assert window.listen.start("voice-10", 0, play=False)
    page.listening_bar.stop.grab_focus()
    focus_event(window, lambda: page.listening_bar.buttons["again"], lambda: page.merge_speaker("voice-10", "host"))
    until(
        window,
        lambda: "voice-10" not in page._speaker_rows and window.listen.speaker == "voice-11" and window.listen.cued,
    )
    assert not window.player.playing
    assert page.listening_bar.buttons["again"].get_visible()
    assert page.listening_bar.buttons["again"].has_focus()
    page.undo_speaker_merge()
    until(window, lambda: "voice-10" in page._speaker_rows and page._speaker_future is None)
    # Direct bar merge must hand off with the collapsed sidebar still hidden.
    window.set_default_size(360, 650)
    until(window, lambda: page.split.get_collapsed() and page.listening_bar.get_width() == 360)
    page._listen_speaker("voice-10")
    until(window, lambda: page.split.get_show_content() and not page.speakers.get_mapped())
    bar.stop.grab_focus()
    focus_event(window, lambda: bar.buttons["again"], lambda: page.merge_speaker("voice-10", "host"))
    assert "voice-10" not in page._speaker_rows
    assert window.listen.speaker == "voice-11" and window.listen.cued and not window.player.playing
    assert page.split.get_show_content() and not page.speakers.get_mapped()
    assert page._merge_transition is None and not page._rows_rebuilt_tick
    page.undo_speaker_merge()
    window.set_default_size(900, 650)
    until(
        window,
        lambda: "voice-10" in page._speaker_rows and page._speaker_future is None and not page._rows_rebuilt_tick,
    )
    window.listen.stop()
    update_metadata(meeting.directory, speakers=original_metadata["speakers"])
    window.monitor.refresh_now()
    until(window, lambda: all(s.provenance == "user" for s in page.meeting.speakers) and not page._rows_rebuilt_tick)
    meeting = page.meeting

    # Deliver actual X11 keys in the private display; observe controller
    # requests instead of racing the short turns' automatic playback ticks.
    def press(sequence):
        key(window, sequence, until)

    def drained():
        done = []
        GLib.idle_add(lambda: done.append(True) or False)
        until(window, lambda: done)

    requests = []
    handler = window.listen.connect(
        "requested", lambda *_: requests.append((window.listen.speaker, window.listen.index))
    )
    window.listen.start("voice-0", 0, play=False)
    page.listening_bar.stop.grab_focus()
    requests.clear()
    press("alt+Down")
    until(window, lambda: requests)
    window.player.pause()
    assert requests[0] == ("voice-0", 1)
    requests.clear()
    press("alt+Up")
    until(window, lambda: requests)
    window.player.pause()
    assert requests[0] == ("voice-0", 0)
    window.listen.start("voice-10", 0, play=False)
    requests.clear()
    press("alt+shift+Down")
    until(window, lambda: requests)
    window.player.pause()
    assert requests[0] == ("voice-11", 0)
    requests.clear()
    press("alt+shift+Up")
    until(window, lambda: requests)
    window.player.pause()
    assert requests[0] == ("voice-10", 0)
    entry = Gtk.Entry()
    page.get_child().prepend(entry)
    window.protect_text_input(entry)
    entry.grab_focus()
    until(window, lambda: window._text_focused())
    requests.clear()
    assert not page.listening_keys.actions.lookup_action("next-turn").get_enabled()
    press("alt+Down")
    drained()
    assert not requests and window.listen.active
    # GTK may use Alt+Down for normal focus traversal. Test Esc with the
    # entry focused, independently of that platform navigation behaviour.
    entry.grab_focus()
    until(window, lambda: window._text_focused())
    press("Escape")
    drained()
    assert not requests and window.listen.active, (
        requests,
        window.listen.active,
        type(window.get_focus()).__name__,
        entry.get_mapped(),
        entry.get_parent(),
    )
    page.get_child().remove(entry)
    page.listening_bar.stop.grab_focus()
    page.listening_bar.name_speaker()
    until(window, lambda: page.speakers_dialog.play.has_focus())
    press("Escape")
    until(window, lambda: page.speakers_dialog.closed and window.get_visible_dialog() is None)
    assert window.listen.active
    page.listening_bar.merge.popup()
    until(window, lambda: page.listening_bar.merge.get_popover().get_mapped())
    press("Escape")
    until(window, lambda: not page.listening_bar.merge.get_popover().get_mapped())
    assert window.listen.active
    window.set_default_size(600, 650)
    until(window, lambda: page.split.get_collapsed() and page.split.get_show_content())
    press("F9")
    until(window, lambda: not page.split.get_show_content())
    press("Escape")
    until(window, lambda: page.split.get_show_content())
    assert window.listen.active
    press("Escape")
    until(window, lambda: not window.listen.active)
    assert window.meeting_page is page
    for dead in (
        "nudge-forward",
        "next-match",
        "previous-match",
        "show-sidebar",
        "show-properties",
        "follow-along",
        "show-transcript",
        "show-summary",
    ):
        assert not app.get_accels_for_action("win." + dead)
    window.listen.disconnect(handler)
    window.set_default_size(1120, 720)
    until(window, lambda: not page.split.get_collapsed())
    app.on_shortcuts()
    shortcut_dialog = app.shortcuts_dialog
    expected_shortcuts = {
        text("shortcuts.listen.turns"),
        text("shortcuts.listen.speakers"),
        text("shortcuts.playTurn"),
        text("listen.stop"),
        text("player.back10"),
        text("player.forward10"),
    }
    until(window, lambda: expected_shortcuts <= {value.get_text() for value in labels(shortcut_dialog)})
    shortcut_dialog.close()
    until(window, lambda: app.shortcuts_dialog is None and window.get_visible_dialog() is None)
    # The source-row click assertions below require the previously closed dialog
    # identity, so retain the latest dialog produced by the key journey.
    dialog = page.speakers_dialog
    window.monitor.close()
    row, play, when = page._speaker_rows["voice-12"]
    assert when.endswith("00:00:34")
    assert any(value.get_text() == text("meeting.speakerShare.underOne") for value in labels(row))
    # Play is an overlay sibling of the naming button, never nested in it.
    assert isinstance(row.get_child(), Gtk.Button) and play.get_parent() is row
    play.grab_focus()
    until(window, lambda: play.get_mapped() and play.get_sensitive() and play.get_width() > 0)
    assert play.get_realized(), "A mapped button must be realized before activation"
    assert play.activate()
    # A monitor scan can finish during GTK's delayed keyboard activation.
    # Equal freshly parsed words must preserve the live button and its click.
    fresh = replace(meeting, words=tuple(list(meeting.words)))
    assert fresh.words == meeting.words and fresh.words is not meeting.words
    page.update(fresh)
    assert page._speaker_rows["voice-12"][1] is play
    assert play.get_realized()
    until(window, lambda: window.listen.active)
    assert window.listen.speaker == "voice-12" and page.speakers_dialog is dialog and dialog.closed
    timeline = page.timeline
    assert timeline.readout_at(34150) == "00:00:34"
    assert timeline.readout_at(34050) == text("player.timeline.tooltip", position="00:00:34", name="Unidentified voice")
    assert len(timeline._target) == 1 and timeline._target[0].start_ms == 34000
    assert window.get_focus() is play
    window.player.pause()
    left, track_width = timeline._geometry()
    point = Graphene.Point()
    point.init(round(left + 34000 * track_width / 40000) + 4, page.seek.get_height() / 2)
    ok, pointer = page.seek.compute_point(window, point)
    assert ok
    dx, dy = window.get_surface_transform()
    xid = str(GdkX11.X11Surface.get_xid(window.get_surface()))
    pointer_events = []
    for signal in ("drag-begin", "drag-update", "drag-end", "cancel"):
        timeline.gesture.connect(
            signal, lambda _g, *args, signal=signal: pointer_events.append((signal, args, timeline._dragging))
        )
    # Real GDK pointer delivery, four pixels from the drawn tick. Without the
    # capture handler a normal range click would seek beyond this exact start.
    subprocess.run(
        [
            "xdotool",
            "mousemove",
            "--window",
            xid,
            str(round(pointer.x + dx)),
            str(round(pointer.y + dy)),
            "click",
            "1",
        ],
        check=True,
        timeout=5,
    )
    until(window, lambda: window.player.position == 34000 and not timeline._dragging)
    assert window.listen.active and not window.player.playing
    # Complete the timestamp/slider allocation before constructing a second
    # native sequence. Frame notifications, not a delay, establish readiness.
    frames = []
    window.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 2)[1])
    until(window, lambda: len(frames) >= 2)
    left, track_width = timeline._geometry()
    point.init(round(left + 34000 * track_width / 40000) + 4, page.seek.get_height() / 2)
    ok, pointer = page.seek.compute_point(window, point)
    assert ok
    end_x = round(left + track_width / 2)
    end = Graphene.Point()
    end.init(end_x, page.seek.get_height() / 2)
    ok, endpoint = page.seek.compute_point(window, end)
    assert ok
    subprocess.run(
        [
            "xdotool",
            "mousemove",
            "--window",
            xid,
            str(round(pointer.x + dx)),
            str(round(pointer.y + dy)),
            "mousedown",
            "1",
            "mousemove",
            "--window",
            xid,
            str(round(endpoint.x + dx)),
            str(round(endpoint.y + dy)),
            "mouseup",
            "1",
        ],
        check=True,
        timeout=5,
    )
    expected = round((end_x - left) * 40000 / track_width)
    try:
        until(window, lambda: window.player.position == expected and not timeline._dragging)
    except AssertionError:
        print(
            dict(
                expected=expected,
                position=window.player.position,
                dragging=timeline._dragging,
                dragged=timeline._dragged,
                press=timeline._press_x,
                x=timeline._x,
                endpoint=end_x,
                geometry=timeline._geometry(),
                events=pointer_events,
            ),
            flush=True,
        )
        raise
    assert window.listen.active and not window.player.playing
    assert not timeline.drag_popup.get_visible()
    window.player.seek(34000)
    assert window.get_focus() is page.seek
    play.grab_focus()
    # Listening targets update only the current turn's affected text rows.
    until(window, lambda: any(words.has_css_class("listen-current") for _t, words, _r in page.bound.values()))
    target = next(t for t in page.meeting.turns if t.speaker == "voice-12")
    until(window, lambda: target.index in page.bound)
    _turn, words, transcript_row = page.bound[target.index]
    assert words.has_css_class("listen-current")
    name_button = transcript_row.get_first_child().get_first_child()
    assert isinstance(name_button, Gtk.Button)
    assert name_button.get_tooltip_text() == text("listen.speakerName.tooltip", name="Unidentified voice")
    window.player.pause()
    transcript_row.get_first_child().get_last_child().emit("clicked")
    assert window.player.playing and window.player.position == target.start_ms
    assert window.get_focus() is play
    window.player.pause()
    starts = []
    playing = window.player.connect(
        "notify::playing", lambda player, _p: starts.append(player.position) if player.playing else None
    )
    page.seek.grab_focus()  # Anchor outside the transcript; enter it using Tab.
    for _ in range(24):
        before = window.get_focus()
        key(window, "shift+Tab", until)
        until(window, lambda before=before: window.get_focus() is not before)
        focused = window.get_focus()
        if any(focused is row.get_parent() for _t, _w, row in page.bound.values()):
            break
    else:
        raise AssertionError("Tab did not reach the transcript list item")
    assert not any(row.get_focusable() for _t, _w, row in page.bound.values())
    before = window.get_focus()
    key(window, "Up", until)
    until(window, lambda: window.get_focus() is not before)
    focused = window.get_focus()
    keyboard_turn = next(t for t, _w, row in page.bound.values() if focused is row.get_parent())
    key(window, "shift+Return", until)
    until(window, lambda: starts)
    assert starts[0] == keyboard_turn.start_ms
    window.player.pause()
    starts.clear()
    assert window.get_focus() is focused
    # A different focus widget can be an intermediate GTK list transition.
    # Observe entry into the intended header, rather than any focus change.
    focus_event(
        window,
        lambda: page.bound[keyboard_turn.index][2].get_first_child().get_first_child(),
        lambda: key(window, "Tab", until),
    )
    focus = window.get_focus()
    inside_turn = keyboard_turn
    key(window, "shift+KP_Enter", until)
    until(window, lambda: starts)
    assert starts[0] == inside_turn.start_ms
    assert window.get_focus() is focus
    window.player.disconnect(playing)
    window.player.pause()
    name_button.emit("clicked")
    assert window.listen.active and window.listen.speaker == "voice-12"
    assert window.listen.index == 0
    focus_changes = []

    def focus_state(*_):
        focused = window.get_focus()
        state = dict(
            widget=type(focused).__name__,
            tooltip=focused.get_tooltip_text() if focused else None,
            play=focused is play,
            mapped=play.get_mapped(),
            sensitive=play.get_sensitive(),
            current=page._speaker_rows["voice-12"][1] is play,
            collapsed=page.split.get_collapsed(),
            active=window.is_active(),
        )
        focus_changes.append(state)
        return state

    focus_handler = window.connect("notify::focus-widget", focus_state)
    assert play.grab_focus(), focus_state()
    focus_state()
    # A real wheel event suspends transcript following, including auto-advance.
    assert window.listen.start("voice-0", 126)

    def span_started():
        ok, position = window.player._pipeline.query_position(Gst.Format.TIME)
        return ok and window.listen.lead_start(126) <= position // Gst.MSECOND < window.listen.span_end(126)

    until(window, span_started)
    window.player.pause()
    scroll_calls = []
    scroll_to = page.list.scroll_to

    def observed_scroll(index, *args):
        scroll_calls.append(index)
        return scroll_to(index, *args)

    page.list.scroll_to = observed_scroll
    point = Graphene.Point()
    point.init(page.scroller.get_width() / 2, page.scroller.get_height() / 2)
    ok, pointer = page.scroller.compute_point(window, point)
    assert ok
    subprocess.run(
        ["xdotool", "mousemove", "--window", xid, str(round(pointer.x + dx)), str(round(pointer.y + dy)), "click", "4"],
        check=True,
        timeout=5,
    )
    until(window, lambda: page._follow_until > GLib.get_monotonic_time())
    scroll_calls.clear()
    window.player.play()
    try:
        until(window, lambda: window.listen.index == 127)
    except AssertionError:
        print(
            dict(
                stage="auto advance",
                index=window.listen.index,
                count=window.listen.count,
                speaker=window.listen.speaker,
                position=window.player.position,
                playing=window.player.playing,
                span=window.player._span,
                last=window.listen._span_last,
            ),
            flush=True,
        )
        raise
    window.player.pause()
    assert not scroll_calls and page._follow_until > GLib.get_monotonic_time()
    focus = window.get_focus()
    assert window.listen.previous_turn()
    assert page._follow_until == 0 and window.get_focus() is focus
    window.player.pause()

    def reading_visible():
        index = min(page._turn_rows[window.listen.turn.index])
        if index not in page.bound:
            return False
        row = page.bound[index][2]
        ok, bounds = row.compute_bounds(page.scroller)
        return ok and bounds.get_y() >= 0 and bounds.get_y() + bounds.get_height() <= page.scroller.get_height()

    until(window, reading_visible)
    assert page._reading_index in page._turn_rows[window.listen.turn.index]
    assert scroll_calls
    page.list.scroll_to = scroll_to
    assert window.listen.start("voice-12", 0)
    window.player.pause()
    until(window, reading_visible)
    assert page._reading_index == target.index
    bar = page.listening_bar
    until(window, lambda: bar.get_child_revealed())
    assert bar.name.get_text() == "Unidentified voice"
    assert bar.turn.get_text() == text("listen.turn", index=1, count=1, position="00:00:34")
    assert not bar.buttons["nextTurn"].get_sensitive()
    assert not bar.buttons["nextSpeaker"].get_sensitive()
    assert bar.buttons["previousSpeaker"].get_sensitive()
    # Allocate the same real widget at the narrow minimum and inspect collapse.
    bar._collapse(3)
    assert not bar.excerpt.get_visible() and not bar.merge.get_visible()
    assert not bar.buttons["name"].get_visible()
    assert bar.more.get_visible() and bar.stop.get_visible()
    assert bar.buttons["previousTurn"].get_visible()
    bar._collapse(0)
    assert play.get_icon_name() == "media-playback-stop-symbolic"
    assert row.has_css_class("listening")
    until(window, lambda: page.speakers_scroller.get_vadjustment().get_value() > 0)
    assert window.get_focus() is play, (focus_state(), focus_changes)
    window.disconnect(focus_handler)
    assert play.activate()
    until(window, lambda: not window.listen.active)
    assert not window.player.playing and play.get_icon_name() == "media-playback-start-symbolic"
    assert not row.has_css_class("listening")
    # Renaming updates the accessible name without announcing a turn step.
    announcements = []
    announce = bar.body.announce
    bar.body.announce = lambda message, *_: announcements.append(message)
    assert window.listen.start("voice-0", 0, play=False)
    assert announcements[-1] == f"Unidentified voice, {page._speaker_rows['voice-0'][2]}"
    announcements.clear()
    original_snapshot = window._source_snapshot
    other_row = window.rows.get_first_child()
    while other_row is not None and (not hasattr(other_row, "meeting") or other_row.meeting.key == meeting.key):
        other_row = other_row.get_next_sibling()
    assert other_row is not None
    window.rows.select_row(other_row)
    assert window.selected.key != meeting.key
    library_row = window.rows.get_first_child()
    while library_row is not None and getattr(getattr(library_row, "meeting", None), "key", None) != meeting.key:
        library_row = library_row.get_next_sibling()
    assert library_row is not None
    window.rows.select_row(library_row)
    assert window.selected.key == meeting.key
    renamed_meeting = replace(
        meeting,
        speakers=tuple(
            replace(speaker, name="Dana") if speaker.id == "voice-0" else speaker for speaker in meeting.speakers
        ),
    )
    # Renaming must preserve transcript model items and focused header widgets.
    until(window, reading_visible)
    splices, headers, input_seen = [], [], []
    model_item = page.store.get_item(0)
    splice_handler = page.store.connect("items-changed", lambda *_: splices.append(True))
    refresh_header = page._refresh_header

    def observed_header(turn, row):
        headers.append(turn.index)
        if len(headers) == 1:
            GLib.idle_add(lambda: input_seen.append(len(headers)) or False, priority=GLib.PRIORITY_DEFAULT)
        return refresh_header(turn, row)

    page._refresh_header = observed_header
    window._library_scanned(
        replace(
            original_snapshot,
            meetings=tuple(renamed_meeting if m.key == meeting.key else m for m in original_snapshot.meetings),
        )
    )
    until(
        window,
        lambda: (
            bool(input_seen)
            and not page._header_pending
            and not page._header_source
            and all(
                any(value.get_text() == "Dana" for value in labels(row))
                for turn, _words, row in page.bound.values()
                if turn.speaker == "voice-0"
            )
        ),
    )
    # D84 keeps each rejoined run whole: this fixture now has 16 reading
    # rows, exactly one legal batch (formerly 18). Assert the production's
    # existing 16-header cap, even when that batch finishes all bound rows.
    # A large refresh may not process almost all rows before yielding either.
    assert input_seen and 0 < input_seen[0] <= min(16, len(headers)), (
        "Header refresh monopolized the main loop",
        input_seen,
        len(headers),
    )
    assert not splices and page.store.get_item(0) is model_item
    page._refresh_header = refresh_header
    assert bar.name.get_text() == "Dana" and not announcements
    # A single-speaker meeting exposes Name but never an empty Merge menu.
    merges = [{"from": s.id, "into": "voice-0"} for s in renamed_meeting.speakers if s.id != "voice-0"]
    merged_metadata = dict(renamed_meeting.metadata, speakerMerges=merges)
    # A cached transcript can still reference merged-away ids. Both the real
    # preview and virtualized transcript must resolve them without exceptions.
    stale_merged = replace(
        renamed_meeting,
        speakers=tuple(s for s in renamed_meeting.speakers if s.id == "voice-0"),
        metadata=merged_metadata,
    )
    window._library_scanned(
        replace(
            original_snapshot,
            meetings=tuple(stale_merged if m.key == meeting.key else m for m in original_snapshot.meetings),
        )
    )
    assert "host" not in {speaker.id for speaker in stale_merged.speakers}
    assert any(turn.speaker == "host" for turn in stale_merged.preview_turns)
    # Exercise a fresh real GTK bind with a genuinely absent speaker id.
    # This is separate from the rescan's no-splice/widget-retention guarantee.
    factory = page.list.get_factory()
    page.list.set_factory(None)
    page.list.set_factory(factory)
    until(window, lambda: page.bound and any(turn.speaker == "host" for turn, _words, _row in page.bound.values()))
    assert any(turn.speaker == "host" for turn, _words, _row in page.bound.values())
    host_turn = next(t for t in stale_merged.listening_turns if t.speaker == "host")
    assert "Dana" in page.timeline.readout_at(host_turn.start_ms)
    until(
        window,
        lambda: all(
            any(value.get_text() == "Dana" for value in labels(row)) for _turn, _words, row in page.bound.values()
        ),
    )
    # Preview refresh can arrive after transcript rows in a later loop turn.
    until(window, lambda: any(value.get_text() == "Dana" for value in labels(window.preview)))
    assert any(value.get_text() == "Dana" for value in labels(window.preview))

    def preview_chip_names():
        child = window.preview.get_first_child()
        while child is not None and not isinstance(child, Adw.WrapBox):
            child = child.get_next_sibling()
        return [value.get_text() for value in labels(child)] if child is not None else None

    until(window, lambda: preview_chip_names() == ["Dana"])
    assert preview_chip_names() == ["Dana"]
    # A separate fixture retains both listed ids for the overlay-only check.
    listed_merge = replace(
        stale_merged, speakers=tuple(s for s in renamed_meeting.speakers if s.id in ("host", "voice-0"))
    )
    window._library_scanned(
        replace(
            original_snapshot,
            meetings=tuple(listed_merge if m.key == meeting.key else m for m in original_snapshot.meetings),
        )
    )
    until(window, lambda: preview_chip_names() == ["Dana"])
    assert preview_chip_names() == ["Dana"]
    assert not bar.merge.get_sensitive() and not bar.actions.lookup_action("merge").get_enabled()
    # Only the overlay changes: raw speakers, words and preview turns stay equal.
    # Both preview and retained transcript headers must invalidate.
    unmerged_preview = replace(
        listed_merge, metadata=dict(merged_metadata, speakerMerges=[m for m in merges if m["from"] != "host"])
    )
    window._library_scanned(
        replace(
            original_snapshot,
            meetings=tuple(unmerged_preview if m.key == meeting.key else m for m in original_snapshot.meetings),
        )
    )
    until(window, lambda: preview_chip_names() == ["Claire Lee", "Dana"])
    assert preview_chip_names() == ["Claire Lee", "Dana"]
    until(
        window,
        lambda: all(
            any(value.get_text() == "Claire Lee" for value in labels(row))
            for turn, _words, row in page.bound.values()
            if turn.speaker == "host"
        ),
    )
    assert not splices and page.store.get_item(0) is model_item
    page.store.disconnect(splice_handler)
    section = bar.merge_model.get_item_link(0, Gio.MENU_LINK_SECTION)
    assert section is not None and section.get_n_items() == 1
    assert (
        section.get_item_attribute_value(0, "label", None).unpack()
        == f"Claire Lee, {text('speakers.mergeInto.speaksBefore')}"
    )
    assert bar.merge.get_sensitive() and bar.actions.lookup_action("merge").get_enabled()

    # The single-speaker fixture itself uses the merged words and rebuilt turns.
    merged_words = apply_merges(renamed_meeting.words, merges)
    merged_turns = reading_turns(merged_words)
    one_speaker = replace(
        stale_merged,
        speakers=tuple(s for s in stale_merged.speakers if s.id == "voice-0"),
        words=merged_words,
        turns=merged_turns,
        listening_turns=speaker_turns(merged_words),
        preview_turns=tuple(PreviewTurn(t.speaker, t.start_ms, t.text) for t in merged_turns[:3]),
    )
    window._library_scanned(
        replace(
            original_snapshot,
            meetings=tuple(one_speaker if m.key == meeting.key else m for m in original_snapshot.meetings),
        )
    )
    assert bar.buttons["name"].get_sensitive()
    assert not bar.merge.get_sensitive() and not bar.actions.lookup_action("merge").get_enabled()
    assert page._highlighted_rows == page._turn_rows[window.listen.turn.index]
    assert page._highlighted_rows == frozenset(t.index for t in one_speaker.turns)
    assert page.store.get_n_items() == len(one_speaker.turns)
    window._library_scanned(original_snapshot)
    bar.body.announce = announce
    window.listen.stop()
    # Markup dims only the lead-in and preserves literal transcript characters.
    assert window.listen.start("voice-10", 0, play=False)
    assert '<span alpha="55%">' in bar.excerpt.get_label()
    assert not bar.excerpt.has_css_class("dim-label")
    assert bar.excerpt.get_use_markup()
    window.listen.stop()
    assert page.timeline._target == ()
    # Read-only rows retain Play, but have no naming action.
    old_play = page._speaker_rows["voice-0"][1]
    generation = len(rebuilt)
    window.set_visible(False)
    # Deliver the read-only state through the scanner's real UI entry point.
    # The one-second activity tick reapplies _source_snapshot even after
    # monitor.close(); a page-only edit would be replaced by its editable copy
    # during GTK's delayed activation, unrealizing and cancelling the button.
    editable_snapshot = window._source_snapshot
    readonly_snapshot = replace(
        editable_snapshot,
        meetings=tuple(
            replace(m, metadata_editable=False) if m.key == meeting.key else m for m in editable_snapshot.meetings
        ),
    )
    window._library_scanned(readonly_snapshot)
    replacement = page._speaker_rows["voice-0"][1]
    assert replacement is not old_play and not replacement.get_realized()
    assert len(rebuilt) == generation
    # Check the actual replacement's wiring independently of GTK activation:
    # clicked must reach the existing controller even before its first map.
    replacement.emit("clicked")
    assert window.listen.active and window.listen.speaker == "voice-0"
    page._listen_speaker("voice-0")
    assert not window.listen.active
    window.present()
    until(window, lambda: len(rebuilt) > generation)
    row, play, _ = rebuilt[-1]["voice-0"]
    assert play is not old_play and page._speaker_rows["voice-0"][1] is play
    assert not isinstance(row.get_child(), Gtk.Button) and play.get_visible()
    assert play.get_realized() and play.get_mapped() and play.get_sensitive()
    clicks, errors = [], []
    play.connect("clicked", lambda *_: clicks.append(window.listen.speaker))
    error_handler = window.player.connect("error", lambda _player, message: errors.append(message))
    assert play.activate()
    # Force the formerly failing ordering; no dependence on the timer phase.
    window._activity_tick()
    assert not page.meeting.metadata_editable
    assert page._speaker_rows["voice-0"][1] is play and play.get_realized()
    try:
        until(window, lambda: window.listen.active)
    except AssertionError as error:
        raise AssertionError(
            dict(
                stage="read-only Play",
                clicks=clicks,
                errors=errors,
                current=page._speaker_rows["voice-0"][1] is play,
                realized=play.get_realized(),
                mapped=play.get_mapped(),
                sensitive=play.get_sensitive(),
                width=play.get_width(),
                loaded=window.player.loaded,
                prerolled=window.player._prerolled,
                position=window.player.position,
            )
        ) from error
    finally:
        window.player.disconnect(error_handler)
    assert window.listen.speaker == "voice-0" and clicks == ["voice-0"]
    assert page.speakers_dialog is dialog and dialog.closed
    assert not page.listening_bar.buttons["name"].get_visible()
    assert not page.listening_bar.merge.get_visible()
    assert not page.listening_bar.actions.lookup_action("name").get_enabled()
    assert not page.listening_bar.actions.lookup_action("merge").get_enabled()
    page.listening_bar._collapse(3)
    assert page.listening_bar.more_model.get_n_items() == 2
    page.listening_bar._collapse(page.listening_bar._layout or 0)
    page._listen_speaker("voice-0")
    generation = len(rebuilt)
    window._library_scanned(editable_snapshot)
    until(window, lambda: len(rebuilt) > generation)
    # Force a caption rebuild in each supported language and check the time
    # receives its natural width, even when provenance must ellipsize.
    for language in LANGUAGES:
        with using_language(language):
            page._signature = None
            page.update(meeting)
            row, _, when = page._speaker_rows["voice-0"]
            caption = row.get_child().get_child().get_last_child()
            timed = caption.get_last_child()
            until(window, lambda timed=timed: timed.get_width() >= timed.measure(Gtk.Orientation.HORIZONTAL, -1)[1])
            assert when in timed.get_text()
            # Header batches retain this locale even after an idle boundary.
            header = next(iter(page.bound.values()))[2].get_first_child().get_last_child()
            until(window, lambda header=header: header.get_tooltip_text() == text("player.playFromHere"))
    with using_language("en"):
        page._signature = None
        page.update(meeting)
    page._listen_speaker("voice-12")
    window.set_default_size(360, 650)
    until(window, lambda: page.listening_bar.get_child_revealed() and page.listening_bar.get_width() == 360)
    assert not page.listening_bar.buttons["nextSpeaker"].get_visible()
    assert not page.listening_bar.buttons["name"].get_visible()
    assert page.listening_bar.buttons["nextTurn"].get_mapped()
    assert page.listening_bar.stop.get_mapped()
    assert not page.elapsed.get_visible() and not page.total.get_visible()
    page.stop_listening()
    window.set_default_size(900, 650)
    # Native size includes decorations: this GTK build allocates 890 px to
    # GtkWindow inside a requested 900 px surface. Await the exact real resize.
    until(window, lambda: window.get_surface().get_width() == 900 and not page.split.get_collapsed())

    def save_picture(path):
        painted = []

        def snapshot():
            image = Gtk.Snapshot.new()
            Gtk.WidgetPaintable.new(window).snapshot(image, window.get_width(), window.get_height())
            node = image.to_node()
            if node is None:
                return False
            painted.append(node)
            return True

        until(window, snapshot)
        node = painted[-1]
        assert window.get_renderer().render_texture(node, node.get_bounds()).save_to_png(str(path))

    page._listen_speaker("voice-12")

    def target_visible():
        target = page._speaker_rows["voice-12"][0]
        ok, bounds = target.compute_bounds(page.speakers_scroller)
        return (
            ok and bounds.get_y() >= 0 and bounds.get_y() + bounds.get_height() <= page.speakers_scroller.get_height()
        )

    until(window, lambda: page.listening_bar.get_child_revealed() and target_visible() and reading_visible())
    assert page._reading_index in page._turn_rows[window.listen.turn.index]
    window.player.pause()
    output = Path(os.environ["CASSINI_SCREENSHOT_OUT"])
    save_picture(output)
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    save_picture(output.with_name(output.stem + "-dark.png"))
    # Keyboard focus still scrolls a different row into view without changing
    # the listening target or starting its naming action.
    first, first_play, _ = page._speaker_rows["host"]
    first_play.grab_focus()

    def focused_row_visible():
        ok, bounds = first.compute_bounds(page.speakers_scroller)
        return (
            ok and bounds.get_y() >= 0 and bounds.get_y() + bounds.get_height() <= page.speakers_scroller.get_height()
        )

    until(window, focused_row_visible)
    assert window.listen.speaker == "voice-12" and page.speakers_dialog is dialog and dialog.closed
    page.update(replace(meeting, has_audio=False))
    assert not window.listen.active and not page.listening_bar.get_reveal_child()
    assert not any(words.has_css_class("listen-current") for _t, words, _row in page.bound.values())
    for _turn, _words, bound_row in page.bound.values():
        headers = bound_row.get_first_child()
        assert not headers.get_first_child().get_sensitive() and not headers.get_last_child().get_sensitive()
    assert all(not button.get_visible() for _, button, _ in page._speaker_rows.values())
    # A real player error disables existing header widgets without a rebind too.
    page.update(meeting)
    assert window.listen.start("voice-12", 0, play=False)
    window.player.emit("error", "The fixture audio could not be played.")
    assert not window.listen.active
    for _turn, _words, bound_row in page.bound.values():
        headers = bound_row.get_first_child()
        assert not headers.get_first_child().get_sensitive() and not headers.get_last_child().get_sensitive()
    from markup_runs_check import check_markup_runs

    check_markup_runs(window, until)
    app.begin_exit()
    window.close()
    app.do_shutdown()
    print(
        "PASS listening rows: separate Play/Stop, focus/scroll, read-only, 14 voices, six-language captions, no-audio"
    )
