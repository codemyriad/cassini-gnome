#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find and activate production custom-child buttons through a separate AT-SPI client."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".json")


def child():
    import shutil
    from dataclasses import replace

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gio, GLib, Gst

    sys.path.insert(0, str(ROOT / "src"))
    Gio.resources_register(
        Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource"))
    )
    from cassini_talk.folder_player import FolderPlayer
    from cassini_talk.library_window import LibraryWindow
    from cassini_talk.main import CassiniApplication
    from cassini_talk.player import Player

    def until(check):
        deadline = time.monotonic() + 55
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            for _ in range(64):
                if not context.pending():
                    break
                context.iteration(False)
            if check():
                return
            time.sleep(0.01)
        raise AssertionError("AT-SPI activation did not reach the production callback")

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
            CASSINI_SCREENSHOT_NOW="1793107080000",
        )
        os.environ.pop("CASSINI_SCREENSHOT_ACTIVITY", None)
        original_connect = LibraryWindow._connect_player

        def connect(window):
            sink = Gst.ElementFactory.make("fakesink", None)
            sink.set_property("sync", True)
            if isinstance(window.player, FolderPlayer):
                window.player.audio_sink = sink
            elif isinstance(window.player, Player):
                window.player._pipeline.set_property("audio-sink", sink)
            original_connect(window)

        LibraryWindow._connect_player = connect
        GLib.set_prgname("cassini-accessible-buttons-test")
        app = CassiniApplication("com.gocassini.AccessibleButtonsTest", "test", "development")
        app.transcription_enabled = False
        app.register(None)
        app.activate()
        window = app.get_active_window()
        until(lambda: not window._pending)
        window.monitor.callback = lambda _snapshot: None
        events = []
        saved_snapshot = window.snapshot

        def record_event(event):
            events.append(event)
            temporary = OUTPUT.with_suffix(".tmp")
            temporary.write_text(json.dumps(dict(events=events)))
            temporary.replace(OUTPUT)

        live = replace(
            next(m for m in window.snapshot.meetings if m.title.startswith("Weekly sync")), lifecycle="recording"
        )
        meeting = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")

        def start():
            window.snapshot = replace(
                saved_snapshot, meetings=tuple(live if m.key == live.key else m for m in saved_snapshot.meetings)
            )
            window._source_snapshot = window.snapshot
            window.open_meeting(live)
            record_event("start")

        def stop(_meeting):
            window.snapshot = window._source_snapshot = saved_snapshot
            record_event("stop")
            window.back_to_library()
            window.open_meeting(meeting)
            window.meeting_page.name_speakers = name_speaker
            window.meeting_page.rate_button.get_popover().connect("map", lambda *_: record_event("speed-menu"))

            def changed(button, *_):
                if button.get_label() == "1.5×":
                    assert window.player.rate == 1.5
                    assert window.lookup_action("rate").get_state().get_string() == "1.5"
                    record_event("speed-changed")

            window.meeting_page.rate_button.connect("notify::label", changed)

        def name_speaker(_identifier):
            record_event("speaker")
            window.back_to_library()
            window.activate_action("win.search", None)
            window.search_page.entry.set_text("budget")

        # The accessibility client drives the real widgets/actions. Capture
        # and name-writing callbacks are observed without opening any devices.
        app.start_recordings = start
        window.stop_meeting = stop
        app.recording_settings.set_boolean("skip-record-first", False)
        record_event("ready")
        until(lambda: events[-1] == "speaker" and window.player.playing and window.player.position >= 4000)
        record_event("search-play")
        window.player.pause()
        app.on_quit()
        app.do_shutdown()


def parent():
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    Atspi.set_timeout(1000, 1000)

    def drain():
        context = GLib.MainContext.default()
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)

    def buttons():
        def walk(node):
            if node.get_role() in (
                Atspi.Role.PUSH_BUTTON,
                Atspi.Role.TOGGLE_BUTTON,
                Atspi.Role.RADIO_MENU_ITEM,
                Atspi.Role.RADIO_BUTTON,
                Atspi.Role.MENU_ITEM,
            ):
                yield node
            for index in range(node.get_child_count()):
                item = node.get_child_at_index(index)
                if item and item.get_state_set().contains(Atspi.StateType.SHOWING):
                    yield from walk(item)

        desktop = Atspi.get_desktop(0)
        for index in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(index)
            if "accessible-buttons" in app.get_name().lower():
                yield from walk(app)

    def activate(name, expected_event):
        deadline = time.monotonic() + 10
        names = []
        while time.monotonic() < deadline:
            assert process.poll() is None, f"GTK child exited {process.returncode}; see {log_path}"
            try:
                drain()
                found = list(buttons())
                names = [button.get_name() for button in found]
                target = next((button for button in found if button.get_name() == name), None)
                if target and target.get_state_set().contains(Atspi.StateType.SENSITIVE):
                    action = target.get_action_iface()
                    assert action and action.get_n_actions() > 0, f"No AT-SPI action for {name}"
                    print(f"AT-SPI activating {name}", flush=True)
                    assert action.do_action(0), f"AT-SPI could not activate {name}"
                    print(f"AT-SPI dispatched {name}", flush=True)
                    break
            except GLib.Error:
                pass  # A replaced page can invalidate its old accessible objects.
            time.sleep(0.025)
        else:
            raise AssertionError(f"No named AT-SPI button {name!r}; visible buttons: {names!r}")
        if expected_event:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                drain()
                if OUTPUT.exists() and expected_event in json.loads(OUTPUT.read_text())["events"]:
                    return
                assert process.poll() is None, f"GTK child exited before {expected_event}"
                time.sleep(0.025)
            raise AssertionError(f"AT-SPI {name!r} did not invoke its callback")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.unlink(missing_ok=True)
    log_path = OUTPUT.with_suffix(".gtk.log")
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, __file__, "--child"], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while not OUTPUT.exists() and time.monotonic() < deadline:
                assert process.poll() is None, f"GTK child exited before ready; see {log_path}"
                time.sleep(0.025)
            assert OUTPUT.exists(), "GTK child did not finish loading its private fixture"
            activate("Record", None)
            activate("Start recording", "start")
            activate("Stop", "stop")
            activate("Playback speed, 1×", "speed-menu")
            activate("1.5×", "speed-changed")
            activate("Marta", "speaker")
            activate("Play from 00:00:04", "search-play")
            assert process.wait(10) == 0
            assert not any(value in log_path.read_text() for value in ("Gtk-WARNING", "Gtk-CRITICAL"))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(5)
    print("PASS real AT-SPI names/actions: Record, Start recording, Stop, speaker row, Search playback")


if "--child" in sys.argv:
    child()
else:
    parent()
