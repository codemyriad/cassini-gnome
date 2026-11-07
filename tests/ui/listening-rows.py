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
from gi.repository import Adw, Gio, GLib, Gst, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.i18n import LANGUAGES, using_language  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_store import atomic_json, update_metadata  # noqa: E402
from cassini_talk.recording_session import RecordingSession  # noqa: E402
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
    # Name from the bar pauses listening and focuses the sample, preserving
    # the pane's original entry-first dialog path.
    window.listen.start("voice-10", 0, play=False)
    page.listening_bar.name_speaker()
    dialog = page.speakers_dialog
    until(window, lambda: dialog.play.has_focus())
    assert not window.player.playing
    assert dialog.sample_from.get_text() == text("speakers.sampleFrom", position="00:00:32")
    dialog.close()
    until(window, lambda: page.listening_bar.buttons["name"].has_focus())
    window.listen.stop()
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
    page.merge_speaker("voice-10", "host")
    until(
        window,
        lambda: "voice-10" not in page._speaker_rows and window.listen.speaker == "voice-11" and window.listen.cued,
    )
    assert not window.player.playing
    assert page.listening_bar.buttons["again"].get_visible()
    assert page.listening_bar.buttons["again"].has_focus()
    page.undo_speaker_merge()
    until(window, lambda: "voice-10" in page._speaker_rows and page._speaker_future is None)
    window.listen.stop()
    update_metadata(meeting.directory, speakers=original_metadata["speakers"])
    window.monitor.refresh_now()
    until(window, lambda: all(s.provenance == "user" for s in page.meeting.speakers) and not page._rows_rebuilt_tick)
    meeting = page.meeting
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
    assert window.get_focus() is play
    assert play.activate()
    until(window, lambda: not window.listen.active)
    assert not window.player.playing and play.get_icon_name() == "media-playback-start-symbolic"
    assert not row.has_css_class("listening")
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
    with using_language("en"):
        page._signature = None
        page.update(meeting)
    window.set_default_size(900, 650)
    until(window, lambda: window.get_width() <= 900)

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

    until(window, lambda: page.listening_bar.get_child_revealed() and target_visible())
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
    assert all(not button.get_visible() for _, button, _ in page._speaker_rows.values())
    app.begin_exit()
    window.close()
    app.do_shutdown()
    print(
        "PASS listening rows: separate Play/Stop, focus/scroll, read-only, 14 voices, six-language captions, no-audio"
    )
