#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D60 (R83): the test recording from Settings and the first-run step, through AT-SPI.

A separate AT-SPI client finds and activates Test recording, Play back, Test
again and Done by name, and reads the result lines. This display has no Pulse:
Settings tests use the recorder's synthetic sources (440/880 Hz), and the
first-run test uses the real devices, so both streams are refused.
"""

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
ACKS = OUTPUT.with_suffix(".ack")
PRGNAME = "cassini-test-recording-test"
WORKING = ["Your microphone: working", "This computer’s sound: working"]
SILENT = ["Your microphone: working", "This computer’s sound: no sound heard"]
REFUSED = ["Your microphone: couldn’t be started.", "This computer’s sound: couldn’t be started."]


def child():
    import shutil
    import stat
    from functools import partial
    from threading import Event
    from uuid import uuid4

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gst", "1.0")
    from gi.repository import Adw, Gio, GLib, Gst, Gtk
    from painted import painted_node

    sys.path.insert(0, str(ROOT / "src"))
    Gio.resources_register(
        Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource"))
    )
    from cassini_talk import browser_service
    from cassini_talk.capture_guard import recorder_busy
    from cassini_talk.folder_player import FolderPlayer
    from cassini_talk.main import CassiniApplication
    from cassini_talk.sound_check import folders_root
    from cassini_talk.sound_check_dialog import SoundCheckDialog

    def until(check, seconds=40):
        # Blocking iterations serve the AT-SPI client's requests as they come;
        # a short heartbeat re-checks conditions no source wakes up for.
        deadline = time.monotonic() + seconds
        context = GLib.MainContext.default()
        heartbeat = GLib.timeout_add(10, lambda: True)
        try:
            while not check():
                if time.monotonic() > deadline:
                    raise AssertionError("Test recording GUI condition timed out")
                context.iteration(True)
        finally:
            GLib.source_remove(heartbeat)

    events = []

    def record_event(event):
        events.append(event)
        temporary = OUTPUT.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(events=events)))
        temporary.replace(OUTPUT)

    def acked(name):
        until(lambda: ACKS.exists() and name in json.loads(ACKS.read_text()))

    def shot(name):
        window = app.get_active_window()
        frames = []
        window.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 3)[1])
        window.queue_draw()
        until(lambda: len(frames) >= 3)
        node = painted_node(window)
        texture = window.get_renderer().render_texture(node, node.get_bounds())
        assert texture.save_to_png(str(OUTPUT.with_name(f"test-recording-{name}.png")))

    def shots(name):
        style = Adw.StyleManager.get_default()
        for scheme, suffix in ((Adw.ColorScheme.FORCE_LIGHT, "light"), (Adw.ColorScheme.FORCE_DARK, "dark")):
            style.set_color_scheme(scheme)
            shot(f"{name}-{suffix}")
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

    def private(folder):
        levels = (folder, folder.parent, folder.parent.parent)
        return all(stat.S_IMODE(level.stat().st_mode) == 0o700 for level in levels)

    def result():
        until(lambda: check.state == "result" and app.sound_check_dialog and app.sound_check_dialog.lines)
        return app.sound_check_dialog.lines

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
        Gtk.Settings.get_default().props.gtk_enable_animations = False
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        # A crash left a test folder behind: the app's start sweeps it.
        root = folders_root()
        for level in (root.parent.parent, root.parent, root):
            level.mkdir(mode=0o700, exist_ok=True)
        leftover = root / str(uuid4())
        (leftover / "Recording-x").mkdir(mode=0o700, parents=True)
        (leftover / "Recording-x/track-microphone.wav").write_bytes(b"left by a crash")

        def player(_dialog):
            sink = Gst.ElementFactory.make("fakesink", None)
            sink.set_property("sync", True)
            return FolderPlayer(audio_sink=sink)

        SoundCheckDialog._new_player = player
        GLib.set_prgname(PRGNAME)
        app = CassiniApplication("com.gocassini.TestRecordingTest", "test", "development")
        app.transcription_enabled = False
        app.register(None)
        app.activate()
        window = app.get_active_window()
        until(lambda: not window._pending and not leftover.exists())
        check = app.sound_check
        library = tmp / "fixtures/main/root"
        before = sorted(library.iterdir())
        real_test = app.start_test_recording
        real_native = app.start_native
        app.start_test_recording = partial(real_test, test=True)
        record_event("ready")

        # Settings > Recording: the row, then a test through the AT-SPI client.
        app.on_preferences()
        settings = app.settings_dialog
        assert settings.test_row.get_title() == "Test recording"
        assert settings.test_row.get_subtitle().startswith("Records 8 seconds from your microphone")
        until(lambda: settings.test_row.get_sensitive() and settings.test_button.get_sensitive())
        record_event("settings")
        until(lambda: check.state == "running" and app.sound_check_dialog is not None)
        dialog = app.sound_check_dialog
        assert dialog.status.get_label() == "Testing… Say something and play a sound."
        assert not dialog.buttons.get_visible() and not settings.test_row.get_sensitive()
        until(lambda: check.folder is not None and check.folder.exists())
        folder = check.folder
        assert folder.parent == root and private(folder)
        shots("running")
        assert result() == WORKING
        assert dialog.buttons.get_visible() and dialog.play_button.get_sensitive()
        assert dialog.note.get_label() == "Play it back to check that it sounds right."
        assert dialog.done_button.has_focus(), "Done takes the keyboard focus with the result"
        assert not app.native_captures and not app.recorder, "A test is never a capture"
        assert sorted(library.iterdir()) == before, "Nothing reaches the Library"
        shots("result")
        record_event("result")
        # Play back: both streams together through FolderPlayer, to the end.
        until(lambda: dialog.player is not None and dialog.player.playing)
        assert len(dialog.player.sources) == 2
        until(lambda: not dialog.player.playing and dialog.player.position == dialog.player.duration, 20)
        assert dialog.player.duration >= 7900
        # Test again: this computer's sound plays nothing now (no sound heard).
        parse = Gst.parse_bin_from_description

        def muted(description, ghost):
            return parse(description.replace("freq=880", "freq=880 volume=0"), ghost)

        Gst.parse_bin_from_description = muted
        record_event("played")
        until(lambda: check.state == "running" and check.folder not in (None, folder))
        assert app.sound_check_dialog is dialog and not dialog.closed and dialog.player is None
        until(lambda: not folder.exists())
        folder = check.folder
        assert result() == SILENT
        Gst.parse_bin_from_description = parse
        shots("silent")
        record_event("again")
        until(lambda: dialog.closed and not check.busy)
        assert not folder.exists() and app.sound_check_dialog is None and not settings.closed
        assert settings.test_row.get_sensitive()

        # Any recording disables the row; the AT-SPI client reads it insensitive.
        app.start_native(test=True)
        native = app.recorder
        until(lambda: native is not None and recorder_busy(native) and not settings.test_row.get_sensitive())
        assert not app.can_test_recording()
        record_event("recording")
        acked("recording")
        app.stop_native()
        until(lambda: native.pipeline is None and not native._finishing and settings.test_row.get_sensitive())

        # A real Record during a test discards it and starts as usual.
        settings.test_row.activate()
        until(lambda: check.state == "running" and check.folder is not None and check.folder.exists())
        dialog, folder = app.sound_check_dialog, check.folder
        app.recording_settings.set_boolean("skip-record-first", True)
        app.start_native = lambda: real_native(test=True)
        # Record's broker reply is asynchronous. Hold it until test cleanup
        # finishes so this always exercises a start after the old recorder was
        # retired, rather than depending on the runner's broker startup speed.
        previous_native = app.recorder
        release_reply, requested = Event(), Event()
        request = browser_service.request

        def deferred_reply(*args, **kwargs):
            reply = request(*args, **kwargs)
            if args and args[0] == "start-all":
                requested.set()
                assert release_reply.wait(40), "Test did not release the Record reply"
            return reply

        browser_service.request = deferred_reply
        try:
            app.on_record()
            until(lambda: dialog.closed and not folder.exists() and not check.busy and requested.is_set())
            assert check.state == "idle" and check.outcomes is None
            assert app.recorder is previous_native, "The held reply cannot start a replacement yet"
            release_reply.set()
            until(
                lambda: (
                    (native := app.recorder) is not None
                    and native is not previous_native
                    and recorder_busy(native)
                    and native.session is not None
                )
            )
            native = app.recorder
        finally:
            release_reply.set()
            browser_service.request = request
        assert not native.session.directory.is_relative_to(root)
        app.start_native = real_native
        app.stop_native()
        until(lambda: native.pipeline is None and not native._finishing)
        app.recording_settings.set_boolean("skip-record-first", False)
        settings.force_close()
        until(lambda: settings.closed)

        # First run: the real devices, which this display does not have.
        app.start_test_recording = real_test
        app.on_record()
        consent = app.record_first
        assert consent and consent.get_title() == "Before you record"
        until(lambda: consent.test_button.get_sensitive())
        shots("first-run")
        record_event("first-run")
        assert result() == REFUSED
        dialog, folder = app.sound_check_dialog, check.folder
        assert not dialog.play_button.get_sensitive() and not dialog.note.get_visible(), "Nothing to play back"
        shots("refused")
        record_event("refused")
        until(lambda: dialog.closed and not folder.exists() and not check.busy)
        assert app.record_first is consent
        # A test goes with the dialog it was started from.
        consent.test_button.emit("clicked")
        until(lambda: check.state != "idle" and check.folder is not None)
        dialog, folder = app.sound_check_dialog, check.folder
        consent.close()
        until(lambda: app.record_first is None and dialog.closed and not folder.exists() and not check.busy)
        assert not list(root.iterdir()), "Every test folder is deleted"
        record_event("finished")
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

    def nodes():
        # Every control here is in a dialog: the Library window is not walked
        # beyond the few levels that hold the dialogs (a full walk takes ~10 s).
        def walk(node, depth=0, inside=False):
            inside = inside or node.get_role() == Atspi.Role.DIALOG
            if inside:
                yield node
            elif depth >= 4:
                return
            for index in range(node.get_child_count()):
                item = node.get_child_at_index(index)
                if item and item.get_state_set().contains(Atspi.StateType.SHOWING):
                    yield from walk(item, depth + 1, inside)

        desktop = Atspi.get_desktop(0)
        for index in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(index)
            if app and PRGNAME in (app.get_name() or ""):
                yield from walk(app)

    def find(check, what, seconds=10):
        deadline = time.monotonic() + seconds
        seen = []
        while time.monotonic() < deadline:
            assert process.poll() is None, f"GTK child exited {process.returncode}; see {log_path}"
            try:
                drain()
                found = list(nodes())
                seen = [(n.get_role_name(), n.get_name()) for n in found if n.get_name()]
                # An accessible's truth is its child count: test for None.
                if (value := check(found)) not in (None, False):
                    return value
            except GLib.Error:
                pass  # A replaced page can invalidate its old accessible objects.
            time.sleep(0.025)
        raise AssertionError(f"No {what}; showing named nodes: {seen!r}")

    def button(name, *, sensitive=True):
        def check(found):
            # Only the named buttons' states: other nodes can be stale.
            named = [n for n in found if n.get_role() == Atspi.Role.PUSH_BUTTON and n.get_name() == name]
            return next((n for n in named if n.get_state_set().contains(Atspi.StateType.SENSITIVE) == sensitive), None)

        return find(check, f"{'sensitive' if sensitive else 'insensitive'} button {name!r}")

    def activate(name):
        action = button(name).get_action_iface()
        assert action and action.get_n_actions() > 0, f"No AT-SPI action for {name}"
        assert action.do_action(0), f"AT-SPI could not activate {name}"
        print(f"AT-SPI activated {name}", flush=True)

    def lines(expected):
        # The result lines are named rows a screen reader reads.
        find(lambda found: set(expected) <= {n.get_name() for n in found}, f"result lines {expected!r}", 40)
        print(f"AT-SPI read {expected!r}", flush=True)

    def wait(event, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            drain()
            if OUTPUT.exists() and event in json.loads(OUTPUT.read_text())["events"]:
                return
            assert process.poll() is None, f"GTK child exited before {event}; see {log_path}"
            time.sleep(0.025)
        raise AssertionError(f"The GTK child did not reach {event}")

    def ack(name):
        acks = json.loads(ACKS.read_text()) if ACKS.exists() else []
        temporary = ACKS.with_suffix(".acktmp")
        temporary.write_text(json.dumps(acks + [name]))
        temporary.replace(ACKS)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.unlink(missing_ok=True)
    ACKS.unlink(missing_ok=True)
    log_path = OUTPUT.with_suffix(".gtk.log")
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, __file__, "--child"], stdout=log, stderr=log)
        try:
            wait("settings", 30)
            activate("Test recording")
            wait("result")
            lines(WORKING)
            for name in ("Play back", "Test again", "Done"):
                button(name)
            activate("Play back")
            wait("played", 30)
            activate("Test again")
            wait("again", 40)
            lines(SILENT)
            activate("Done")
            wait("recording", 30)
            button("Test recording", sensitive=False)
            ack("recording")
            wait("first-run", 60)
            activate("Test recording")
            wait("refused", 30)
            lines(REFUSED)
            button("Play back", sensitive=False)
            activate("Done")
            wait("finished", 60)
            assert process.wait(20) == 0, f"GTK child failed; see {log_path}"
            log_text = log_path.read_text()
            assert not any(value in log_text for value in ("Gtk-WARNING", "Gtk-CRITICAL", "Adwaita-WARNING"))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(5)
    print("PASS test recording: Settings and first run, result lines, Play back, Test again, Done, Record discards")


if "--child" in sys.argv:
    child()
else:
    parent()
