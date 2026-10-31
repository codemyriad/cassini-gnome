#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Production GTK meter subscription to a separate process with four voices."""

import json
import math
import os
import select
import shutil
import subprocess
import sys
import tempfile
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
from gi.repository import Gio, GLib, Graphene, Gtk  # noqa: E402
from readiness import wait_until  # noqa: E402
from spectro_memory import render_state_bytes, source_digest  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.capture_activity import BrowserActivity  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402


def until(predicate, description):
    return wait_until(predicate, description, timeout=15)


def reply(child, command=None):
    if command:
        child.stdin.write(command + "\n")
        child.stdin.flush()
    assert select.select([child.stdout], [], [], 10)[0], "broker measurement command timed out"
    line = child.stdout.readline()
    assert line, f"broker probe exited ({child.poll()})"
    return json.loads(line)


with tempfile.TemporaryDirectory() as temporary:
    tmp = Path(temporary)
    schemas = tmp / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(GSETTINGS_SCHEMA_DIR=str(schemas), CASSINI_LIBRARY_DIR=str(tmp / "Library"))
    path = tmp / "broker/browser.sock"
    child = subprocess.Popen(
        [sys.executable, str(ROOT / "tests/spectro_broker_probe.py"), str(tmp), str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    app = window = services = None
    try:
        assert reply(child)["ready"]
        services = DesktopServices()
        app = CassiniApplication("io.codemyriad.SpectrogramBrowserTest", "test", "development")
        app.register(None)
        app.activate()
        window = app.get_active_window()
        window.set_default_size(1130, 730)
        window.navigation.set_animate_transitions(False)
        window._activity_source = BrowserActivity(window._activity_tick, path=path)
        until(lambda: window.live_meeting() is not None, "independent browser capture in Library")
        window.open_meeting(window.live_meeting())
        page = window.recording_page
        views = (page.capture._controls["picture"], page.mic_level, page.others_level)
        until(
            lambda: (
                all(v.model and v.texture for v in views)
                and all(any(views[0].model.bands[s]) for s in ("me", "others"))
            ),
            "broker PCM reaches both actual application lanes",
        )
        assert len(window.spectrograms.sessions) == 1
        model = views[0].model
        assert all(v.model is model for v in views)
        until(lambda: model.last is not None and model.last >= 200, "four-second browser history")
        frames = []
        measure = os.environ.get("CASSINI_SPECTRO_MEASURE") == "D"
        if measure:
            source_hash = source_digest()
            before = dict(
                tick=window.spectrograms.tick_ns, snapshots=sum(v.snapshot_ns for v in views), late=model.late
            )
            reply(child, "begin")
            start, cpu = time.monotonic(), time.process_time()
            handler = window.add_tick_callback(
                lambda _w, clock: (frames.append(clock.get_frame_time() / 1000), True)[1]
            )
            loop = GLib.MainLoop()
            GLib.timeout_add(60000, lambda: (loop.quit(), False)[1])
            loop.run()
            elapsed = time.monotonic() - start
            window.remove_tick_callback(handler)
            report = reply(child, "report")
            intervals = sorted(b - a for a, b in zip(frames, frames[1:], strict=False))
            assert source_digest() == source_hash, "source changed during browser measurement"
            report.update(
                sourceSha256=source_hash,
                renderStateBytes=render_state_bytes(views),
                case="D",
                appCpuPercent=100 * (time.process_time() - cpu) / elapsed,
                drawingTickPercent=(window.spectrograms.tick_ns - before["tick"]) / (elapsed * 1e7),
                snapshotPercent=(sum(v.snapshot_ns for v in views) - before["snapshots"]) / (elapsed * 1e7),
                frameP95Ms=intervals[math.ceil(len(intervals) * 0.95) - 1],
                frameMaxMs=max(intervals),
                late=model.late - before["late"],
            )
            assert report["selected"] == dict(me=1, others=3) and not report["errors"], report
            Path(os.environ["CASSINI_SCREENSHOT_OUT"]).with_suffix(".json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps(report))
        snapshot = Gtk.Snapshot.new()
        Gtk.WidgetPaintable.new(window).snapshot(snapshot, window.get_width(), window.get_height())
        node, bounds = snapshot.to_node(), Graphene.Rect()
        bounds.init(0, 0, 1120, 720)
        texture = window.get_renderer().render_texture(node, bounds)
        assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
        window.set_visible(False)
        until(
            lambda: not window.spectrograms.sessions and not window.spectrograms.tick_id,
            "hidden browser view released subscription",
        )
    finally:
        if child.poll() is None:
            child.stdin.write("quit\n")
            child.stdin.flush()
        try:
            child.wait(15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(5)
            raise AssertionError("broker measurement did not exit after quit") from None
        if window:
            window._activity_source.close()
            window._activity_source = None
            app._capture_close_allowed = True
            window.close()
            app.comfort.close()
        if services:
            services.close()
    assert child.returncode == 0, "private browser capture failed"
print(
    "PASS separate-process browser spectrogram: four remote tracks, native microphone, shared model and hidden detach"
)
