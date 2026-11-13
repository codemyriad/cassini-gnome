#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Production Recording page with real metadata edits in a private display."""

import faulthandler
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
sys.path.insert(0, str(ROOT / "tests"))
from desktop_services import DesktopServices  # noqa: E402
from painted import apply_screenshot_scheme  # noqa: E402

from cassini_talk.global_shortcut import GlobalShortcut  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.recording_details import SilenceClock  # noqa: E402


def until(predicate):
    context = GLib.MainContext.default()
    while not predicate():
        context.iteration(True)  # Dispatch an event; the outer process guard handles hangs.


# Diagnostic only. The existing screenshot subprocess watchdog is the sole
# time-based hang guard; no per-condition deadline determines success (D88).
faulthandler.dump_traceback_later(60, repeat=True)


services = DesktopServices()
until(
    lambda: services.bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", ("org.gtk.Notifications",)),
        GLib.VariantType.new("(b)"),
        Gio.DBusCallFlags.NONE,
        1000,
        None,
    ).unpack()[0]
)
activations = []
shortcut = GlobalShortcut(lambda: activations.append(True), connection=services.bus)
until(lambda: shortcut.bound)
assert [c["method"] for c in services.calls] == ["CreateSession", "ListShortcuts", "BindShortcuts"]
assert services.calls[2]["values"][1][0][1]["preferred_trigger"] == "CTRL+ALT+r"
services.activate(session="/wrong/session")
services.activate(identifier="wrong")
services.activate()
until(lambda: activations)
assert activations == [True]
shortcut.close()
until(lambda: services.closed)
services.activate()
services.denied = True
denied = GlobalShortcut(lambda: activations.append(False), connection=services.bus)
until(lambda: denied.error)
assert not denied.bound and activations == [True]
denied.close()
services.denied = False

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
    os.environ["CASSINI_LIBRARY_DIR"] = str(tmp / "fixtures/main/root")
    os.environ["CASSINI_SCREENSHOT_ACTIVITY"] = str(tmp / "fixtures/main/activity.json")
    os.environ["CASSINI_SCREENSHOT_NOW"] = "1793107080000"
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schemas)
    apply_screenshot_scheme()
    app = CassiniApplication("com.gocassini.RecordingPageTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending)
    live = next(m for m in window.snapshot.meetings if m.lifecycle == "recording")
    before = {p.name: p.read_bytes() for p in live.directory.iterdir() if p.name != "meeting.json"}
    # An old finalizer may be delivered after the next capture has started.
    # It must not consume the next capture's pending navigation request.
    from types import SimpleNamespace

    old = SimpleNamespace(
        session=None,
        pipeline=None,
        starting=False,
        stopping=False,
        _finishing=False,
        line=None,
        minutes_left=90,
        levels=[0.0, 0.0],
        close=lambda: None,
    )
    app._pending_recording = str(live.directory)
    app._native_finished(old, str(Path(tmp) / "Recording-older"))
    assert app._pending_recording == str(live.directory), "Old finish discarded new Recording-page navigation"
    app._native_finished(old, str(live.directory))
    assert app._pending_recording is None, "Matching finish must retire its own pending request"
    app._last_native = None
    window.open_meeting(live)
    page = window.recording_page
    assert page and not window.meeting_page and page.timer.get_label() == "00:12:48"
    assert window.detail_stack.get_visible_child_name() == "recording"
    assert page.get_parent() is window.recording_slot
    assert window.navigation.get_navigation_stack().get_n_items() == 1
    until(lambda: not window.split.get_collapsed() and window.sidebar_header.get_mapped() and page.get_mapped())
    assert page.capture is window.capture_header
    assert not page.back_button.get_visible()
    assert not hasattr(page, "details_button")
    assert any(
        page.more_menu.get_item_attribute_value(i, "label", None).unpack() == "Recording details…"
        for i in range(page.more_menu.get_n_items())
    )
    assert page.subtitle_label.get_label().startswith("Today, ")
    assert not isinstance(page.mic_row.get_first_child(), Gtk.Image)
    assert not isinstance(page.others_row.get_first_child(), Gtk.Image)
    change_box = page.change_sources.get_child()
    assert isinstance(change_box.get_first_child(), Gtk.Image)
    assert isinstance(change_box.get_last_child(), Gtk.Image)
    assert page.title_stack.get_visible_child_name() == "view"
    page.rename_button.emit("clicked")
    assert page.title_stack.get_visible_child_name() == "edit"
    page.finish_rename()
    assert page.title_stack.get_visible_child_name() == "view"

    # Single-clicking a saved row vs live row swaps the right pane inside the split view.
    saved_row = next(r for r in window._row_by_key.values() if r.meeting.lifecycle == "saved")
    live_row = window._row_by_key[live.key]
    window.rows.select_row(saved_row)
    assert window.recording_page is None and window.meeting_page is not None
    assert window.detail_stack.get_visible_child_name() == "meeting"
    window.rows.select_row(live_row)
    page = window.recording_page
    assert page is not None and window.meeting_page is None
    assert window.detail_stack.get_visible_child_name() == "recording"

    # Narrow collapsed split-view shows the back button and returns to the list on win.back-library.
    window.set_default_size(690, 720)
    until(lambda: window.split.get_collapsed() and page.back_button.get_visible())
    window.activate_action("win.back-library", None)
    assert window.recording_page is None and not window.split.get_show_content()
    window.set_default_size(1120, 720)
    until(lambda: not window.split.get_collapsed())
    window.open_meeting(live)
    page = window.recording_page
    assert page and not page.back_button.get_visible()
    from unittest.mock import patch

    with patch.object(Gio.AppInfo, "create_from_commandline", side_effect=GLib.Error("No control centre")):
        page._check_sound()
    assert page.stop.get_sensitive()
    assert not window.lookup_action("export").get_enabled()
    window._play_selected()
    assert not window.player.playing
    page.title_entry.set_text("Renamed while recording")
    assert not hasattr(page, "notes_view") and not hasattr(page, "notes")
    until(lambda: json.loads((live.directory / "meeting.json").read_text())["title"] == "Renamed while recording")
    data = json.loads((live.directory / "meeting.json").read_text())
    assert data["notes"] == [dict(atMs=700000, text="Decision on the travel budget around here")]
    assert data["notes"][0] == dict(atMs=700000, text="Decision on the travel budget around here")
    assert page.headline.get_label() == "Renamed while recording"
    # GTK4 focuses the Entry's inner GtkText. A scanner refresh after the
    # debounce must not reset the user's caret or current selection.
    until(lambda: not page._dirty and not page._saving)
    page.title_entry.grab_focus()
    until(lambda: window.get_focus() is not None and window.get_focus().is_ancestor(page.title_entry))
    page.title_entry.set_position(-1)
    position = page.title_entry.get_position()
    page.update(page.meeting)
    assert page.title_entry.get_position() == position, "Refresh reset the focused title caret"
    page.title_entry.select_region(0, -1)
    selection = page.title_entry.get_selection_bounds()
    page.update(page.meeting)
    assert page.title_entry.get_selection_bounds() == selection, "Refresh cleared the title selection"
    from dataclasses import replace

    current = page.meeting
    page.update(replace(current, title="An earlier scanner title"))
    assert page.title_entry.get_text() == "Renamed while recording", "Refresh overwrote an active edit"
    page.meeting = current
    window.set_focus(None)
    changes = []
    handler = page.title_entry.connect("changed", lambda *_: changes.append(True))
    page.update(current)
    page.title_entry.disconnect(handler)
    assert not changes, "An unchanged unfocused title was rewritten"
    assert all((live.directory / name).read_bytes() == data for name, data in before.items())
    app.comfort.states[live.key] = dict(levels=[0.0, 0.6], voices=4, silent=False, line=None, minutes=50)
    app.comfort.clocks[live.key] = SilenceClock(180)
    with patch.object(GLib, "get_monotonic_time", return_value=200_000_000):
        app.comfort.tick()
    assert page.check.get_visible() and page.mic_warning.get_visible()
    assert page.others_caption.get_label() == "Listening inside your browser · 4 voices"
    app.comfort.states[live.key].update(line="errors.diskLow.line", minutes=29)
    page.update_status()
    assert page.lines.get_visible()
    # A muted system fallback shows the exact catalog warning on the page
    # and the live pill. Browser-only capture never inherits the sink's mute.
    from cassini_talk.session_events import source_state
    from cassini_talk.strings import text

    original_sources = app.comfort.sources
    app.comfort.sources = lambda _meeting: source_state(
        [
            dict(type="selection", atMs=0, me=True, others=True, reason="start"),
            dict(type="othersSource", atMs=0, source="system", reason="lost"),
        ]
    )
    app.comfort.states[live.key].update(output_muted=True, line=None)
    page.update_status()
    window._update_live()

    def texts(widget):
        result = [widget.get_label()] if hasattr(widget, "get_label") else []
        child = widget.get_first_child()
        while child:
            result.extend(texts(child))
            child = child.get_next_sibling()
        return result

    assert text("errors.othersFallback.line") in texts(page.lines), "Persistent fallback info missing"
    assert not window.recording_lines.get_visible(), "Sidebar must not duplicate right-pane Recording info lines"
    assert not any("Recording since" in t for t in texts(page) if t)
    assert "Named from the meeting window. Rename it any time." not in texts(page)
    fallback_line = next(
        child
        for child in (page.lines.get_first_child(), page.lines.get_last_child())
        if text("errors.othersFallback.line") in texts(child)
    )
    assert fallback_line.has_css_class("info-line"), "Fallback is information, not an amber refusal"
    assert text("recording.systemMuted") in texts(page.lines), "Muted output warning missing on Recording page"
    assert any(c["muted"].get_visible() for c in window._capture_headers), "Muted output warning missing on live pill"
    unchanged = page.lines.get_first_child()
    page.update_status()
    assert page.lines.get_first_child() is unchanged, "Unchanged warning rebuilt its widgets"
    app.comfort.sources = original_sources
    page.update_status()
    window._update_live()
    assert text("errors.othersFallback.line") not in texts(page.lines), "Restored browser kept stale fallback info"
    assert text("recording.systemMuted") not in texts(page.lines), "Browser capture inherited system mute"
    assert not any(c["muted"].get_visible() for c in window._capture_headers)
    # Closing/switching flushes a pending keystroke edit before debounce.
    page.title_entry.set_text("Renamed just before closing")
    window.back_to_library()
    # Reopen the stale scanner object before its debounce; the pending draft
    # must preserve the pending title and the pre-existing notes.
    window.open_meeting(live)
    reopened = window.recording_page
    assert reopened.title_entry.get_text() == "Renamed just before closing"
    reopened.update(live)
    assert reopened.title_entry.get_text() == "Renamed just before closing"
    from concurrent.futures import Future

    failed = Future()
    failed.set_exception(OSError("Synthetic save failure"))
    reopened._saving += 1
    reopened._saved(failed, dict(title=reopened.title_entry.get_text()))
    assert reopened._dirty and reopened._title_dirty

    reopened.title_entry.set_text("Title after reopening")
    window.back_to_library()
    until(lambda: json.loads((live.directory / "meeting.json").read_text())["title"] == "Title after reopening")
    saved = json.loads((live.directory / "meeting.json").read_text())["notes"]
    assert saved == [dict(atMs=700000, text="Decision on the travel budget around here")]
    assert not window.recording_page
    # The sidebar and header share the native monotonic clock even when the
    # wall clock moves a full day forwards or backwards.
    from types import SimpleNamespace

    original_now = window._fixed_now
    original_badge = window._live_badges[live.key]
    with patch.object(GLib, "get_monotonic_time", return_value=65_500_000):
        app.recorder = SimpleNamespace(
            session=SimpleNamespace(directory=live.directory, manifest={"events": []}),
            pipeline=object(),
            _start_us=0,
            levels=[0.2, 0.4],
            line=None,
            minutes_left=50,
            stopping=False,
        )
        for shift in (-86_400_000, 86_400_000):
            window._fixed_now = original_now + shift
            window._row(live)
            assert window._live_badges[live.key].get_label() == "Recording 1:05"
            window._update_live()
            assert window._live_badges[live.key].get_label() == "Recording 1:05"
            assert all(c["timer"].get_label() == "1:05" for c in window._capture_headers)
    app.native_captures.clear()  # Retire the injected clock-only fixture controller.
    app.recorder = None
    window._fixed_now = original_now
    window._live_badges[live.key] = original_badge
    window._update_live()
    # GNotification travels over the private bus, not a mocked Python method.
    window.open_meeting(live)
    window.set_visible(False)
    app.comfort.tick()
    until(lambda: "recording" in services.notifications)
    payload = services.notifications["recording"]
    assert payload["title"] == "Cassini is recording"
    assert [b["label"] for b in payload["buttons"]] == ["Stop", "Show"]
    assert [b["action"] for b in payload["buttons"]] == ["app.stop-recording", "app.open-recording"]
    window._fixed_now += 1000
    app.comfort.tick()
    until(lambda: services.notifications["recording"]["title"] == "Cassini is recording")
    assert "Recording since" in services.notifications["recording"]["body"]
    app.activate_action("open-recording", None)
    until(lambda: window.get_visible() and "recording" not in services.notifications)
    window.back_to_library()
    window._fixture_activity = {}
    window._activity = {}
    window.monitor.capture_activity = lambda: {}
    window.monitor.refresh_now()
    until(lambda: not window._pending and all(m.lifecycle != "recording" for m in window.snapshot.meetings))
    # A producer's lost Stop reaches the real persistent info line and desktop
    # notification over the private bus, even with the Recording page visible.
    from cassini_talk.browser_capture import BrowserCapture
    from cassini_talk.recording_details import live_meeting

    class ReadyNativeTap:
        # This display fixture owns synthetic readiness PCM. Importing the unit
        # suite for its Tap would also load unrelated shared-vector fixtures.
        def __init__(self, kind, origin, append, failed, *, level):
            self.append = append

        def start(self):
            self.append(b"\x01\x00" * 4, 100)

        def stop(self):
            pass

    window.monitor.close()
    capture = BrowserCapture(
        lambda *_: None, parent=Path(os.environ["CASSINI_LIBRARY_DIR"]), tap_factory=ReadyNativeTap
    )
    capture.elapsed_ms = lambda: 1000
    capture.start(dict(type="start", captureId="visible-loss", timelineOriginMs=1, microphone=True))
    capture.handle(dict(type="track", id="voice", kind="remote", sampleRate=48000, channels=1))
    capture.handle(dict(type="audio", id="voice", sequence=0, offsetMs=0, pcm="AQACAA=="))
    app._browser_snapshot = dict(statuses=[dict(recording=True, automatic=True, path=str(capture.directory))])
    current = live_meeting(capture.directory)
    app.comfort.known[current.key] = current
    # Direct opens promote the loaded capture before activity replays the
    # cached Library snapshot (the production _opened_file path does this).
    # Discovery is intentionally closed here so it cannot rescue this fixture.
    window._library_loaded(current)
    window.open_meeting(current)
    page = window.recording_page
    alert = "recording-fallback-" + current.key
    assert text("errors.othersFallback.line") not in texts(page.lines)
    assert alert not in services.notifications
    capture.handle(dict(type="stop", failureReason="lost", cause="frameError", captureId="visible-loss"))
    identifier, tap = capture.native_taps["system"]
    track = next(t for t in capture.host.session.manifest["tracks"] if t["id"] == identifier)
    tap.append(bytes(9600), track["startOffsetMs"])
    app.comfort.tick()
    # Force the one-second activity callback before the queued observation is
    # delivered. An undiscovered capture used to disappear here, leaving this
    # test waiting for a line on a closed page while its notification arrived.
    window._activity_tick()
    assert window.recording_page is page and not page._closed, "Activity replay closed the live Recording page"

    def fallback_wait_diagnostics():
        # Diagnostic timer only: never terminates the wait or decides pass/fail.
        # Keep both async observations and their sources for an outer hang.
        from cassini_talk.recording_details import disk_reserve

        known = app.comfort.known.get(current.key)
        volume = os.statvfs(capture.directory)
        evidence = dict(
            expected_line=text("errors.othersFallback.line"),
            expected_alert=alert,
            lines=list(texts(page.lines)),
            page_closed=page._closed,
            current_page=window.recording_page is page,
            page_line_signature=page._line_signature,
            snapshot_has_capture=any(m.key == current.key for m in window.snapshot.meetings),
            notifications=list(services.notifications),
            statuses=app._browser_snapshot.get("statuses", []),
            activity=window._activity,
            known_events=list(known.capture_events) if known else None,
            session_events=capture.host.session.manifest.get("events"),
            source_state=repr(app.comfort.sources(current)),
            observations=app.comfort.states.get(current.key),
            observation_pending=list(app.comfort.futures),
            capture_finished=capture.finished,
            fallback_pending=capture.fallback_pending,
            free_bytes=volume.f_bavail * volume.f_frsize,
            reserve_bytes=disk_reserve(volume.f_blocks * volume.f_frsize),
        )
        print("Fallback wait diagnostics: " + json.dumps(evidence, ensure_ascii=False), flush=True)
        Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".fallback.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        )
        return False

    diagnostic = GLib.timeout_add_seconds(55, fallback_wait_diagnostics)
    try:
        until(lambda: text("errors.othersFallback.line") in texts(page.lines) and alert in services.notifications)
    except BaseException:
        fallback_wait_diagnostics()
        raise
    finally:
        if GLib.MainContext.default().find_source_by_id(diagnostic):
            GLib.source_remove(diagnostic)
    assert window.get_visible()
    assert services.notifications[alert]["title"] == text("errors.othersFallback.line")
    assert services.notifications[alert]["body"] == current.title
    info = page.lines.get_last_child()
    page.update_status()
    app.comfort.tick()
    assert page.lines.get_last_child() is info, "Fallback info must persist across unchanged observations"
    current = live_meeting(capture.directory)
    window._library_loaded(current)
    window.show_recording_details()
    dialog = window.recording_details_dialog
    expected = text("details.othersFallback.cause.frameError")
    until(lambda: dialog.report and expected in dialog.report.plain_text())
    dialog.close()
    assert sum(item["id"] == alert for item in services.history) == 1, "Polling repeated the loss alert"
    capture.stop(at_ms=1500)
    app._browser_snapshot = dict(statuses=[])
    saved_capture = live_meeting(capture.directory, "saved")
    window._library_loaded(saved_capture)
    app.comfort._fallback_notification(saved_capture)
    assert sum(item["id"] == alert for item in services.history) == 1, "Normal Stop sent another loss alert"
    # The real global Stop action sees a broker reservation before a session
    # folder exists. The headless broker regression orders Stop against input
    # startup; here the actual GAction must dispatch it from the desktop.
    app._browser_snapshot = dict(
        statuses=[
            dict(
                id="capture:signal-pending",
                captureId="signal-pending",
                path=None,
                recording=True,
                starting=True,
            )
        ]
    )
    stop_calls = []
    original_stop = app.stop_browser_recordings
    app.stop_browser_recordings = lambda: stop_calls.append(True)
    try:
        app.activate_action("stop-recording", None)
        assert stop_calls == [True], "Global Stop missed the pending signal recording"
    finally:
        app.stop_browser_recordings = original_stop
        app._browser_snapshot = dict(statuses=[])

    # D74 is a system route from the start, with quiet persisted provenance;
    # it must never display the browser-loss info or send its warning alert.
    signal = BrowserCapture(lambda *_: None, parent=Path(os.environ["CASSINI_LIBRARY_DIR"]), tap_factory=ReadyNativeTap)
    signal.signal_owned = True
    signal.elapsed_ms = lambda: 1000
    signal.start(
        dict(type="start", captureId="visible-signal", timelineOriginMs=1, microphone=True),
        others_source="system",
        reason="start",
        others_route=dict(source="system", reason="mixedSiteSignal", site="Synthetic meeting"),
    )
    for identifier, tap in signal.native_taps.values():
        track = next(t for t in signal.host.session.manifest["tracks"] if t["id"] == identifier)
        tap.append(bytes(96000), track["startOffsetMs"])
    app._browser_snapshot = dict(statuses=[dict(recording=True, automatic=True, path=str(signal.directory))])
    current = live_meeting(signal.directory)
    app.comfort.known[current.key] = current
    # Direct opens promote the loaded capture before activity replays the
    # cached Library snapshot (the production _opened_file path does this).
    # Discovery is intentionally closed here so it cannot rescue this fixture.
    window._library_loaded(current)
    window.open_meeting(current)
    app.comfort.tick()
    assert current.capture_source == "gnome" and current.key in app.comfort.active()
    assert text("errors.othersFallback.line") not in texts(window.recording_page.lines)
    assert "recording-fallback-" + current.key not in services.notifications
    window.show_recording_details()
    dialog = window.recording_details_dialog
    expected = text("details.othersRoute.mixedGeneric")
    until(
        lambda: dialog.report and expected in dialog.report.notes and dialog.get_mapped() and expected in texts(dialog)
    )

    def save_window_shot(path):
        painted = []
        frame_clock = window.get_frame_clock()
        handler = frame_clock.connect("after-paint", lambda *_: painted.append(True))
        try:
            window.queue_draw()
            until(lambda: painted)
        finally:
            frame_clock.disconnect(handler)
        pictures = []

        def snapshot():
            image = Gtk.Snapshot.new()
            Gtk.WidgetPaintable.new(window).snapshot(image, window.get_width(), window.get_height())
            node = image.to_node()
            if node is None:
                return False
            pictures.append(node)
            return True

        until(snapshot)
        node = pictures[-1]
        assert window.get_renderer().render_texture(node, node.get_bounds()).save_to_png(str(path))

    if output := os.environ.get("CASSINI_SCREENSHOT_OUT"):
        save_window_shot(output)
    dialog.close()
    signal.stop(at_ms=2500)
    window._library_loaded(live_meeting(signal.directory, "saved"))
    app._browser_snapshot = dict(statuses=[])

    # Pressing Stop while a browser recording is muxing to .opus must disable Stop
    # and flip RecordingPage + LibraryWindow to "Saving…" on the exact click frame.
    import threading

    from cassini_talk import capture_guard

    previous_animations = Gtk.Settings.get_default().props.gtk_enable_animations
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    app.recording_error = None
    mux_capture = BrowserCapture(
        lambda *_: None, parent=Path(os.environ["CASSINI_LIBRARY_DIR"]), tap_factory=ReadyNativeTap
    )
    mux_capture.elapsed_ms = lambda: 768_000
    mux_capture.start(
        dict(
            type="start",
            captureId="mux-saving-feedback",
            title="Weekly sync, product & engineering",
            timelineOriginMs=1_793_106_312_000,
            microphone=True,
        )
    )
    mux_capture.handle(dict(type="track", id="voice", kind="remote", sampleRate=48000, channels=1))
    mux_capture.handle(dict(type="audio", id="voice", sequence=0, offsetMs=0, pcm="AQACAA=="))
    mux_meeting = replace(live_meeting(mux_capture.directory), started_at_ms=1_793_106_312_000)
    app.comfort.states[mux_meeting.key] = dict(levels=[0.35, 0.55], voices=3, silent=False, line=None, minutes=50)
    app._browser_snapshot = dict(statuses=[dict(recording=True, stopping=False, path=str(mux_capture.directory))])
    window._activity_source = SimpleNamespace(
        path=None,
        snapshot={mux_meeting.id: "recording"},
        paths={mux_meeting.id: str(mux_capture.directory)},
        connected=True,
        answered_at=1.0,
        _browser_capture_phases={},
        close=lambda: None,
    )
    import concurrent.futures

    from cassini_talk.library import LibrarySnapshot, Meeting

    pre_mux_snapshot = window._source_snapshot
    synthetic_saved = tuple(
        Meeting(
            id=f"00000000-0000-4000-8000-{i:012d}",
            directory=Path(tmp) / f"Recording-00000000-0000-4000-8000-{i:012d}",
            title=f"Archived call {i}",
            started_at_ms=1_793_106_000_000 - i * 60_000,
            duration_ms=600_000,
            lifecycle="saved",
            has_audio=True,
            quote_line="“Synthetic archived call.”",
        )
        for i in range(250)
    )
    app.comfort.known[mux_meeting.key] = mux_meeting
    window._source_snapshot = LibrarySnapshot(
        meetings=(mux_meeting, *synthetic_saved),
        directories=(mux_meeting.directory, *(m.directory for m in synthetic_saved)),
    )
    window._updated(window._source_snapshot)
    window.capture_changed()
    assert len(window.snapshot.meetings) == 251
    unrelated_rows_before = [window._row_by_key[m.key] for m in synthetic_saved]
    unrelated_children_before = [r.get_child() for r in unrelated_rows_before]
    mux_row_before = window._row_by_key[mux_meeting.key]

    window.open_meeting(mux_meeting)
    mux_page = window.recording_page
    assert mux_page is not None
    assert mux_page.stop.get_sensitive() and mux_page.stop_content.get_label() == text("common.stop")
    assert mux_page.capture._controls["stop"].get_sensitive()
    assert mux_page.capture._controls["stop"].get_label() == text("common.stop")
    assert window.live_stop.get_sensitive() and window.live_stop.get_label() == text("common.stop")

    mux_entered, release_mux = threading.Event(), threading.Event()
    original_stop_browser = capture_guard.stop_browser_captures
    original_executor = concurrent.futures.ThreadPoolExecutor
    original_row = window._row
    original_fill_row = window._fill_row
    row_builds, row_fills, stop_order = [], [], []

    class StopOrderExecutor(original_executor):
        def submit(self, fn, /, *args, **kwargs):
            if fn is gated_mux_stop:
                stop_order.append("worker_submitted")
            return super().submit(fn, *args, **kwargs)

    def tracked_row(meeting):
        row_builds.append(meeting.key)
        return original_row(meeting)

    def tracked_fill_row(row, meeting, signature=None):
        stop_order.append("row_filled")
        row_fills.append(meeting.key)
        return original_fill_row(row, meeting, signature)

    def gated_mux_stop(*_args, **_kwargs):
        mux_entered.set()
        assert release_mux.wait(10)
        return [str(mux_capture.directory)]

    concurrent.futures.ThreadPoolExecutor = StopOrderExecutor
    window._row = tracked_row
    window._fill_row = tracked_fill_row
    capture_guard.stop_browser_captures = gated_mux_stop
    try:
        mux_page.stop.emit("clicked")
        # Exact click frame — worker submitted before GTK UI update, zero unrelated rows rebuilt:
        assert stop_order == ["worker_submitted", "row_filled"]
        assert row_builds == [], "Stop must not rebuild cached Library rows"
        assert row_fills == [mux_meeting.key], "Stop must update only the stopped meeting's row"
        assert window._row_by_key[mux_meeting.key] is mux_row_before
        assert [window._row_by_key[m.key] for m in synthetic_saved] == unrelated_rows_before
        assert [r.get_child() for r in unrelated_rows_before] == unrelated_children_before
        assert not mux_page.stop.get_sensitive()
        assert mux_page.stop_content.get_label() == text("states.badge.saving")
        assert mux_page.stop.get_child() is mux_page.saving_content
        assert mux_page.saving_spinner.get_spinning()
        assert mux_page.dot.has_css_class("saving")
        assert not mux_page.capture._controls["stop"].get_sensitive()
        assert mux_page.capture._controls["stop"].get_label() == text("states.badge.saving")
        assert mux_page.capture._controls["live"].has_css_class("saving")
        assert not window.live_stop.get_sensitive()
        assert window.live_stop.get_label() == text("states.badge.saving")
        assert mux_page.meeting.lifecycle == "saving"
        mux_row_meeting = next(m for m in window.snapshot.meetings if m.key == mux_meeting.key)
        assert mux_row_meeting.lifecycle == "saving"
        assert text("states.badge.saving") in texts(mux_row_before)
        assert mux_entered.wait(10)
        until(lambda: window.recording_page is mux_page and window.detail_stack.get_visible_child_name() == "recording")
        if output := os.environ.get("CASSINI_SCREENSHOT_OUT"):
            save_window_shot(Path(output).with_name("recording-saving.png"))
        release_mux.set()
        until(lambda: not app._browser_stops)
        # Delivery while broker has not confirmed publication finished keeps 'saving' and retains all cached rows:
        assert row_builds == []
        assert window.snapshot.meetings[0].lifecycle == "saving"
        assert [window._row_by_key[m.key] for m in synthetic_saved] == unrelated_rows_before
        window._row = original_row
        window._fill_row = original_fill_row
        saved_fixtures = tuple(m for m in pre_mux_snapshot.meetings if m.lifecycle == "saved")
        window._source_snapshot = LibrarySnapshot(
            meetings=(mux_meeting, *saved_fixtures),
            directories=(mux_meeting.directory, *(m.directory for m in saved_fixtures)),
        )
        window._updated(window._source_snapshot)
        window.back_to_library()
        window.capture_changed()
        until(lambda: window.recording_page is None and window.detail_stack.get_visible_child_name() != "recording")
        assert not window.live_stop.get_sensitive()
        assert window.live_stop.get_label() == text("states.badge.saving")
        if output := os.environ.get("CASSINI_SCREENSHOT_OUT"):
            save_window_shot(Path(output).with_name("library-saving.png"))
        # Pre-scan browser Stop (before LibraryScanner discovers the folder) still shows disabled Saving…:
        window._source_snapshot = LibrarySnapshot(
            meetings=saved_fixtures, directories=tuple(m.directory for m in saved_fixtures)
        )
        window._updated(window._source_snapshot)
        app._browser_stops.add(None)
        try:
            window._update_live()
            assert window.capture_header.get_visible_child_name() == "live"
            assert not window.live_stop.get_sensitive()
            assert window.live_stop.get_label() == text("states.badge.saving")
        finally:
            app._browser_stops.discard(None)
        window._update_live()
        assert window.capture_header.get_visible_child_name() == "record"

        # Deferred native Stop during startup retains _last_native on app; once
        # finalization completes, _stop_requested clears before 'changed' so the
        # Library header restores the Record control instead of staying on Saving…
        from cassini_talk.native_recorder import NativeRecorder

        prepare_entered = threading.Event()
        release_prepare = threading.Event()
        original_prepare = NativeRecorder._prepare

        def gated_prepare(parent):
            prepare_entered.set()
            assert release_prepare.wait(10)
            return original_prepare(parent or Path(os.environ["CASSINI_LIBRARY_DIR"]))

        with patch.object(NativeRecorder, "_prepare", staticmethod(gated_prepare)):
            try:
                app.start_native(test=True)
                assert prepare_entered.wait(10)
                deferred = app.recorder
                assert deferred is not None and deferred.starting
                deferred.stop()
                window._update_live()
                assert window.capture_header.get_visible_child_name() == "live"
                assert not window.live_stop.get_sensitive()
                assert window.live_stop.get_label() == text("states.badge.saving")
                release_prepare.set()
                until(lambda: not app.native_captures and deferred.pipeline is None and not deferred._finishing)
                assert app._last_native is deferred
                assert not deferred._stop_requested and not capture_guard.recorder_stopping(deferred)
                assert window.capture_header.get_visible_child_name() == "record"
                record_button = window.capture_header.get_child_by_name("record")
                assert record_button is not None and record_button.get_visible() and record_button.get_sensitive()
                assert app.lookup_action("record").get_enabled()
            finally:
                release_prepare.set()
        app._last_native = None
    finally:
        Gtk.Settings.get_default().props.gtk_enable_animations = previous_animations
        app.comfort.states.pop(mux_meeting.key, None)
        release_mux.set()
        concurrent.futures.ThreadPoolExecutor = original_executor
        window._row = original_row
        window._fill_row = original_fill_row
        capture_guard.stop_browser_captures = original_stop_browser
        mux_capture.stop(at_ms=5000)
        window._activity_source = None
        app._browser_snapshot = dict(statuses=[])
        window._source_snapshot = pre_mux_snapshot
        window._library_loaded(live_meeting(mux_capture.directory, "saved"))

    window.close()
    app.comfort.close()
    # No Library window is required for the Browser window's controller state
    # to drive hidden notifications, meters, track changes and disk observation.
    from cassini_talk.recording_session import RecordingSession

    browser_app = CassiniApplication("com.gocassini.BrowserOnlyComfortTest", "test", "development")
    browser_app.register(None)
    browser_window = Adw.ApplicationWindow(application=browser_app)
    session = RecordingSession(Path(os.environ["CASSINI_LIBRARY_DIR"]), "Browser-only recording", source="chromium")
    session.add_track("local", "Mic", "local", 48000, 1)
    session.add_track("one", "Remote", "remote", 48000, 1)
    session.append("local", bytes(96000), 0)
    # D26.1 meters measure AC: constant PCM is DC-only silence, not a voice.
    tone = (b"\x00\x20" * 60 + b"\x00\xe0" * 60) * 400  # 400 Hz, one second.
    session.append("one", tone, 0)
    key = session.directory.name
    browser_window._browser_snapshot = dict(statuses=[dict(recording=True, path=str(session.directory))])
    browser_app.comfort.tick()
    until(lambda: key in browser_app.comfort.states and browser_app.comfort.states[key]["voices"] == 1)
    until(
        lambda: (
            "recording" in services.notifications
            and "Browser-only recording" in services.notifications["recording"]["body"]
        )
    )
    assert len(browser_app.get_windows()) == 1 and not browser_window.get_visible()
    assert browser_app.comfort.states[key]["levels"][1] > 0
    session.add_track("two", "Joined later", "remote", 48000, 1)
    session.append("two", tone, 0)
    before = {p.name: p.read_bytes() for p in session.directory.iterdir()}
    browser_app.comfort.tick()
    until(lambda: browser_app.comfort.states[key]["voices"] == 2)
    assert all((session.directory / name).read_bytes() == data for name, data in before.items())
    browser_window._browser_snapshot = dict(statuses=[])
    browser_app.comfort.tick()
    until(lambda: "recording" not in services.notifications)
    session.finish()
    # A native controller also drives notifications without a Library window.
    from types import SimpleNamespace

    native_session = RecordingSession(Path(os.environ["CASSINI_LIBRARY_DIR"]), "Native-only recording", source="gnome")
    native_session.add_track("mic", "Me", "microphone", 48000, 1)
    native_session.append("mic", bytes(96000), 0)
    browser_app.recorder = SimpleNamespace(
        session=native_session,
        pipeline=object(),
        starting=False,
        stopping=False,
        _finishing=False,
        levels=[0.2, 0.5],
        line=None,
        minutes_left=50,
        _start_us=GLib.get_monotonic_time(),
    )
    browser_app.comfort.tick()
    until(
        lambda: (
            "recording" in services.notifications
            and "Native-only recording" in services.notifications["recording"]["body"]
        )
    )
    assert native_session.directory.name in browser_app.comfort.active()
    assert len(browser_app.get_windows()) == 1 and not browser_window.get_visible()
    browser_app.recorder.pipeline = None
    native_session.finish()
    browser_app.comfort.tick()
    until(lambda: "recording" not in services.notifications)
    browser_window.close()
    browser_app.comfort.close()

services.close()
print(
    "PASS portal binding/denial/activation, GNotification/Open, Recording page without notes, title edit/flush/retry, preserved old notes, silence/disk, committed fallback info/one-shot notification/Details"
)
