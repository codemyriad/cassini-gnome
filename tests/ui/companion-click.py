#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Real setup entry points, live broker/watchers and AT-SPI event traffic."""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
assert os.environ.get("GSK_RENDERER") == "cairo"


def listener():
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    event = Atspi.EventListener.new(lambda *_: None)
    assert event.register("object:state-changed")
    Path(sys.argv[2]).touch()
    GLib.MainLoop().run()


if "--listener" in sys.argv:
    listener()
    raise SystemExit

# GTK warnings and callback exceptions can otherwise leave a passing Python
# exit status. Keep the real app in a child and make those diagnostics fatal.
if "--gtk-child" not in sys.argv:
    log_path = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".gtk.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        result = subprocess.run(
            [sys.executable, __file__, "--gtk-child"], stdout=log, stderr=subprocess.STDOUT, timeout=90
        )
    output = log_path.read_text()
    print(output, end="", flush=True)
    assert result.returncode == 0, f"Companion click child failed: {result.returncode}"
    assert not any(value in output for value in ("Gtk-WARNING", "Gtk-CRITICAL", "Traceback (most recent call last)")), (
        output
    )
    raise SystemExit

import builtins  # noqa: E402
import io  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from concurrent.futures import Future  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from dataclasses import replace  # noqa: E402
from unittest.mock import patch  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, GLib  # noqa: E402

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from desktop_services import DesktopServices  # noqa: E402

from cassini_talk import browser_service, companion, companion_bundle, library, meeting_text  # noqa: E402
from cassini_talk.browser_watch import BrowserWatch  # noqa: E402
from cassini_talk.capture_activity import BrowserActivity, read_browser_activity  # noqa: E402
from cassini_talk.companion_setup import CompanionSetup  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_text import reading_turns  # noqa: E402
from cassini_talk.portable import Word  # noqa: E402
from cassini_talk.settings_dialog import SettingsDialog  # noqa: E402

MAIN = threading.get_ident()
RUN = subprocess.run
context = GLib.MainContext.default()


class MainThreadWork:
    """CPU work per direct action or main-context dispatch, including frames."""

    def __init__(self):
        self.cpu, self.wall = [], []

    def run(self, call, *args):
        cpu_before, wall_before = time.thread_time(), time.monotonic()
        try:
            return call(*args)
        finally:
            self.cpu.append(time.thread_time() - cpu_before)
            self.wall.append(time.monotonic() - wall_before)

    def check(self, state, entry):
        assert self.cpu and max(self.cpu) < 0.1, (state, entry, "main-thread CPU", max(self.cpu))
        print(
            f"PASS {state}/{entry}: max GTK dispatch/action CPU {max(self.cpu) * 1000:.1f} ms, "
            f"wall diagnostic {max(self.wall) * 1000:.1f} ms, samples={len(self.cpu)}",
            flush=True,
        )


meter = None


def until(check):
    # Each iteration blocks for and dispatches an actual GLib event. The
    # enclosing 90 s child-process watchdog only guards a missing event.
    while not check():
        if meter is None:
            context.iteration(True)
        else:
            meter.run(context.iteration, True)


def painted(window):
    frame = window.get_frame_clock()
    seen = []
    handler = frame.connect("after-paint", lambda *_: seen.append(True))
    try:
        window.queue_draw()
        until(lambda: seen)
    finally:
        frame.disconnect(handler)


def settled_frame(window, page):
    # Closing a sheet can queue a default-idle style/header refresh after its
    # unmap. Drain those tracked sources, then include their resulting paint.
    while True:
        idle = []
        GLib.idle_add(lambda idle=idle: idle.append(True) or False)
        until(lambda idle=idle: idle and not page._style_source and not page._header_source)
        painted(window)
        if not page._style_source and not page._header_source:
            return


def signaled(event):
    event.set()
    GLib.idle_add(lambda: False)  # Wake an event waiter on the GTK context.


def worker_only(call):
    def guarded(*args, **kwargs):
        assert threading.get_ident() != MAIN, f"Blocking operation on GTK thread: {call}"
        return call(*args, **kwargs)

    return guarded


def file_io_guard(call):
    def guarded(*args, **kwargs):
        if threading.get_ident() == MAIN:
            frame = sys._getframe(1)
            while frame:
                module = frame.f_globals.get("__name__", "")
                if module.startswith("cassini_talk."):
                    assert module not in {
                        "cassini_talk.companion",
                        "cassini_talk.companion_setup",
                        "cassini_talk.browser_watch",
                        "cassini_talk.capture_activity",
                        "cassini_talk.browser_connection",
                        "cassini_talk.settings_dialog",
                    }, f"GTK-thread file I/O in {module}: {call}"
                    break
                frame = frame.f_back
        return call(*args, **kwargs)

    return guarded


def socket_io_guard(call):
    def guarded(connection, *args, **kwargs):
        assert threading.get_ident() != MAIN or connection.gettimeout() == 0, "GTK-thread blocking socket I/O"
        return call(connection, *args, **kwargs)

    return guarded


def completed_result(future, *args, **kwargs):
    assert threading.get_ident() != MAIN or future.done(), "GTK waited for an unfinished Future"
    return original_result(future, *args, **kwargs)


# Preserve the original store-build latency gate. Default local installation
# has its own actual Chromium/guide gate, without weakening these bounds.
companion_bundle.store_listing_public = lambda: True

original_result = Future.result
with tempfile.TemporaryDirectory() as temporary, ExitStack() as guards:
    tmp = Path(temporary)
    ready = tmp / "atspi-ready"
    ready_watch = Gio.File.new_for_path(str(ready)).monitor_file(Gio.FileMonitorFlags.NONE, None)
    ready_watch.connect("changed", lambda *_: None)
    atspi = subprocess.Popen([sys.executable, __file__, "--listener", str(ready)])
    try:
        until(ready.exists)
        ready_watch.cancel()
        assert atspi.poll() is None, "AT-SPI listener/registry must remain live"
        RUN([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
        schemas = tmp / "schemas"
        schemas.mkdir()
        shutil.copyfile(
            ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
        )
        RUN(["glib-compile-schemas", str(schemas)], check=True)
        tools = tmp / "bin"
        tools.mkdir()
        chrome = tools / "google-chrome"
        chrome.write_text("#!/bin/sh\nexit 0\n")
        chrome.chmod(0o700)
        for profile in ("Default", "Profile 1"):
            (Path(os.environ["XDG_CONFIG_HOME"]) / "google-chrome" / profile).mkdir(parents=True)
        os.environ.update(
            PATH=str(tools) + ":" + os.environ["PATH"],
            GSETTINGS_SCHEMA_DIR=str(schemas),
            CASSINI_LIBRARY_DIR=str(tmp / "fixtures/main/root"),
            CASSINI_ISOLATED_AUDIO="1",
            CASSINI_SCREENSHOT_NOW="1793107080000",
        )
        os.environ.pop("CASSINI_SCREENSHOT_ACTIVITY", None)
        services = DesktopServices()
        service = browser_service.BrowserService(tmp / "browser.json")
        server_ready = threading.Event()
        server = threading.Thread(
            target=service.serve, args=(browser_service.service_path(), server_ready), daemon=True
        )
        server.start()
        server_ready.wait()
        # Every production path into blocking browser work is guarded. Watch,
        # Activity and Settings remain real; only opening an external browser
        # is recorded instead of spawning one.
        for owner, name in (
            (companion, "installed_browsers"),
            (companion, "register"),
            (companion_bundle, "prepare"),
            (BrowserWatch, "_read"),
            (CompanionSetup, "_snapshot"),
            (CompanionSetup, "_launch_add"),
            (SettingsDialog, "_call"),
            (library, "reading_turns"),
            (meeting_text, "reading_turns"),
            (meeting_text, "reader_turns"),
        ):
            guards.enter_context(patch.object(owner, name, worker_only(getattr(owner, name))))
        guards.enter_context(
            patch("cassini_talk.capture_activity.read_browser_activity", worker_only(read_browser_activity))
        )
        for name in ("Popen", "run"):
            guards.enter_context(patch.object(subprocess, name, worker_only(getattr(subprocess, name))))
        guards.enter_context(patch.object(time, "sleep", worker_only(time.sleep)))
        guards.enter_context(patch.object(threading.Thread, "join", worker_only(threading.Thread.join)))
        guards.enter_context(patch.object(Future, "result", completed_result))
        for owner, name in ((builtins, "open"), (io, "open"), (Path, "stat"), (Path, "iterdir"), (os, "scandir")):
            guards.enter_context(patch.object(owner, name, file_io_guard(getattr(owner, name))))
        for name in ("recv", "recv_into", "send", "sendall"):
            guards.enter_context(patch.object(socket.socket, name, socket_io_guard(getattr(socket.socket, name))))
        original_connect = socket.socket.connect
        guards.enter_context(patch.object(socket.socket, "connect", worker_only(original_connect)))
        launches, launch_entered, launch_release = [], threading.Event(), threading.Event()

        def open_add(browser):
            assert threading.get_ident() != MAIN
            signaled(launch_entered)
            launch_release.wait()
            launches.append(browser.name)
            return subprocess.Popen(["/bin/true"])

        guards.enter_context(patch.object(companion, "open_add", open_add))
        app = CassiniApplication("com.gocassini.CompanionClickTest", "test", "development")
        # Enable the real startup path; the private desktop services and broker
        # already exist, and no host socket/display/browser is reachable.
        os.environ.pop("CASSINI_SCREENSHOT_ISOLATED")
        app.register(None)
        app.activate()
        window = app.get_active_window()
        until(
            lambda: (
                not window._pending and app.browser_watch and app.browser_connection.snapshot()["phase"] == "waiting"
            )
        )
        assert isinstance(window._activity_source, BrowserActivity)
        assert app.browser_watch.lease is not None
        assert (
            len(list((Path(os.environ["XDG_CONFIG_HOME"]) / "google-chrome/NativeMessagingHosts").glob("*.json"))) == 2
        )
        meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
        words = tuple(Word(meeting.speakers[0].id, i * 100, i * 100 + 90, f"word{i}") for i in range(3600))
        window.monitor.callback = lambda _snapshot: None
        meeting = replace(meeting, words=words, turns=reading_turns(words))
        window.snapshot = window._source_snapshot = replace(
            window.snapshot, meetings=tuple(meeting if m.key == meeting.key else m for m in window.snapshot.meetings)
        )
        window.open_meeting(meeting)
        page = window.meeting_page
        until(lambda: bool(page.bound))
        assert len(page.meeting.words) > 3000 and page._companion.get_parent() is not None
        # Let the initial 3600-link transcript layout and accessibility cache
        # finish. The first dialog itself is deliberately never pre-created.
        settled_frame(window, page)
        for state in ("waiting", "failed", "needsBrowser"):
            if state != "waiting":
                app.browser_connection.error = companion.SetupError("unwritable" if state == "failed" else "noBrowser")
                app._browser_connection_changed()
            if state == "needsBrowser":
                app.browser_connection.browsers = ()
            assert app.browser_connection.snapshot()["phase"] == state
            for entry in ("meeting", "preferences", "library"):
                if entry == "preferences":
                    settings = app.settings_dialog = SettingsDialog(app)
                    settings.present(window)
                    settings.set_visible_page_name("browserCompanion")
                    until(settings.get_mapped)
                    button = settings.browser_add
                elif entry == "library":
                    # This browser has connected before, but is currently away.
                    app.browser_connection.ever_connected = True
                    window.browser_status.render()
                    button = window.browser_status.add
                else:
                    button = page.companion_setup_button
                meter = MainThreadWork()
                count = len(launches)
                meter.run(button.emit, "clicked")
                dialog = app.companion_setup
                until(dialog.get_mapped)
                assert window.get_visible_dialog() is dialog
                assert len(page.meeting.words) > 3000
                assert len(launches) == count and not launch_entered.is_set(), "Entry point launched the browser"
                settled_frame(window, page)
                if state == "waiting" and entry == "meeting":
                    # Re-presenting an already mounted sheet keeps the same
                    # instance and focus. A closed sheet is never reused.
                    assert meter.run(lambda: app.on_companion_setup(parent=window)) is dialog
                    assert dialog.get_mapped()
                    meter.run(dialog.add_button.emit, "clicked")
                    until(launch_entered.is_set)
                    assert "Opening Chrome" in dialog.add_caption.get_label()
                    launch_release.set()
                    until(lambda dialog=dialog: launches == ["Chrome"] and dialog.future is None)
                    launch_entered.clear()
                # An unchanged render preserves the existing signature/widgets.
                with (
                    patch.object(
                        dialog.add_caption, "set_label", side_effect=AssertionError("Unchanged render updated a label")
                    ),
                    patch.object(
                        dialog, "set_default_widget", side_effect=AssertionError("Unchanged render reset focus")
                    ),
                ):

                    def unchanged_batch(dialog=dialog):
                        for _ in range(10):
                            dialog.render()

                    meter.run(unchanged_batch)
                meter.run(dialog.force_close)
                # closed starts the closing animation. Keep its frame work in
                # the CPU samples, and wait for unmap before closing the
                # parent or clicking an entry behind the modal sheet.
                until(lambda dialog=dialog: dialog.closed and not dialog.get_mapped())
                if entry == "preferences":
                    meter.run(settings.force_close)
                    until(lambda settings=settings: settings.closed and not settings.get_mapped())
                settled_frame(window, page)
                meter.check(state, entry)
                meter = None
            if state == "failed":
                dialog = app.on_companion_setup(parent=window)
                until(dialog.get_mapped)
                entered, release = threading.Event(), threading.Event()
                original_attempt = app.browser_connection.attempt

                def delayed_attempt(
                    entered=entered, release=release, original_attempt=original_attempt, *, force=False
                ):
                    signaled(entered)
                    release.wait()
                    return original_attempt(force=force)

                app.browser_connection.attempt = delayed_attempt
                meter = MainThreadWork()
                meter.run(dialog.connect_button.emit, "clicked")
                assert app.browser_connection.snapshot()["phase"] == "settingUp"
                until(entered.is_set)
                release.set()
                until(lambda: app.browser_connection.snapshot()["phase"] == "waiting")
                settled_frame(window, page)
                meter.check("failed", "retry")
                meter = None
                app.browser_connection.attempt = original_attempt
                dialog.force_close()
                until(lambda dialog=dialog: dialog.closed)
        orphan = app.companion_setup = CompanionSetup(app)
        assert orphan.get_root() is None and not orphan.closed
        replacement = app.on_companion_setup(parent=window)
        until(replacement.get_mapped)
        assert replacement is not orphan and orphan.closed and not orphan.timer
        replacement.force_close()
        until(lambda: replacement.closed)
        other = Adw.ApplicationWindow(application=app)
        other.present()
        until(other.get_mapped)
        previous = app.on_companion_setup(parent=other)
        until(previous.get_mapped)
        replacement = app.on_companion_setup(parent=window)
        until(replacement.get_mapped)
        assert replacement is not previous and previous.closed and not previous.timer
        assert replacement.get_root() is window and window.get_visible_dialog() is replacement
        replacement.force_close()
        until(lambda: replacement.closed)
        other.close()
        adjustment = page.scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        until(lambda: adjustment.get_value() > 0)
        adjustment.set_value(0)
        until(lambda: adjustment.get_value() == 0)
        assert page._companion.get_visible() and page._companion.is_ancestor(page)
        assert atspi.poll() is None
        app.on_quit()
        app.do_shutdown()
        guards.close()
        service.closed.set()
        server.join()
        assert not server.is_alive()
        print(
            "PASS setup click gate: live private broker/watch/activity, AT-SPI listener, no GTK blocking calls",
            flush=True,
        )
    finally:
        ready_watch.cancel()
        guards.close()
        atspi.terminate()
        atspi.wait()
