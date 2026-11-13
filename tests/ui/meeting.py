#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real Meeting navigation/transport/items/preferences in the private display."""

import json
import os
import shutil
import subprocess
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
from gi.repository import Adw, Gio, GLib, GObject, Gst, Gtk  # noqa: E402

build = Path(os.environ["CASSINI_TEST_BUILD"])
Gio.resources_register(Gio.Resource.load(str(build / "src/cassini-talk.gresource")))
sys.path.insert(0, str(ROOT / "tests"))
from desktop_services import DesktopServices  # noqa: E402
from painted import painted_node  # noqa: E402

from cassini_talk.folder_player import FolderPlayer  # noqa: E402
from cassini_talk.library_window import LibraryWindow  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402


def shot(window, suffix):
    out = os.environ.get("CASSINI_SCREENSHOT_OUT")
    if not out:
        return
    target = Path(out)
    settings = Gtk.Settings.get_default()
    previous_anim = settings.props.gtk_enable_animations
    manager = Adw.StyleManager.get_default()
    previous_scheme = manager.get_color_scheme()
    settings.props.gtk_enable_animations = False
    try:
        for scheme, extra in ((Adw.ColorScheme.FORCE_LIGHT, ""), (Adw.ColorScheme.FORCE_DARK, "-dark")):
            manager.set_color_scheme(scheme)
            until(lambda: not window.meeting_page or window.meeting_page._style_source == 0)
            frames = []
            window.add_tick_callback(lambda *_, frames=frames: (frames.append(True), len(frames) < 3)[1])
            window.queue_draw()
            until(lambda frames=frames: len(frames) >= 2)
            node = painted_node(window)
            texture = window.get_renderer().render_texture(node, node.get_bounds())
            assert texture.save_to_png(str(target.with_name(target.stem + suffix + extra + ".png")))
    finally:
        manager.set_color_scheme(previous_scheme)
        settings.props.gtk_enable_animations = previous_anim
        until(lambda: not window.meeting_page or window.meeting_page._style_source == 0)


def until(check):
    end = time.monotonic() + 8
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Meeting GUI condition timed out"


def start_listening(window):
    """D70: listen to the first speaker of the open meeting (UI entry points come later)."""
    from cassini_talk.meeting_text import speaker_turns

    turns = speaker_turns(window.meeting_page.meeting.words)
    window.listen.set_turns(turns)
    until(lambda: window.player._prerolled)
    assert window.listen.start(turns[0].speaker)
    assert window.listen.active and window.player.span_follow is not None


def assert_stopped_listening(window):
    assert not window.listen.active and window.listen.speaker is None
    assert window.player.span_follow is None


def labels(widget):
    result = [widget.get_label()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        result += labels(child)
        child = child.get_next_sibling()
    return result


services = DesktopServices()
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    os.environ["CASSINI_LIBRARY_DIR"] = str(root / "fixtures/main/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(root / "fixtures/main/activity.json")
    os.environ["CASSINI_SCREENSHOT_NOW"] = "1793107080000"
    app = CassiniApplication("com.gocassini.MeetingTest", "test", "development")
    app.register(None)
    app.activate()
    window = next(w for w in app.get_windows() if isinstance(w, LibraryWindow))
    until(lambda: not window._pending)
    meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    from cassini_talk.meeting_store import read_metadata, update_metadata

    meta = read_metadata(meeting.directory)
    update_metadata(meeting.directory, transcript=dict(meta["transcript"], status="done"), etaMinutes=None)
    window.monitor.refresh_now()
    until(lambda: any(m.key == meeting.key and m.transcript == "done" for m in window.snapshot.meetings))
    meeting = next(m for m in window.snapshot.meetings if m.key == meeting.key)
    window.player.shutdown()
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    window.player = FolderPlayer(audio_sink=sink)
    window._connect_player()
    window.open_meeting(meeting)
    page = window.meeting_page
    assert page._exact
    until(lambda: page.list.ready and len(page.bound) == 5 and window.player._prerolled)
    assert all(v in labels(page.speakers) for v in ["Marta", "Giulia Rossi", "Speaker 3"])
    # Imported recognition metadata has no corresponding local saved voice.
    assert "auto" not in labels(page.speakers)
    assert page._speaker_rows["marta"][0].get_child().get_first_child().has_css_class("speaker-dot-1")
    guest_dot = page._speaker_rows["guest"][0].get_child().get_first_child()
    assert guest_dot.has_css_class("speaker-dot-0") and guest_dot.get_next_sibling().has_css_class("speaker-unnamed")
    # WP3 Find in this meeting: Ctrl+F focuses, highlights active + passive hits, steps and clears.
    assert page.find_row.get_visible() and page.find_shortcut_badge.get_visible()
    assert page.find_shortcut_badge.get_label() == "Ctrl+F"
    window.activate_action("win.find-in-meeting", None)
    until(lambda: page.find_bar.has_css_class("focused") and not page.find_shortcut_badge.get_visible())
    page.find_entry.set_text("migration")
    until(lambda: page.find_count_label.get_visible() and page.find_count_label.get_label() == "1 of 2")
    assert 'underline="single"' in page.bound[1][1].get_label()
    assert "background=" in page.bound[3][1].get_label() and 'underline="single"' not in page.bound[3][1].get_label()
    shot(window, "-find")
    page.find_next_button.emit("clicked")
    until(lambda: page.find_count_label.get_label() == "2 of 2")
    assert 'underline="single"' in page.bound[3][1].get_label()
    page.find_prev_button.emit("clicked")
    until(lambda: page.find_count_label.get_label() == "1 of 2")
    page.clear_find()
    until(lambda: page.find_entry.get_text() == "" and not page.find_count_label.get_visible())
    assert 'underline="single"' not in page.bound[1][1].get_label()
    menu_changes = []
    handler = page.more_menu.connect("items-changed", lambda *_: menu_changes.append(1))
    page.update(meeting)
    page.update(meeting)
    assert not menu_changes, "An unchanged meeting must not rebuild the More menu"
    page.more_menu.disconnect(handler)
    # D84 repairs the fixture's inflated sentence-final item to 188–189 s.
    # Seek inside its displayed timing rather than the discarded tail.
    window.player.seek(188500)
    current = next(row for row in page.bound.values() if row[0].speaker == "guest")
    assert "background=" in current[1].get_label()
    assert 'alpha="60%"' in page.bound[3][1].get_label()
    assert page.elapsed.get_label() == "00:03:08" and page.total.get_label() == "00:47:05"
    assert isinstance(page.rate_button, Gtk.MenuButton)
    window.activate_action("win.rate", GLib.Variant("s", "1.5"))
    assert window.player.rate == 1.5 and page.rate_button.get_label() == "1.5×"
    assert window.lookup_action("rate").get_state().get_string() == "1.5"
    window.activate_action("win.rate", GLib.Variant("s", "nan"))
    assert window.player.rate == 1.5, "Invalid rate must be ignored"
    # Real GStreamer clock, synchronised fakesink: 4 wall seconds at 2×.
    window.activate_action("win.rate", GLib.Variant("s", "2"))
    window.player.seek(10000)
    until(lambda: window.player._prerolled)
    window.player.play()
    until(lambda: window.player.position > 10100)
    started, position = time.monotonic(), window.player.position
    until(lambda: time.monotonic() - started >= 4)
    advance = window.player.position - position
    window.player.pause()
    assert 7000 <= advance <= 9000, ("2× clock advance", advance)
    print(f"PASS real 2× playback: {advance} ms in 4 wall seconds", flush=True)
    window.activate_action("win.rate", GLib.Variant("s", "1.5"))
    window.player.seek(192000)

    current[1].emit("activate-link", "item:0")
    until(lambda: window.player.position == 188000)
    page.seek.emit("change-value", Gtk.ScrollType.JUMP, 190000)
    assert window.player.position == 190000
    page.play.emit("clicked")
    until(lambda: window.player.playing)
    assert page.play.get_tooltip_text() == "Pause"
    page.play.emit("clicked")
    assert not window.player.playing
    # D26: both surfaces explain a completed empty transcript and run the
    # normal write-down action. No real model, device or user Library is used.
    with (
        patch.object(window.monitor, "callback", lambda _snapshot: None),
        patch.object(window, "_source_snapshot", window._source_snapshot),
        patch.object(window, "snapshot", window.snapshot),
    ):
        from dataclasses import replace
        from types import SimpleNamespace

        base_snapshot = window._source_snapshot

        def sync_snapshot(current):
            updated = replace(
                base_snapshot,
                meetings=tuple(current if m.key == current.key else m for m in base_snapshot.meetings),
            )
            window._source_snapshot = updated
            window._updated(updated)

        # WP5 (L-45): queued/running meetings show the header transcribing status row,
        # keep the desktop transcript area empty while transcribing, and open the chevron
        # details popover with 5 steps, 3 stats, and Stop and keep progress.
        from cassini_talk import i18n as fmt
        from cassini_talk.transcription import TranscriptionProgress

        queued = replace(meeting, words=(), turns=(), preview_turns=(), transcript="queued", eta_minutes=None)
        sync_snapshot(queued)
        assert page.transcribing_row.get_visible()
        assert "Transcribing…" in labels(page.transcribing_row), "Queued without ETA must show Transcribing…"
        assert not page.transcribing_row.eta_label.get_visible()
        assert page.transcribing_blank.get_visible() and page.scroller.get_opacity() == 0.0
        assert not page.find_row.get_visible()
        assert not page.speakers_scroller.get_visible()
        assert not page.lines.get_visible()
        assert not page.write_button.get_visible()

        running = replace(meeting, transcript="running", eta_minutes=3)
        live_prog = TranscriptionProgress(
            step="words",
            fraction=0.62,
            audio_done_ms=min(running.duration_ms or 180000, 90000),
            words_so_far=4812,
            eta_minutes=3,
        )
        transcriber = app._ensure_transcriber()
        transcriber._set_progress(running.directory, live_prog, notify=False, reset=True)
        try:
            sync_snapshot(running)
            assert page.transcribing_row.get_visible()
            assert "Transcribing" in labels(page.transcribing_row)
            assert "· about 3 min left" in labels(page.transcribing_row)
            assert page.transcribing_blank.get_visible() and page.scroller.get_opacity() == 0.0, (
                "L-45: desktop transcript area stays empty while transcribing"
            )
            assert not page.find_row.get_visible()
            assert not page.speakers_scroller.get_visible()
            assert page.player_overlay.get_visible()
            assert page.stop_transcription_button is page.transcribing_row.stop_button
            assert page.stop_transcription_button.get_label() == "Stop"
            assert page.stop_transcription_button.get_sensitive()
            shot(window, "-transcribing")

            page.transcribing_row.details_button.popup()
            until(lambda: page.transcribing_row.popover.get_mapped())
            pop_labels = labels(page.transcribing_row.popover)
            for expected in (
                "Transcribing",
                "62%",
                "Reading",
                "Preparing",
                "Words",
                "Speakers & matching",
                "Saving",
                "Time left",
                "About 3 min",
                "Audio done",
                "Words so far",
                fmt.number(4812),
                "Stop and keep progress",
                "Keeps going in the background while Cassini is open.",
            ):
                assert expected in pop_labels, f"Missing {expected!r} in popover labels {pop_labels!r}"

            # Exercise the live progress notification path without a snapshot update.
            transcriber._set_progress(
                running.directory,
                TranscriptionProgress(
                    step="speakers",
                    fraction=0.84,
                    audio_done_ms=running.duration_ms or 180000,
                    words_so_far=6120,
                    eta_minutes=1,
                ),
            )
            until(
                lambda: (
                    "· about 1 min left" in labels(page.transcribing_row)
                    and "84%" in labels(page.transcribing_row.popover)
                )
            )
            live_pop_labels = labels(page.transcribing_row.popover)
            assert "About 1 min" in live_pop_labels
            assert fmt.number(6120) in live_pop_labels

            with patch.object(app, "stop_transcription") as pop_stop_call:
                page.transcribing_row.popover_stop_button.emit("clicked")
                pop_stop_call.assert_called_once_with(running)
            until(lambda: not page.transcribing_row.popover.get_mapped())
        finally:
            transcriber._clear_progress_locked(running.directory)

        with patch.object(app, "stop_transcription") as stop_call:
            page.stop_transcription_button.emit("clicked")
            stop_call.assert_called_once_with(running)
        app._transcription_stops.add(app._stop_key(running))
        try:
            page.update(running)
            assert page.stop_transcription_button is not None and not page.stop_transcription_button.get_sensitive()
        finally:
            app._transcription_stops.clear()
        stopped_partial = replace(meeting, transcript="none", eta_minutes=None)
        sync_snapshot(stopped_partial)
        assert not page.transcribing_row.get_visible()
        assert page.stop_transcription_button is None
        assert page.write_button.get_visible() and page.write_button.get_label() == "Write it down"
        assert not page.transcribing_blank.get_visible() and page.scroller.get_opacity() == 1.0
        assert page._turns == stopped_partial.turns, "Stopping transcription must keep partial words visible"
        shot(window, "-stopped")
        sync_snapshot(replace(running, eta_minutes=0))
        assert "Transcribing, less than a minute left" in labels(page.transcribing_row), "ETA-only refresh must update"
        stopped_early = replace(
            meeting, lifecycle="saved", transcript="done", eta_minutes=None, interrupted_at_ms=2472000
        )
        sync_snapshot(stopped_early)
        assert not page.transcribing_row.get_visible()
        assert not page.transcribing_blank.get_visible() and page.scroller.get_opacity() == 1.0
        assert "Interrupted at 00:41:12" in labels(page.lines)
        sync_snapshot(replace(meeting, transcript="done", eta_minutes=None))
        assert not page.transcribing_row.get_visible()
        assert not any(value.startswith("Transcribing") for value in labels(page.lines))
        assert "Interrupted at 00:41:12" not in labels(page.lines)

        # Library preview also offers Stop while queued/running and restores Write it down once stopped.
        settings = Gtk.Settings.get_default()
        previous_anim = settings.props.gtk_enable_animations
        settings.props.gtk_enable_animations = False
        try:
            sync_snapshot(running)
            window.back_to_library()
            until(lambda: window.meeting_page is None and window.navigation.get_visible_page() is not None)
            assert (
                window.detail_stack.get_visible_child_name() == "empty"
                and window.empty_selection_label.get_label() == "No meeting selected"
            )
            shot(window, "-empty-selection")
            window.rows.select_row(window._row_by_key[running.key])
            until(lambda: window.selected is not None and window.selected.key == running.key)
            assert (
                window.stop_transcription_button is not None and window.stop_transcription_button.get_label() == "Stop"
            )
            assert window.stop_transcription_button.get_sensitive()
            shot(window, "-library-transcribing")
            with patch.object(app, "stop_transcription") as stop_call:
                window.stop_transcription_button.emit("clicked")
                stop_call.assert_called_once_with(running)
            window._selected(None, SimpleNamespace(meeting=stopped_partial))
            assert window.stop_transcription_button is None
            assert (
                window.meeting_page.write_button.get_visible()
                and window.meeting_page.write_button.get_label() == "Write it down"
            )
            window._source_snapshot = base_snapshot
            window._updated(base_snapshot)
            window.open_meeting(meeting)
            page = window.meeting_page
            until(lambda: page.list.ready)
        finally:
            settings.props.gtk_enable_animations = previous_anim

        empty = replace(meeting, words=(), turns=(), preview_turns=(), transcript="done", eta_minutes=None)
        assert empty.no_speech and empty.can_write_down

        def widgets(widget):
            yield widget
            child = widget.get_first_child()
            while child:
                yield from widgets(child)
                child = child.get_next_sibling()

        with patch.object(app, "write_down") as retry:
            page.update(empty)
            assert "No speech was found in this meeting." in labels(page.lines)
            again = next(w for w in widgets(page.lines) if isinstance(w, Gtk.Button))
            assert again.get_label() == "Write it down again"
            again.emit("clicked")
            retry.assert_called_once_with(empty)
            retry.reset_mock()
            window._selected(None, SimpleNamespace(meeting=empty))
            assert "No speech was found in this meeting." in labels(window.meeting_page.lines)
            again = next(
                w
                for w in widgets(window.meeting_page.lines)
                if isinstance(w, Gtk.Button) and w.get_label() == "Write it down again"
            )
            again.emit("clicked")
            retry.assert_called_once_with(empty)
        page.update(meeting)
        window._selected(None, SimpleNamespace(meeting=meeting))
    # Both scans and the independent one-second refresh must keep reading the
    # synthetic fixture throughout this block. Restore the real model afterwards.
    with (
        patch.object(window.monitor, "callback", lambda _snapshot: None),
        patch.object(window, "_source_snapshot", window._source_snapshot),
        patch.object(window, "snapshot", window.snapshot),
    ):
        # Growing transcripts splice stable rows and never drag a paused reader back.
        from dataclasses import replace

        from cassini_talk.meeting_text import reading_turns
        from cassini_talk.portable import Word
        from cassini_talk.tokens import speaker_colour

        timed = tuple(
            Word(meeting.speakers[i % len(meeting.speakers)].id, i * 1000, i * 1000 + 900, f"Item {i}")
            for i in range(400)
        )

        def show_synthetic(words):
            current = replace(meeting, words=words, turns=reading_turns(words), transcript="done", eta_minutes=None)
            snapshot = replace(
                window._source_snapshot,
                meetings=tuple(current if m.key == current.key else m for m in window._source_snapshot.meetings),
            )
            window._source_snapshot = snapshot
            window._updated(snapshot)
            # Exercise the refresh that used to revert this test's model on
            # slower hosted layout, rather than depending on timer alignment.
            window._activity_tick()
            assert page.meeting.words is words
            assert page.list.items is getattr(current.turns, "layout", current.turns)

        # The real GTK label folds a brief reply at the original seam, and
        # its link still seeks the reply's own audio rather than the host's.
        first, second = (speaker.id for speaker in meeting.speakers[:2])
        interrupted = (
            Word(first, 0, 1000, "We should"),
            Word(second, 900, 1200, "Yes <&>."),
            Word(first, 1000, 2000, "ship it."),
        )
        show_synthetic(interrupted)
        until(lambda: len(page.bound) == 1 and page.bound[0][0].words[0] is interrupted[0])
        inline = page.bound[0][1]
        assert inline.get_text() == f"We should ({meeting.speaker_for(second).name}: Yes <&>.) ship it."
        assert 'href="aside:0:0"' in inline.get_label()
        inline.emit("activate-link", "aside:0:0")
        until(lambda: window.player.position == 900)
        assert 'background="#BDDCE4" foreground="#191B1F">Yes &lt;&amp;&gt;.' in inline.get_label()
        from cassini_talk.meeting_text import LayoutRow

        chunk_list = [
            Word(
                first,
                i * 100,
                i * 100 + 100,
                "chunk" if i == 126 else ("bridge" if i == 127 else f"tok{i}"),
            )
            for i in range(200)
        ]
        chunk_list.insert(100, Word(second, 9950, 10250, "Right."))
        show_synthetic(tuple(chunk_list))
        until(
            lambda: (
                len(page.meeting.turns) == 1
                and len(page._turns) == 2
                and isinstance(page._turns[0], LayoutRow)
                and 0 in page.bound
                and 1 in page.bound
            )
        )
        page.find_entry.set_text("chunk bridge")
        assert len(page._find_matches) == 1 and page.find_count_label.get_label() == "1 of 1"
        assert 'underline="single"' in page.bound[0][1].get_label()
        assert 'underline="single"' in page.bound[1][1].get_label()
        page.clear_find(focus_transcript=False)
        # Equal-content refresh (reparsed Word objects with unchanged signature) must
        # still map Find matches beyond chunk 0 onto row 1, word 53.
        retained_layout = page._turns
        reparsed_chunk_words = tuple(Word(w.speaker, w.start_ms, w.end_ms, w.text) for w in chunk_list)
        reparsed_meeting = replace(
            page.meeting,
            title="Renamed after merge",
            words=reparsed_chunk_words,
            turns=reading_turns(reparsed_chunk_words),
        )
        sync_snapshot(reparsed_meeting)
        assert page._turns is retained_layout and page.meeting.turns is reparsed_meeting.turns
        page.find_entry.set_text("tok180")
        assert len(page._find_matches) == 1 and page.find_count_label.get_label() == "1 of 1"
        assert page._find_matches[0].row_index == 1 and page._find_matches[0].spans == (((1, -1), 53, 0, 6),)
        until(lambda: 1 in page.bound and 'underline="single"' in page.bound[1][1].get_label())
        page.clear_find(focus_transcript=False)
        show_synthetic(timed)
        until(lambda: page.scroller.get_vadjustment().get_upper() > 5000)
        adjustment = page.scroller.get_vadjustment()
        adjustment.set_value(1500)
        until(lambda: adjustment.get_value() > 1000 and page._header_scrolled)
        assert not page.full_header.get_visible() and page.compact_header_box.get_visible()
        assert page.scroll_fade.get_visible() and page.top_bar.has_css_class("scrolled")
        page.settings.set_boolean("companion-card-dismissed", True)
        page.set_connected(False)
        shot(window, "-scrolled")
        page.settings.set_boolean("companion-card-dismissed", False)
        page.set_connected(False)
        frames = []
        page.list.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 2)[1])
        until(lambda: len(frames) >= 2)

        def visible_anchor():
            candidates = []
            for turn, _words, row in page.bound.values():
                success, bounds = row.compute_bounds(page.scroller)
                if success and bounds.get_y() + bounds.get_height() > 0 and bounds.get_y() < page.scroller.get_height():
                    candidates.append((turn.index, bounds.get_y()))
            return min(candidates)

        anchor = visible_anchor()
        stable = page.bound[anchor[0]][2]
        grown = timed + (Word(meeting.speakers[0].id, 400000, 400900, "New words"),)
        show_synthetic(grown)
        frames = []
        page.list.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 2)[1])
        until(lambda: len(frames) >= 2)
        until(lambda: page.list.ready)
        assert page.bound[anchor[0]][2] is stable
        after = visible_anchor()
        assert after[0] == anchor[0] and abs(after[1] - anchor[1]) < 2, (after, anchor)
        # Resuming after reading elsewhere must return to the active turn, even
        # when a paused model update already computed that same timed item.
        page.play.emit("clicked")

        def active_visible():
            for turn, _words, row in page.bound.values():
                if turn.index == page._reading_index:
                    success, bounds = row.compute_bounds(page.scroller)
                    return success and 0 <= bounds.get_y() < page.scroller.get_height()
            return False

        until(lambda: window.player.playing and active_visible())
        page.play.emit("clicked")
        adjustment.set_value(0)
        until(lambda: not page._header_scrolled and page.full_header.get_visible())
        assert not page.compact_header_box.get_visible() and not page.scroll_fade.get_visible()
        style = Adw.StyleManager.get_default()
        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        def transcript_colours(dark):
            if not page.bound:
                return False
            for turn, _words, row in page.bound.values():
                name = row.get_first_child().get_first_child()
                caption = name.get_child() if isinstance(name, Gtk.Button) else name
                expected = speaker_colour(page.meeting.speaker_for(turn.speaker), dark=dark)
                if expected not in caption.get_label():
                    return False
            return True

        until(lambda: style.get_dark() and transcript_colours(True))
        dark_first = speaker_colour(meeting.speakers[0], dark=True)
        assert page._colour(meeting.speakers[0]) == dark_first
        assert page._speaker_rows[meeting.speakers[0].id][0].get_mapped()
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        until(lambda: not style.get_dark() and transcript_colours(False))
    page.update(meeting)
    assert page._companion.get_visible()
    page.set_connected(True)
    assert not page._companion.get_visible()
    page.set_connected(False)
    page._dismiss_card()
    start_listening(window)
    window.back_to_library()
    until(lambda: window.meeting_page is None)
    assert_stopped_listening(window)
    window.open_meeting(meeting)
    page = window.meeting_page
    assert not page._companion.get_visible(), "Not now must persist across page instances"
    metadata = meeting.directory / "meeting.json"
    value = json.loads(metadata.read_text())
    value["transcript"]["status"] = "failed"
    value["etaMinutes"] = None
    metadata.write_text(json.dumps(value))
    until(lambda: window.meeting_page.meeting.transcript == "failed")
    assert "Could not write this meeting down. The audio is fine." in labels(page.lines)
    start_listening(window)
    window.toggle_search()
    until(lambda: window.search_page is not None and window.meeting_page is None)
    assert_stopped_listening(window)
    window.back_to_library()
    until(lambda: window.search_page is None)
    received = next(m for m in window.snapshot.meetings if m.origin == "received")
    app.open_in_new_window(Gio.File.new_for_path(str(received.audio_file)))
    until(lambda: window.meeting_page is not None and window.selected and window.selected.key == received.key)
    window.open_meeting(received)
    until(lambda: window.meeting_page is not None)
    assert window.meeting_page.meeting.words
    assert not isinstance(window.player, FolderPlayer), "Received meeting must exercise player replacement"
    assert window.player.rate == 1.5 and window.meeting_page.rate_button.get_label() == "1.5×"
    subprocess.run(["xdotool", "key", "ctrl+bracketright"], check=True)
    until(lambda: window.player.rate == 1.75)
    assert window.meeting_page.rate_button.get_label() == "1.75×"
    app.on_shortcuts()
    until(lambda: app.shortcuts_dialog and app.shortcuts_dialog.get_mapped())
    shortcut_labels = labels(app.shortcuts_dialog)
    assert "Increase playback speed" in shortcut_labels and "Decrease playback speed" in shortcut_labels
    assert "Preferences" in shortcut_labels and "Back to Library" in shortcut_labels
    app.shortcuts_dialog.close()
    until(lambda: app.shortcuts_dialog is None)

    assert len(app.get_windows()) == 1, "File opens belong to the pushed reading page"
    # Library files opened via Open/CLI remain attached to live model updates.
    own = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    start_listening(window)
    received_player = window.player
    app.open_in_new_window(Gio.File.new_for_path(str(own.directory / "session.json")))
    until(lambda: window.meeting_page and window.meeting_page.meeting.key == own.key)
    assert not window._external_meeting
    assert window.player is not received_player, "Opening a folder meeting replaces the file player"
    assert_stopped_listening(window)
    changed = json.loads((own.directory / "meeting.json").read_text())
    changed.update(title="Live Library rename", titleSource="user")
    (own.directory / "meeting.json").write_text(json.dumps(changed))
    until(lambda: window.meeting_page.meeting.title == "Live Library rename")
    # An outside folder is read-only; the underlying Library keeps refreshing.
    from cassini_talk.recording_session import RecordingSession

    outside = Path(temporary) / "outside"
    outside.mkdir()
    session = RecordingSession(outside)
    session.add_track("local", "Mic", "local", 48000, 1)
    session.append("local", bytes(9600), 0)
    session.finish()
    app.open_in_new_window(Gio.File.new_for_path(str(session.directory / "session.json")))
    until(lambda: window.meeting_page and window.meeting_page.meeting.directory == session.directory)
    assert window._external_meeting and not window.meeting_page.meeting.metadata_editable
    assert not window.meeting_page.write_button.get_visible()
    changed["title"] = "Updated underneath external reading"
    (own.directory / "meeting.json").write_text(json.dumps(changed))
    until(lambda: any(m.title == changed["title"] for m in window.snapshot.meetings))
    assert window.meeting_page.meeting.directory == session.directory
    window.back_to_library()
    assert any(m.title == changed["title"] for m in window.snapshot.meetings)

    # Single-click row selection must initialize the selected meeting's player for
    # both timeline pointer seeking (before Play) and immediate speaker sampling.
    single_click_target = next(
        m
        for m in window.snapshot.meetings
        if m.origin == "own"
        and m.metadata_editable
        and m.has_audio
        and len(m.speakers) >= 2
        and any(s.sample_span for s in m.speakers)
    )
    other_playable = next(
        m
        for m in window.snapshot.meetings
        if m.key != single_click_target.key and m.has_audio and m.lifecycle == "saved"
    )
    window.rows.select_row(window._row_by_key[other_playable.key])
    until(lambda: window._loaded_key == other_playable.key and window.player.loaded and window.player._prerolled)
    window.rows.select_row(window._row_by_key[single_click_target.key])
    until(
        lambda: (
            window.meeting_page is not None
            and window.meeting_page.meeting.key == single_click_target.key
            and window._loaded_key == single_click_target.key
            and window.player.loaded
            and window.player._prerolled
            and window.meeting_page.timeline.scale.get_width() > 40
        )
    )
    sc_page = window.meeting_page
    assert not window.player.playing and window.player.position == 0
    tl_left, tl_width = sc_page.timeline._geometry()
    sc_page.timeline._begin(sc_page.timeline.gesture, tl_left + max(12.0, tl_width * 0.5), 4.0)
    sc_page.timeline._end(sc_page.timeline.gesture, 0.0, 0.0)
    until(lambda: window.player.position > 0 and window._loaded_key == single_click_target.key)
    sample_speaker = next(s for s in single_click_target.speakers if s.sample_span)
    sc_page.name_speakers(sample_speaker.id)
    until(
        lambda: (
            sc_page.speakers_dialog is not None
            and sc_page.speakers_dialog.get_mapped()
            and sc_page.speakers_dialog.play.get_sensitive()
        )
    )
    assert sc_page.speakers_dialog._can_play()
    sc_page.speakers_dialog.play.emit("clicked")
    until(lambda: sc_page.speakers_dialog.sample_active and window.player.playing)
    sc_page.speakers_dialog.close()
    until(lambda: sc_page.speakers_dialog.closed and not window.player.playing)

    # Switching meetings while a listening-bar speaker merge is in flight must
    # recompute the originating meeting's directory and suppress A's UI completion on B.
    import threading
    from unittest.mock import patch

    from cassini_talk import speaker_edits

    entered_merge = threading.Event()
    release_merge = threading.Event()
    real_merge_into = speaker_edits.merge_into
    recomputed_calls = []
    switched_toasts = []
    real_recompute = app.recompute_voices
    real_add_toast = window.toasts.add_toast

    def gated_merge(directory, src_id, dst_id, *, scanner=None):
        entered_merge.set()
        assert release_merge.wait(10), "Timed out waiting to release gated merge"
        return real_merge_into(directory, src_id, dst_id, scanner=scanner)

    merge_src = single_click_target.speakers[-1].id
    merge_dst = single_click_target.speakers[0].id
    with (
        patch.object(speaker_edits, "merge_into", side_effect=gated_merge),
        patch.object(
            app,
            "recompute_voices",
            side_effect=lambda directory, pair: (
                recomputed_calls.append((directory, pair)),
                real_recompute(directory, pair),
            )[1],
        ),
        patch.object(
            window.toasts,
            "add_toast",
            side_effect=lambda toast: (switched_toasts.append(toast.get_title()), real_add_toast(toast))[1],
        ),
    ):
        sc_page.merge_speaker(merge_src, merge_dst)
        until(lambda: entered_merge.is_set())
        window.rows.select_row(window._row_by_key[other_playable.key])
        until(lambda: sc_page.meeting.key == other_playable.key)
        assert sc_page._speaker_future is None
        switched_toasts.clear()
        release_merge.set()
        until(
            lambda: (
                len(recomputed_calls) == 1
                and any(
                    m.key == single_click_target.key and m.metadata.get("speakerMerges")
                    for m in window.snapshot.meetings
                )
                and not window._pending
            )
        )
        assert recomputed_calls == [(single_click_target.directory, (merge_src, merge_dst))]
        assert not switched_toasts
        assert sc_page._merge_transition is None

    # Transcript turn headers must use speaker slot inks and secondary timestamp ink
    # without Libadwaita's permanent link underline class (HARMONY 10.10 / WP3 L-43).
    from cassini_talk.tokens import token

    transcript_meeting = next(m for m in window.snapshot.meetings if m.words)
    window.rows.select_row(window._row_by_key[transcript_meeting.key])
    until(
        lambda: (
            window.meeting_page is not None
            and window.meeting_page.meeting.key == transcript_meeting.key
            and bool(window.meeting_page.bound)
        )
    )
    tr_page = window.meeting_page

    def _walk_widgets(root):
        yield root
        child = root.get_first_child()
        while child is not None:
            yield from _walk_widgets(child)
            child = child.get_next_sibling()

    bound_widgets = [w for _turn, _words, row in tr_page.bound.values() for w in _walk_widgets(row)]
    name_buttons = [w for w in bound_widgets if "transcript-name" in w.get_css_classes()]
    time_buttons = [w for w in bound_widgets if "transcript-time" in w.get_css_classes()]
    assert name_buttons and time_buttons
    for btn in name_buttons:
        assert "link" not in btn.get_css_classes()
        lbl = btn.get_child()
        assert isinstance(lbl, Gtk.Label) and any(c.startswith("speaker-ink-") for c in lbl.get_css_classes())
    for scheme, is_dark in ((Adw.ColorScheme.FORCE_LIGHT, False), (Adw.ColorScheme.FORCE_DARK, True)):
        Adw.StyleManager.get_default().set_color_scheme(scheme)
        until(lambda expected=is_dark: Adw.StyleManager.get_default().get_dark() == expected)
        ink2_hex = token("ink-2", dark=is_dark).lstrip("#")
        expected_ink2 = tuple(int(ink2_hex[i : i + 2], 16) for i in (0, 2, 4))
        for btn in time_buttons:
            assert "link" not in btn.get_css_classes()
            rgba = btn.get_color()
            actual_rgb = (round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255))
            assert all(abs(a - b) <= 2 for a, b in zip(actual_rgb, expected_ink2, strict=True)), (
                f"Transcript time colour mismatch (dark={is_dark}): got {actual_rgb}, expected {expected_ink2}"
            )
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    until(lambda: not Adw.StyleManager.get_default().get_dark())

    # Real confirmation widgets, deterministic controller double; no device access.
    class Controller(GObject.Object):
        __gsignals__ = {name: (GObject.SignalFlags.RUN_LAST, None, (str,)) for name in ("finished", "error")}
        pipeline = object()
        session = None
        line = None
        minutes_left = None

        def stop(self):
            self.pipeline = None
            GLib.idle_add(lambda: (self.emit("finished", str(session.directory)), False)[1])

    recorder = Controller()
    app.recorder = recorder
    app.on_quit()
    assert app._capture_confirmation
    dialog = app._capture_confirmation
    dialog.emit("response", "keep")
    dialog.force_close()
    until(lambda: not window.get_visible())
    assert recorder.pipeline is not None and app._capture_background
    app.activate()
    assert window.get_visible()
    app.on_quit()
    dialog = app._capture_confirmation
    dialog.emit("response", "stop")
    dialog.force_close()
    try:
        until(lambda: not app.get_windows())
    except AssertionError:
        print([(type(w).__name__, w.get_visible()) for w in app.get_windows()], flush=True)
        raise
print(
    "Meeting GUI passed: timed reading, folder/Opus navigation, playback, card persistence, live updates and close confirmation"
)
