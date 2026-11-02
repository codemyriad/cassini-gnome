#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""UX11 real radio controls, native source changes, failure/retry and source cards."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import Future
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
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.session_events import SourceState  # noqa: E402
from cassini_talk.strings import text  # noqa: E402
from cassini_talk.what_is_recorded import explain_fallback  # noqa: E402


def until(check):
    end = time.monotonic() + 10
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        if check():
            return
        time.sleep(0.005)
    assert check(), "What is recorded GUI did not settle"


def shot(window, suffix):
    frames = []
    window.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 3)[1])
    window.queue_draw()
    until(lambda: len(frames) >= 2)
    paintable, snapshot = Gtk.WidgetPaintable.new(window), Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert (texture.get_width(), texture.get_height()) == (1120, 720)
    target = Path(os.environ["CASSINI_SCREENSHOT_OUT"])
    assert texture.save_to_png(str(target.with_name(target.stem + suffix + ".png")))


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    fixtures = tmp / "fixtures"
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(fixtures)], check=True)
    subprocess.run([sys.executable, str(ROOT / "tests/ui/recording-fixture.py"), str(fixtures)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(fixtures / "recording/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(fixtures / "recording/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().props.gtk_enable_animations = False
    Gtk.Settings.get_default().props.gtk_font_name = "Cantarell 11"
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    app = CassiniApplication("com.gocassini.SourcesBoardTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending)
    meeting = next(m for m in window.snapshot.meetings if m.lifecycle == "recording")
    window.open_meeting(meeting)
    page = window.recording_page
    window._update_live()
    page.change_sources.emit("clicked")
    dialog = page.sources_dialog
    assert dialog.radios[(True, True)].get_active()
    assert dialog.path_title.get_label() == text("common.listeningInsideBrowser")
    shot(window, "-browser")
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    shot(window, "-browser-dark")
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    attempted, retry = Future(), Future()
    with patch.object(app, "change_recording_sources", side_effect=[attempted, retry]):
        dialog.radios[(True, False)].set_active(True)
        assert not dialog.choices.get_sensitive()
        attempted.set_exception(OSError("Fixture source unavailable"))
        until(lambda: dialog.future is None)
        assert dialog.radios[(True, True)].get_active() and dialog.failure.get_visible()
        assert dialog.failure.get_first_child().get_next_sibling().get_label() == text("whatIsRecorded.changeFailed")
        shot(window, "-failed")
        dialog.failure.get_last_child().emit("clicked")
        retry.set_result(SourceState(True, False, "none", "user", 100))
        until(lambda: dialog.future is None)
        assert dialog.radios[(True, False)].get_active() and not dialog.failure.get_visible()
        assert dialog.path_title.get_label() == text("whatIsRecorded.othersHeard.none.title")
    dialog.force_close()
    app._capture_close_allowed = True
    window.close()
    app.do_shutdown()

    os.environ["CASSINI_LIBRARY_DIR"] = str(tmp / "native-library")
    os.environ.pop("CASSINI_SCREENSHOT_ACTIVITY", None)
    os.environ.pop("CASSINI_SCREENSHOT_NOW", None)
    app = CassiniApplication("com.gocassini.SourcesNativeTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(lambda: not window._pending)
    app.start_native(test=True)
    until(
        lambda: (
            window.recording_page is not None
            and not app.recorder.starting  # Each side opens on its own (R79 §1).
            and len(app.recorder.session.manifest["tracks"]) == 2
            and all(t["frames"] >= 9600 for t in app.recorder.session.manifest["tracks"])
        )
    )
    native, page = app.recorder, window.recording_page
    page.change_sources.emit("clicked")
    dialog = page.sources_dialog
    assert dialog.path_title.get_label() == text("whatIsRecorded.othersHeard.system.title")
    assert dialog.path_description.get_label() == text("whatIsRecorded.othersHeard.system.description")
    shot(window, "-system")
    assert page.lines.get_visible()
    why = explain_fallback(window)
    assert why.get_heading() == text("errors.othersFallback.why.title")
    assert why.get_body() == text("errors.othersFallback.why.body")
    why.force_close()
    dialog.radios[(False, True)].set_active(True)
    until(lambda: dialog.future is None and not app._source_changes)
    assert dialog.radios[(False, True)].get_active() and not page.mic_row.get_visible()
    assert native.pipeline.get_by_name("microphone") is None
    assert not app.comfort.status(page.meeting)["silent"]
    dialog.radios[(True, False)].set_active(True)
    until(lambda: dialog.future is None and not app._source_changes)
    assert native.pipeline.get_by_name("system") is None and native.pipeline.get_by_name("microphone") is not None
    assert not page.others_row.get_visible() and page.mic_row.get_visible()
    assert not page.lines.get_visible()
    shot(window, "-me")
    dialog.radios[(True, True)].set_active(True)
    until(lambda: dialog.future is None and not app._source_changes)
    assert page.mic_row.get_visible() and page.others_row.get_visible()
    assert len(native.session.manifest["tracks"]) == 4
    dialog.done.emit("clicked")
    until(lambda: dialog.closed)
    app.stop_native()
    until(lambda: native.pipeline is None and not native.stopping)
    assert native.session.manifest["state"] == "completed"
    assert not app._source_changes

    # R79 §1: sides open after the folder exists, so the Library can scan the
    # live row before the start is reported. The Recording page must still open
    # when it is reported (the GNOME 50 indicator gate lost it, #172). Hold the
    # system side's first buffer until that scan has happened: no timing guess.
    from gi.repository import Gst

    from cassini_talk.native_recorder import NativeRecorder

    held, branch = {}, NativeRecorder._branch

    def slow(recorder, kind):
        if kind != "system":
            return branch(recorder, kind)
        result = Gst.parse_bin_from_description(
            "appsrc name=held is-live=true format=time do-timestamp=true "
            'caps="audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved" ! '
            "appsink name=system emit-signals=true sync=false async=false",
            False,
        )
        result.get_by_name("system").connect("new-sample", recorder._sample, "system")
        held["source"] = result.get_by_name("held")
        return result

    with patch.object(NativeRecorder, "_branch", slow):
        app.start_native(test=True)
        late = app.recorder
        until(
            lambda: (
                late.starting
                and "source" in held
                and any(
                    m.directory == late.session.directory and m.lifecycle == "recording"
                    for m in window.snapshot.meetings
                )
            )
        )
        reported = []
        late.connect("started", lambda *_: reported.append(window.recording_page is not None))
        assert held["source"].emit("push-buffer", Gst.Buffer.new_wrapped(bytes(960))) == Gst.FlowReturn.OK
        until(lambda: bool(reported))
    assert reported == [True], "The Recording page opens when the start is reported"
    assert window.recording_page.meeting.directory == late.session.directory
    app.stop_native()
    until(lambda: late.pipeline is None and not late.stopping)

    # The row can even be scanned before the recorder is active: the scan
    # shows an ordinary meeting. The one-second activity tick replays that
    # stale snapshot after the start is reported. It must not open that row
    # as a meeting page (the GNOME 50 indicator gate saw this, #172): the
    # Recording page opens from a live scan. Hold the recorder's preparation
    # until the Library has scanned the stale row; no timing guess.
    held, prepared = {}, NativeRecorder._prepared
    delivered = window.monitor.callback

    def stale(directory):
        return any(
            m.directory == directory and m.lifecycle not in ("recording", "saving") for m in window.snapshot.meetings
        )

    def scanned(*args):
        delivered(*args)
        if "args" in held and stale(held["args"][1].result()[0].directory):
            prepared(*held.pop("args"))

    def gated(recorder, future, test):
        held["args"] = (recorder, future, test)
        window.monitor.refresh_now()
        return False

    window.back_to_library()  # The stopped recording's own meeting page closes.
    window.monitor.callback = scanned
    try:
        with patch.object(NativeRecorder, "_prepared", gated):
            app.start_native(test=True)
            early = app.recorder
            replayed = []

            def started(*_):
                # The activity tick's replay of the snapshot it already has.
                window._updated(window._source_snapshot or window.snapshot)
                page = window.meeting_page
                replayed.append(page is None or page.meeting.directory != early.session.directory)

            early.connect("started", started)
            until(lambda: bool(replayed))
        assert replayed == [True], "A stale, non-live row never opens as a meeting page"
        until(
            lambda: (
                window.recording_page is not None and window.recording_page.meeting.directory == early.session.directory
            )
        )
        assert window.meeting_page is None or window.meeting_page.meeting.directory != early.session.directory
    finally:
        window.monitor.callback = delivered
    app.stop_native()
    until(lambda: early.pipeline is None and not early.stopping)
    window.close()
    app.do_shutdown()
print(
    "PASS What is recorded: actual radios/source release/new WAVs, browser/system/Me cards, failure/retry, Why and light/dark boards"
)
