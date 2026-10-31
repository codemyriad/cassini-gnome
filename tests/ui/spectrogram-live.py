#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Production capture -> shared window view -> GSK, on a private test display."""

import cProfile
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tests/recording")]
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from desktop_services import DesktopServices  # noqa: E402
from gi.repository import Adw, Gio, GLib, Graphene, Gst, Gtk  # noqa: E402
from readiness import wait_until  # noqa: E402
from spectro_memory import render_state_bytes, source_digest  # noqa: E402
from spectro_wm import WindowManager  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.recording_details import SilenceClock  # noqa: E402
from cassini_talk.spectro_view_model import HOLE, OFF, SILENT  # noqa: E402
from cassini_talk.spectrogram_strip import SpectrogramStrip  # noqa: E402


def until(condition, message):
    return wait_until(
        condition,
        message,
        timeout=15,
        diagnostics=lambda: (
            {}
            if "window" not in globals()
            else dict(
                mapped=window.get_mapped(), suspended=window.is_suspended(), state=str(window.get_surface().get_state())
            )
        ),
    )


def shot(window, name):
    frames = []
    window.add_tick_callback(lambda *_: (frames.append(1), len(frames) < 2)[1])
    window.queue_draw()
    until(lambda: len(frames) >= 2, "window painted before screenshot")
    scale = window.get_scale_factor()
    assert scale == int(os.environ.get("CASSINI_SCREENSHOT_SCALE", "1"))
    snapshot = Gtk.Snapshot.new()
    snapshot.scale(scale, scale)
    Gtk.WidgetPaintable.new(window).snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    viewport = Graphene.Rect()
    viewport.init(0, 0, 1120 * scale, 720 * scale)
    assert (window.get_width(), window.get_height()) == (1120, 720), (window.get_width(), window.get_height())
    texture = window.get_renderer().render_texture(node, viewport)
    assert (texture.get_width(), texture.get_height()) == (1120 * scale, 720 * scale)
    path = Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_name(name + ("@2x" if scale == 2 else "") + ".png")
    assert texture.save_to_png(str(path))


def native_states(window, page, recorder):
    def source(kind):
        iterator = recorder._branches[kind].iterate_recurse()
        while True:
            result, element = iterator.next()
            assert result == Gst.IteratorResult.OK, f"No audiotestsrc in {kind} branch"
            if element.get_factory() and element.get_factory().get_name() == "audiotestsrc":
                return element

    microphone = source("microphone")
    old_volume = microphone.get_property("volume")
    microphone.set_property("volume", 0)
    until(lambda: recorder.levels[0] == 0, "actual microphone AC level became silent")
    key = recorder.session.directory.name
    window.get_application().comfort.clocks[key] = SilenceClock(GLib.get_monotonic_time() / 1e6 - 21)
    window.get_application().comfort.native_level()
    page.update_status()
    until(
        lambda: (
            page.check.get_visible()
            and any(flag & SILENT and flag != HOLE for flag in page.mic_level.model.flags["me"])
        ),
        "silent-microphone warning reached the rendered history",
    )
    shot(window, "spectrogram-silent-dark")
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    shot(window, "spectrogram-silent")
    microphone.set_property("volume", old_volume)
    until(lambda: recorder.levels[0] > 0.1, "microphone AC level resumed")
    window.get_application().comfort.native_level()
    page.update_status()
    system = source("system")
    pad = system.get_static_pad("src")
    token = pad.add_probe(Gst.PadProbeType.BUFFER, lambda *_: Gst.PadProbeReturn.DROP)
    try:
        start = recorder.session.writers[recorder._track_ids["microphone"]].frames
        until(
            lambda: recorder.session.writers[recorder._track_ids["microphone"]].frames >= start + 57600,
            "microphone kept recording through a real system-source dropout",
        )
        assert HOLE in page.others_level.model.flags["others"]
        shot(window, "spectrogram-dropout")
    finally:
        pad.remove_probe(token)
    until(
        lambda: page.others_level.model.cell("others", page.others_level.model.last)[0] != HOLE,
        "system source resumed after the dropout",
    )


def stop_picture(window, recorder):
    entered, release = threading.Event(), threading.Event()

    def pause_finalizer():
        entered.set()
        assert release.wait(10), "test did not release the native finalizer"

    recorder._executor.submit(pause_finalizer)
    until(entered.is_set, "bounded finalizer barrier entered")
    try:
        recorder.stop()
        until(
            lambda: window.recording_page and window.recording_page.meeting.lifecycle == "saving",
            "actual Stop entered saving",
        )
        page = window.recording_page
        strips = (page.capture._controls["picture"], page.mic_level, page.others_level)
        until(
            lambda: not window.spectrograms.tick_id and all(s.has_css_class("spectro-stopping") for s in strips),
            "stopping unsubscribed and froze every strip",
        )
        before = [s.texture_updates for s in strips]
        painted_at = GLib.get_monotonic_time()
        until(lambda: GLib.get_monotonic_time() - painted_at >= 230000, "stopping opacity transition completed")
        assert [s.texture_updates for s in strips] == before
        shot(window, "spectrogram-stopping")
    finally:
        release.set()


def analysed(source):
    with source.lock:
        return source.calls, source.analysed, source.analysis_ns, source.analysis_cpu_ns


def measure(window, recorder, case):
    if case in ("B", "C", "E"):
        window.open_meeting(window.live_meeting())
        until(lambda: window.recording_page and window.recording_page.get_mapped(), "Recording page mapped")
    if case == "C":
        window.minimize()
        until(lambda: not recorder.spectrogram.active and not window.spectrograms.tick_id, "hidden views detached")
    frames = []
    handler = 0
    wait_until(
        lambda: min(w.frames for w in recorder.session.writers.values()) >= 48000 * 4,
        "four-second native PCM history before steady-state measurement",
        timeout=15,
    )
    source_hash = source_digest()
    before = analysed(recorder.spectrogram)
    frames_before = window.spectrograms.frames
    snapshots_before = sum(v.snapshot_ns for v in window.spectrograms.bindings)
    late_before = sum(view.model.late for view in window.spectrograms.sessions.values())
    tick_before = window.spectrograms.tick_ns
    cpu_tick_before = window.spectrograms.tick_cpu_ns
    cpu_snapshots_before = sum(v.snapshot_cpu_ns for v in window.spectrograms.bindings)
    profile = cProfile.Profile() if os.environ.get("CASSINI_SPECTRO_PROFILE") else None
    if profile:
        profile.enable()
    duration = 10 if profile else 60
    if case != "C":
        handler = window.add_tick_callback(lambda _w, clock: (frames.append(clock.get_frame_time() / 1000), True)[1])
    started, cpu = time.monotonic(), time.process_time()
    # This is a timed measurement, not a startup wait. Let GTK run its real
    # blocking main loop: a readiness helper's poll cadence distorts frame pacing.
    loop = GLib.MainLoop()
    GLib.timeout_add(duration * 1000, lambda: (loop.quit(), False)[1])
    loop.run()
    elapsed, cpu = time.monotonic() - started, time.process_time() - cpu
    if profile:
        profile.disable()
        profile.dump_stats(os.environ["CASSINI_SPECTRO_PROFILE"])
    after = analysed(recorder.spectrogram)
    if handler:
        window.remove_tick_callback(handler)
    intervals = sorted(b - a for a, b in zip(frames, frames[1:], strict=False))
    report = dict(
        case=case,
        processRssBytes=int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE"),
        numpyLoaded="numpy" in sys.modules,
        sourceSha256=source_hash,
        seconds=elapsed,
        processCpuPercent=100 * cpu / elapsed,
        drawingTickWallPercent=100 * (window.spectrograms.tick_ns - tick_before) / (elapsed * 1e9),
        snapshotWallPercent=100
        * (sum(v.snapshot_ns for v in window.spectrograms.bindings) - snapshots_before)
        / (elapsed * 1e9),
        drawingTickPercent=(window.spectrograms.tick_cpu_ns - cpu_tick_before) / (elapsed * 1e7),
        snapshotPercent=(sum(v.snapshot_cpu_ns for v in window.spectrograms.bindings) - cpu_snapshots_before)
        / (elapsed * 1e7),
        analysisWallPercent=(after[2] - before[2]) / (elapsed * 1e7),
        analysisCpuPercent=(after[3] - before[3]) / (elapsed * 1e7),
        analysisCalls=after[0] - before[0],
        analysed=after[1] - before[1],
        frameP95Ms=intervals[math.ceil(len(intervals) * 0.95) - 1] if intervals else None,
        frameMaxMs=max(intervals, default=None),
        windowCallbacks=window.spectrograms.frames - frames_before,
        renderStateBytes=render_state_bytes(window.spectrograms.bindings),
        late=sum(view.model.late for view in window.spectrograms.sessions.values()) - late_before,
    )
    assert source_digest() == source_hash, "source changed during spectrogram measurement"
    if case == "C":
        assert after == before and not window.spectrograms.tick_id, report
    Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(tmp / "Library"))
    wm = WindowManager()
    services = DesktopServices()
    app = CassiniApplication("io.codemyriad.SpectrogramLiveTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    window.navigation.set_animate_transitions(False)
    until(lambda: not window._pending, "empty Library ready")
    try:
        app.start_native(test=True)
        until(
            lambda: app.recorder and app.recorder.pipeline is not None and window.live_meeting(),
            "native capture in Library",
        )
        recorder = app.recorder
        window.back_to_library()
        until(lambda: window.recording_page is None and window.capture_header.get_mapped(), "Library header mapped")
        picture = window.capture_header._controls["picture"]
        enabled = os.environ.get("CASSINI_SPECTRO") != "0"
        if enabled:
            until(
                lambda: picture.model and all(any(picture.model.bands[s]) for s in ("me", "others")),
                "both committed native lanes rendered",
            )
            assert len(recorder.spectrogram.subscribers) == 1 and window.spectrograms.tick_id
        case = os.environ.get("CASSINI_SPECTRO_MEASURE")
        if case:
            measure(window, recorder, case)
        else:
            until(
                lambda: min(w.frames for w in recorder.session.writers.values()) >= 48000 * 4,
                "full four-second live history",
            )
            shot(window, "spectrogram-library")
            window.open_meeting(window.live_meeting())
            page = window.recording_page
            until(
                lambda: page.get_mapped() and page.mic_level.texture and page.others_level.texture,
                "real Recording row textures",
            )
            views = (page.capture._controls["picture"], page.mic_level, page.others_level)
            until(
                lambda: all(v.texture is not None and v.model is views[0].model for v in views),
                "all mapped strips share the current model",
            )
            assert all(isinstance(v, SpectrogramStrip) and v.model is views[0].model for v in views)
            assert len(recorder.spectrogram.subscribers) == 1
            assert render_state_bytes(views) <= 128 * 1024, render_state_bytes(views)
            assert [v.get_width() for v in views] == [50, 200, 200]
            assert [v.get_height() for v in views] == [20, 32, 32]
            assert all(v.get_accessible_role() == Gtk.AccessibleRole.PRESENTATION for v in views)
            shot(window, "spectrogram-recording")
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            until(lambda: all(v.dark for v in views), "theme propagated to every strip")
            shot(window, "spectrogram-recording-dark")
            settings = Gtk.Settings.get_default()
            settings.set_property("gtk-enable-animations", False)
            assert all(v.reduced_motion for v in views)
            shot(window, "spectrogram-reduced-motion")
            settings.set_property("gtk-enable-animations", True)
            native_states(window, page, recorder)
            # A user selection is a real checkpointed source retirement/re-enable.
            changed = app.change_recording_sources(page.meeting, True, False)
            until(lambda: changed.done(), "others disabled")
            changed.result()
            page.update_status()
            assert not page.others_row.get_visible()
            until(
                lambda: any(flag == OFF for flag in page.capture._controls["picture"].model.flags["others"]),
                "off selection reached the pill",
            )
            shot(window, "spectrogram-others-off")
            changed = app.change_recording_sources(page.meeting, True, True)
            until(lambda: changed.done(), "others re-enabled")
            changed.result()
            page.update_status()
            until(lambda: page.others_row.get_mapped() and page.others_level.texture, "re-enabled row rendered")
            first_model = page.mic_level.model
            window.minimize()
            until(
                lambda: not recorder.spectrogram.active and not window.spectrograms.tick_id, "hidden analysis stopped"
            )
            before = analysed(recorder.spectrogram)
            frames = min(w.frames for w in recorder.session.writers.values())
            until(
                lambda: min(w.frames for w in recorder.session.writers.values()) >= frames + 4800,
                "hidden recording keeps writing PCM",
            )
            assert analysed(recorder.spectrogram) == before
            window.present()
            until(
                lambda: page.mic_level.model is not None and page.mic_level.model is not first_model,
                "remap starts cold",
            )
            until(lambda: page.mic_level.texture is not None, "remap draws actual sound")
            window.back_to_library()
            assert all(v not in window.spectrograms.bindings for v in views)
            assert page.mic_level.texture is None and page.others_level.texture is None
            app.start_native(test=True)
            second = app._last_native
            until(
                lambda: (
                    second is not recorder
                    and second.pipeline is not None
                    and any(m.directory == second.session.directory for m in window.snapshot.meetings)
                ),
                "second independent native recording appeared in Library",
            )
            window.open_meeting(next(m for m in window.snapshot.meetings if m.directory == second.session.directory))
            until(
                lambda: second.spectrogram.active and window.recording_page.mic_level.texture is not None,
                "second recording owns its own visible feed",
            )
            assert recorder.pipeline is not None and not recorder.spectrogram.active
            shot(window, "spectrogram-second-session")
            second.stop()
            until(lambda: second.pipeline is None and not second.stopping, "second recording stopped independently")
            window.open_meeting(next(m for m in window.snapshot.meetings if m.directory == recorder.session.directory))
            until(
                lambda: recorder.spectrogram.active and window.recording_page.mic_level.texture is not None,
                "first recording resumed its visible feed",
            )
            stop_picture(window, recorder)
        if recorder.pipeline is not None and not recorder.stopping:
            recorder.stop()
        until(lambda: recorder.pipeline is None and not recorder.stopping, "capture stopped and saved")
        assert not recorder.spectrogram.subscribers
    finally:
        active = list(app.native_captures.values())
        for item in active:
            if item.pipeline is not None and not item.stopping:
                item.stop()
        until(lambda: all(item.pipeline is None and not item.stopping for item in active), "all test captures reaped")
        app._capture_close_allowed = True
        window.close()
        assert not window.spectrograms.bindings and not window.spectrograms.tick_id
        app.comfort.close()
        services.close()
        wm.close()

print(
    "PASS live application spectrogram: actual committed PCM, Library/page sharing, theme/sweep, selection, hidden-zero-analysis and page detach"
)
