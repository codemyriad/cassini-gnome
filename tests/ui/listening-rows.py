#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private GTK row playback, read-only access, caption fit and focus."""

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
    meeting = window.snapshot.meetings[0]
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
    window.present()
    until(window, lambda: window.player._prerolled and len(page._speaker_rows) == 14)
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
    assert window.listen.speaker == "voice-12" and page.speakers_dialog is None
    assert play.get_icon_name() == "media-playback-stop-symbolic"
    assert row.has_css_class("listening")
    until(window, lambda: page.speakers_scroller.get_vadjustment().get_value() > 0)
    assert window.get_focus() is play
    assert play.activate()
    until(window, lambda: not window.listen.active)
    assert not window.player.playing and play.get_icon_name() == "media-playback-start-symbolic"
    assert not row.has_css_class("listening")
    # Read-only rows retain Play, but have no naming action.
    page.update(replace(meeting, metadata_editable=False))
    row, play, _ = page._speaker_rows["voice-0"]
    assert not isinstance(row.get_child(), Gtk.Button) and play.get_visible()
    until(window, lambda: play.get_width() > 0)
    play.activate()
    until(window, lambda: window.listen.active)
    assert page.speakers_dialog is None
    page._listen_speaker("voice-0")
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

    until(window, target_visible)
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
    assert window.listen.speaker == "voice-12" and page.speakers_dialog is None
    page.update(replace(meeting, has_audio=False))
    assert all(not button.get_visible() for _, button, _ in page._speaker_rows.values())
    app.begin_exit()
    window.close()
    app.do_shutdown()
    print(
        "PASS listening rows: separate Play/Stop, focus/scroll, read-only, 14 voices, six-language captions, no-audio"
    )
