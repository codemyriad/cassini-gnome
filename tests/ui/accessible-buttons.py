#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find and activate production custom-child buttons through a separate AT-SPI client."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".json")


def child():
    import contextlib
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
    from dispatch_wait import drain_app_workers
    from progress_wait import until_context_progress

    from cassini_talk.folder_player import FolderPlayer
    from cassini_talk.library_window import LibraryWindow
    from cassini_talk.main import CassiniApplication
    from cassini_talk.player import Player

    sys.path.insert(0, str(ROOT / "tests"))
    from published_listening_support import published_opus

    from cassini_talk.library import LibraryScanner
    from cassini_talk.strings import text

    events = []
    search_pages = []

    def until(check):
        until_context_progress(
            check,
            context=GLib.MainContext.default(),
            timeout_source_ms=GLib.timeout_source_new,
            source_remove=GLib.SOURCE_REMOVE,
            progress=lambda: len(events),
            diagnostics=lambda: (
                f"events[-1]={events[-1] if events else None}; "
                f"playing={window.player.playing}; position={window.player.position}"
            ),
        )

    with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as cleanup:
        app = window = None

        def teardown():
            if window is not None and window.get_visible():
                window.close()
            if app is not None and not getattr(app, "_exit_begun", False):
                app.do_shutdown()
            drain_app_workers(app, windows=(window,), search_pages=search_pages)

        cleanup.callback(teardown)
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
        saved_snapshot = window.snapshot

        def record_event(event):
            events.append(event)
            temporary = OUTPUT.with_suffix(".tmp")
            temporary.write_text(json.dumps(dict(events=events)))
            temporary.replace(OUTPUT)
            os.write(int(os.environ["CASSINI_TEST_EVENTS_FD"]), b"event\n")

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
            page = window.search_page
            search_pages.append(page)

            def search_updated(page):
                if page.entry.get_text() == "budget" and page.hits:
                    record_event("search-ready")

            page.connect("results-updated", search_updated)
            page.entry.set_text("budget")

        def command(_source, _condition):
            operation = sys.stdin.readline().strip()
            assert operation in ("refresh", "arm")
            page = window.search_page
            if operation == "arm":
                from concurrent.futures import Future

                def during_activation(*_):
                    future = Future()
                    future.set_result((page.hits, page.names, page._meetings, page._turns))
                    page._complete(future, page._generation)
                    record_event("activation-refreshed")

                page.play.connect_after("activate", during_activation)
                record_event("refresh-armed")
                return GLib.SOURCE_CONTINUE
            old_play = page.play
            # Wall time changes between real queries without changing the
            # displayed date. It must not invalidate the context's identity.
            window._fixed_now += 1

            def refreshed(page):
                page.disconnect(handler)
                assert page.play is old_play, "An unchanged search replaced its accessible controls"
                record_event("search-refreshed")

            handler = page.connect("results-updated", refreshed)
            page.request()
            return GLib.SOURCE_CONTINUE

        GLib.io_add_watch(sys.stdin, GLib.IO_IN, command)

        # The accessibility client drives the real widgets/actions. Capture
        # and name-writing callbacks are observed without opening any devices.
        app.start_recordings = start
        window.stop_meeting = stop
        app.recording_settings.set_boolean("skip-record-first", False)
        record_event("ready")
        until(lambda: "speaker" in events and window.player.playing and window.player.position >= 4000)
        record_event("search-play")
        window.player.pause()
        published_root = tmp / "Published"
        published_opus(published_root)
        (published,) = LibraryScanner(published_root).scan().meetings
        assert published.published_file and published.listening_turns
        window.open_meeting(replace(published, metadata_editable=False), external=True)
        page = window.meeting_page
        rebuilt = []
        page.connect("rows-rebuilt", lambda *_: rebuilt.append(True))
        until(lambda: rebuilt and window.player._prerolled)
        for identifier, position in (("silvio", "00:00:02"), ("guest", "00:00:09")):
            _row, play, caption = page._speaker_rows[identifier]
            assert caption == text("speakers.when.once", position=position)
            assert play.get_visible() and play.get_mapped() and play.get_sensitive()
        record_event("published-ready")
        until(lambda: window.listen.active and window.listen.speaker == "guest")
        record_event("published-play")
        window.player.pause()
        app.on_quit()
        app.do_shutdown()
        teardown()


def parent():
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    sys.path.insert(0, str(ROOT / "src"))
    from cassini_talk.strings import plural, text

    Atspi.set_timeout(1000, 1000)

    def drain():
        while context.pending():
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

    def wait_event(expected_event):
        while True:
            drain()
            if OUTPUT.exists() and expected_event in json.loads(OUTPUT.read_text())["events"]:
                return
            assert not pipe_closed and process.poll() is None, (
                f"GTK child exited before {expected_event}; see {log_path}"
            )
            if context.pending():
                continue
            context.iteration(True)

    def find_button(name):
        nonlocal atspi_dirty
        print(f"AT-SPI waiting for {name}", flush=True)
        while True:
            drain()
            assert not pipe_closed and process.poll() is None, (
                f"GTK child exited before button {name!r}; see {log_path}"
            )
            atspi_dirty = False
            try:
                target = next(
                    (
                        button
                        for button in buttons()
                        if button.get_name() == name and button.get_state_set().contains(Atspi.StateType.SENSITIVE)
                    ),
                    None,
                )
                if target:
                    action = target.get_action_iface()
                    assert action and action.get_n_actions() > 0, f"No AT-SPI action for {name}"
                    return target, action
            except GLib.Error:
                pass  # A replaced page can invalidate its old accessible objects.
            if atspi_dirty or context.pending():
                continue
            # Accessibility publication and child pipe events wake this context.
            # The screenshot process watchdog is the only elapsed-time bound.
            context.iteration(True)

    def activate(name, expected_event, *, refresh_after_lookup=False):
        target, action = find_button(name)
        if refresh_after_lookup:
            process.stdin.write("refresh\n")
            process.stdin.flush()
            wait_event("search-refreshed")
            # Query the tree again only after the application has applied results.
            target, action = find_button(name)
            process.stdin.write("arm\n")
            process.stdin.flush()
            wait_event("refresh-armed")
        print(f"AT-SPI activating {name}", flush=True)
        assert action.do_action(0), f"AT-SPI could not activate {name}"
        print(f"AT-SPI dispatched {name}", flush=True)
        if expected_event:
            try:
                wait_event(expected_event)
            except AssertionError as error:
                raise AssertionError(f"AT-SPI {name!r} did not invoke its callback") from error

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.unlink(missing_ok=True)
    log_path = OUTPUT.with_suffix(".gtk.log")
    with log_path.open("w") as log:
        event_fd, child_event_fd = os.pipe()
        context = GLib.MainContext.default()
        pipe_closed = False
        atspi_dirty = False

        def child_event(_fd, _condition):
            nonlocal pipe_closed
            if os.read(event_fd, 4096):
                return GLib.SOURCE_CONTINUE
            pipe_closed = True
            return GLib.SOURCE_REMOVE

        def mark_atspi_dirty(*_):
            nonlocal atspi_dirty
            atspi_dirty = True

        event_watch = GLib.io_add_watch(event_fd, GLib.IO_IN | GLib.IO_HUP, child_event)
        listener = Atspi.EventListener.new(mark_atspi_dirty)
        event_types = ("object:children-changed", "object:state-changed", "object:property-change", "window")
        for event_type in event_types:
            assert listener.register(event_type), event_type
        process = subprocess.Popen(
            [sys.executable, __file__, "--child"],
            stdin=subprocess.PIPE,
            text=True,
            stdout=log,
            stderr=log,
            pass_fds=(child_event_fd,),
            env=dict(os.environ, CASSINI_TEST_EVENTS_FD=str(child_event_fd)),
        )
        os.close(child_event_fd)
        try:
            wait_event("ready")
            activate("Record", None)
            activate("Start recording", "start")
            activate("Stop", "stop")
            activate("Playback speed, 1×", "speed-menu")
            activate("1.5×", "speed-changed")
            activate(
                text(
                    "speakers.row.a11y",
                    name="Marta",
                    provenance=text("meeting.speakerCaption.namedByYou"),
                    when=plural("speakers.when.times", 2, count=2, position="00:02:41"),
                ),
                "speaker",
            )
            wait_event("search-ready")
            activate("Play from 00:00:04", "search-play", refresh_after_lookup=True)
            wait_event("activation-refreshed")
            wait_event("published-ready")
            # Recognised-but-unmatched has no provenance: exact row naming
            # omits the empty item, and activates speaker listening on a read-only file.
            when = text("speakers.when.once", position="00:00:09")
            activate("Dario, " + when, "published-play")
            assert process.wait() == 0
            assert not any(value in log_path.read_text() for value in ("Gtk-WARNING", "Gtk-CRITICAL"))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(5)
            process.stdin.close()
            if not pipe_closed:
                GLib.source_remove(event_watch)
            for event_type in event_types:
                listener.deregister(event_type)
            os.close(event_fd)
    print("PASS real AT-SPI names/actions: Record, Start recording, Stop, speaker row, Search playback")


if "--child" in sys.argv:
    child()
else:
    parent()
