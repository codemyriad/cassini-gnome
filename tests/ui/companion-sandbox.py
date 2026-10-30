#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""No unusable companion actions in Flatpak; Record keeps the native sound path."""

import os
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
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-viewer.gresource")))
from cassini_viewer.browser_connection import BrowserConnection  # noqa: E402
from cassini_viewer.library_window import LibraryWindow  # noqa: E402
from cassini_viewer.main import CassiniApplication  # noqa: E402
from cassini_viewer.settings_dialog import SettingsDialog  # noqa: E402


def until(check):
    deadline = time.monotonic() + 10
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    assert check(), "Sandbox companion gate did not settle"


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(ROOT / "data/com.gocassini.Viewer.gschema.xml", schemas / "com.gocassini.Viewer.gschema.xml")
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    fixtures = tmp / "fixtures"
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(fixtures)], check=True)
    os.environ.update(
        FLATPAK_ID="com.gocassini.Viewer",
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(fixtures / "empty/root"),
    )
    tick_status = []
    original_tick = LibraryWindow._activity_tick

    def activity_tick(window):
        result = original_tick(window)
        if window.recording_page:
            page = window.recording_page
            tick_status.append(
                (page.timer.get_label(), page.mic_level.get_fraction(), page.others_level.get_fraction())
            )
        return result

    with (
        patch(
            "cassini_viewer.browser_service.ensure_service",
            side_effect=AssertionError("Sandbox tried unusable browser bridge"),
        ),
        patch.object(LibraryWindow, "_activity_tick", activity_tick),
        patch("cassini_viewer.companion_bundle.store_listing_public", return_value=True),
    ):
        app = CassiniApplication("com.gocassini.CompanionSandboxTest", "test", "development")
        app.register(None)
        app.activate()
        window = app.get_active_window()
        until(lambda: not window._pending)
        app.browser_connection = BrowserConnection(app._browser_connection_changed, available=False)
        app.retry_browser_connection()
        assert not app.browser_companion_available and app.browser_watch is None
        until(lambda: app.browser_connection.future is None)
        assert not app.browser_connection.timer, "Missing sandbox permission cannot recover through polling"
        activity_timer = window._activity_timer
        assert activity_timer, "Native activity timer must run without a browser bridge"
        assert not window.first_companion.get_visible()
        assert app.on_companion_setup() is None
        dialog = SettingsDialog(app)
        dialog.present(window)
        assert dialog.browser.get_title() == "Not set up"
        assert not dialog.browser_setup.get_visible() and not dialog.automatic.get_visible()
        assert not dialog.browser_add.get_visible() and not dialog.browser_add_local.get_visible()
        assert dialog._future is None
        assert not dialog.move.get_visible(), "Unverified document-portal relocation must stay hidden"
        dialog.force_close()
        until(lambda: dialog.closed)
        # Use the real native recorder with synthetic sources on this private
        # display; its Pulse endpoint deliberately does not exist.
        original = app.start_native
        app.recording_settings.set_boolean("skip-record-first", True)
        with patch.object(app, "start_native", lambda: original(test=True)):
            app.on_record()
        until(
            lambda: (
                app.recorder
                and app.recorder.session
                and all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
            )
        )
        native = app.recorder
        assert {t["kind"] for t in native.session.manifest["tracks"]} == {"microphone", "system"}
        until(lambda: len({row[0] for row in tick_status}) >= 2)
        assert any(mic > 0 and others > 0 for _, mic, others in tick_status), tick_status
        assert window._activity_source is None and window._activity_timer == activity_timer
        app.stop_native()
        until(lambda: native.pipeline is None and not native.stopping)
        assert native.session.manifest["state"] == "completed"
        window.back_to_library()
        os.environ["CASSINI_LIBRARY_DIR"] = str(fixtures / "main/root")
        window.monitor.refresh_now()
        until(lambda: not window._pending and any(m.title == "Budget review" for m in window.snapshot.meetings))
        meeting = next(m for m in window.snapshot.meetings if m.title == "Budget review")
        window.open_meeting(meeting)
        assert not window.meeting_page._companion.get_visible()
        window.meeting_page.set_connected(False)
        assert not window.meeting_page._companion.get_visible()
        # An external host probe can be slow and fail transiently. It runs
        # off GTK; controls stay hidden until a later successful retry.
        from cassini_viewer import flatpak_bridge as bridge

        entered, release = threading.Event(), threading.Event()
        threads, beats = [], []
        main_thread = threading.get_ident()

        def host_info():
            threads.append(threading.get_ident())
            assert threads[-1] != main_thread
            if len(threads) == 1:
                entered.set()
                assert release.wait(5)
                raise OSError("Transient host activation timeout")
            return (tmp / "host-config", tmp / "host-data", "/usr/bin/flatpak", {})

        settings = app.settings_dialog = SettingsDialog(app)
        settings.present(window)
        timer = GLib.timeout_add(10, lambda: (beats.append(1), GLib.SOURCE_CONTINUE)[1])
        try:
            with (
                patch.object(
                    bridge, "deployment", return_value=bridge.Deployment("com.gocassini.Viewer", "x86_64", "master")
                ),
                patch.object(bridge, "host_info", side_effect=host_info),
                # The new bounded desktop inventory runs against this private
                # home, never flatpak-spawn or the user's host desktop.
                patch.object(bridge, "host_command", side_effect=lambda command, **_: command),
                patch.object(SettingsDialog, "_call", return_value={}),
            ):
                app.retry_browser_connection()
                until(lambda: entered.is_set() and len(beats) >= 8)
                assert app.browser_connection.future and not app.browser_connection.future.done()
                recording_worker = app._record_start_executor.submit(threading.get_ident).result(timeout=1)
                assert recording_worker != threads[0], "Blocked host probe occupied the recording executor"
                assert not app.browser_companion_available
                assert not settings.browser_setup.get_visible() and not settings.automatic.get_visible()
                assert not settings.browser_add.get_visible() and not settings.browser_add_local.get_visible()
                assert not window.first_companion.get_visible()
                release.set()
                until(lambda: app.browser_connection.future is None)
                assert not app.browser_companion_available and app.browser_connection.timer
                until(lambda: app.browser_companion_available)  # Actual scheduled retry.
                assert len(threads) >= 3 and all(t != main_thread for t in threads)
                assert not window.first_companion.get_visible(), "Setup belongs in Settings before the first connection"
                assert settings.automatic.get_visible()
                assert not settings.browser_setup.get_visible()
                assert settings.browser.get_title() == "Browser meetings need Chrome, Edge, Brave or Chromium."
                assert settings.browser_add.get_visible() and not settings.browser_add.get_sensitive()
                assert window.meeting_page._companion.get_visible()
                assert window._activity_timer == activity_timer, "Bridge changes must not duplicate the live timer"
        finally:
            release.set()
            GLib.source_remove(timer)
            settings.force_close()
            until(lambda: settings.closed)
        window.close()
        app.do_shutdown()
    print(
        "PASS Flatpak gate, native recording clock/meters without bridge, responsive host probe and retry enabling controls"
    )
