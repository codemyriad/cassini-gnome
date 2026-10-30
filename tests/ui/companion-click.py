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
Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from desktop_services import DesktopServices  # noqa: E402

from cassini_viewer import browser_service, companion, companion_bundle  # noqa: E402
from cassini_viewer.browser_watch import BrowserWatch  # noqa: E402
from cassini_viewer.capture_activity import BrowserActivity, read_browser_activity  # noqa: E402
from cassini_viewer.companion_setup import CompanionSetup  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.meeting_text import reading_turns  # noqa: E402
from cassini_viewer.portable import Word  # noqa: E402
from cassini_viewer.settings_dialog import SettingsDialog  # noqa: E402

MAIN = threading.get_ident()
SLEEP = time.sleep
RUN = subprocess.run
context = GLib.MainContext.default()


def until(check, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for _ in range(32):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        SLEEP(0.001)
    assert check(), "Companion click condition timed out"


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
                if module.startswith("cassini_viewer."):
                    assert module not in {
                        "cassini_viewer.companion",
                        "cassini_viewer.companion_setup",
                        "cassini_viewer.browser_watch",
                        "cassini_viewer.capture_activity",
                        "cassini_viewer.browser_connection",
                        "cassini_viewer.settings_dialog",
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
    atspi = subprocess.Popen([sys.executable, __file__, "--listener", str(ready)])
    try:
        until(ready.exists, 5)
        assert atspi.poll() is None, "AT-SPI listener/registry must remain live"
        RUN([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(tmp / "fixtures")], check=True)
        schemas = tmp / "schemas"
        schemas.mkdir()
        shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
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
        assert server_ready.wait(2)
        # Every production path into blocking browser work is guarded. Watch,
        # Activity and Settings remain real; only opening an external browser
        # is recorded instead of spawning one.
        for owner, name in (
            (companion, "installed_browsers"),
            (companion, "register"),
            (BrowserWatch, "_read"),
            (CompanionSetup, "_snapshot"),
            (CompanionSetup, "_launch_add"),
            (SettingsDialog, "_call"),
        ):
            guards.enter_context(patch.object(owner, name, worker_only(getattr(owner, name))))
        guards.enter_context(
            patch("cassini_viewer.capture_activity.read_browser_activity", worker_only(read_browser_activity))
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
            launch_entered.set()
            assert launch_release.wait(2)
            launches.append(browser.name)

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
            ),
            5,
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
        settled = time.monotonic()
        until(lambda: time.monotonic() - settled >= 2, 3)
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
                gaps, ticks = [], [time.monotonic()]

                def beat(gaps=gaps, ticks=ticks):
                    now = time.monotonic()
                    gaps.append(now - ticks[0])
                    ticks[0] = now
                    return True

                timer = GLib.timeout_add(10, beat)
                count, started = len(launches), time.monotonic()
                button.emit("clicked")
                dialog = app.companion_setup
                until(dialog.get_mapped, 0.5)
                assert window.get_visible_dialog() is dialog
                assert len(page.meeting.words) > 3000
                assert len(launches) == count and not launch_entered.is_set(), "Entry point launched the browser"
                until(lambda started=started: time.monotonic() - started >= 2)
                GLib.source_remove(timer)
                assert gaps and max(gaps) < 0.1, (state, entry, max(gaps))
                print(f"PASS {state}/{entry}: mapped, max heartbeat gap {max(gaps) * 1000:.1f} ms", flush=True)
                if state == "waiting" and entry == "meeting":
                    # Re-presenting an already mounted sheet keeps the same
                    # instance and focus. A closed sheet is never reused.
                    assert app.on_companion_setup(parent=window) is dialog
                    assert dialog.get_mapped()
                    dialog.add_button.emit("clicked")
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
                    for _ in range(10):
                        dialog.render()
                dialog.force_close()
                until(lambda dialog=dialog: dialog.closed)
                if entry == "preferences":
                    settings.force_close()
                    until(lambda settings=settings: settings.closed)
            if state == "failed":
                dialog = app.on_companion_setup(parent=window)
                until(dialog.get_mapped, 0.5)
                entered, release = threading.Event(), threading.Event()
                original_attempt = app.browser_connection.attempt

                def delayed_attempt(entered=entered, release=release, original_attempt=original_attempt):
                    entered.set()
                    assert release.wait(2)
                    return original_attempt()

                app.browser_connection.attempt = delayed_attempt
                before = time.monotonic()
                retry_ticks, retry_gaps = [before], []

                def retry_beat(retry_gaps=retry_gaps, retry_ticks=retry_ticks):
                    now = time.monotonic()
                    retry_gaps.append(now - retry_ticks[0])
                    retry_ticks[0] = now
                    return True

                timer = GLib.timeout_add(10, retry_beat)
                dialog.connect_button.emit("clicked")
                assert time.monotonic() - before < 0.1
                assert app.browser_connection.snapshot()["phase"] == "settingUp"
                until(entered.is_set)
                release.set()
                until(lambda: app.browser_connection.snapshot()["phase"] == "waiting")
                until(lambda before=before: time.monotonic() - before >= 2, 3)
                GLib.source_remove(timer)
                assert retry_gaps and max(retry_gaps) < 0.1, ("retry", max(retry_gaps))
                app.browser_connection.attempt = original_attempt
                dialog.force_close()
                until(lambda dialog=dialog: dialog.closed)
        orphan = app.companion_setup = CompanionSetup(app)
        assert orphan.get_root() is None and not orphan.closed
        replacement = app.on_companion_setup(parent=window)
        until(replacement.get_mapped, 0.5)
        assert replacement is not orphan and orphan.closed and not orphan.timer
        replacement.force_close()
        until(lambda: replacement.closed)
        other = Adw.ApplicationWindow(application=app)
        other.present()
        until(other.get_mapped)
        previous = app.on_companion_setup(parent=other)
        until(previous.get_mapped, 0.5)
        replacement = app.on_companion_setup(parent=window)
        until(replacement.get_mapped, 0.5)
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
        server.join(2)
        assert not server.is_alive()
        print(
            "PASS setup click gate: live private broker/watch/activity, AT-SPI listener, no GTK blocking calls",
            flush=True,
        )
    finally:
        guards.close()
        atspi.terminate()
        atspi.wait(5)
