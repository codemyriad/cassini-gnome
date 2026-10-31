#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D36 real GSK components, frozen columns and warning-free GTK snapshots."""

import json
import os
import sys
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import math  # noqa: E402
from array import array  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

from cassini_talk.spectro_source import SpectroSource  # noqa: E402
from cassini_talk.spectro_view_model import DisplayModel  # noqa: E402
from cassini_talk.spectro_views import NativeFeed, WindowSpectrograms  # noqa: E402
from cassini_talk.spectrogram_strip import SpectrogramStrip  # noqa: E402


def until(predicate):
    loop = GLib.MainLoop()
    failures = []

    def check():
        try:
            if predicate():
                loop.quit()
                return False
        except BaseException as error:
            failures.append(error)
            loop.quit()
            return False
        return True

    def timeout():
        failures.append(AssertionError("GTK spectrogram did not render"))
        loop.quit()
        return False

    tick, limit = GLib.timeout_add(10, check), GLib.timeout_add_seconds(8, timeout)
    loop.run()
    for source in (tick, limit):
        if GLib.MainContext.default().find_source_by_id(source):
            GLib.source_remove(source)
    if failures:
        raise failures[0]


Adw.init()
css = Gtk.CssProvider()
css.load_from_string(".spectro-test-dark { background: #242424; color: white; padding: 12px; border-radius: 8px; }")
Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
app = Adw.Application(application_id="io.codemyriad.SpectrogramTest")
app.register(None)
window = Adw.ApplicationWindow(application=app, title="D36 component harness", default_width=1120, default_height=720)
body = Gtk.Box(
    orientation=Gtk.Orientation.VERTICAL, spacing=30, margin_top=48, margin_bottom=48, margin_start=48, margin_end=48
)
window.set_content(body)
model = DisplayModel()
model.set_events(
    [
        dict(type="selection", atMs=0, me=True, others=True),
        dict(type="selection", atMs=6000, me=True, others=False),
        dict(type="selection", atMs=7000, me=True, others=True),
    ]
)
model.set_warning(6500, True)
model.advance(0)
golden = json.loads((ROOT / "tests/spectro-vectors/spectro-v1.golden.json").read_text())
rows = [(index + 1, 0, bytes(bands)) for index, bands in enumerate(golden["display"]) if not 225 <= index <= 240]
model.ingest(0, dict(me=rows, others=rows))
model.advance(8000)
strips = []
for title, dark, reduced in (("Light", False, False), ("Dark", True, False), ("Reduced motion", False, True)):
    group = Gtk.Box(spacing=30, valign=Gtk.Align.CENTER)
    if dark:
        group.add_css_class("spectro-test-dark")
    group.append(Gtk.Label(label=title, width_request=140, xalign=0))
    for kind, lanes in (("pill", ("me", "others")), ("row", ("me",)), ("row", ("others",))):
        strip = SpectrogramStrip(kind=kind, lanes=lanes, dark=dark, reduced_motion=reduced)
        strip.update(model)
        assert strip.texture_updates == 1
        assert strip.get_accessible_role() == Gtk.AccessibleRole.PRESENTATION
        group.append(strip)
        strips.append(strip)
    body.append(group)
window.present()
until(lambda: all(strip.snapshots for strip in strips))
# Fractional movement needs a snapshot, but no new texture. Reduced motion
# changes only at a released slot, so its update reports no image change.
counts = [strip.texture_updates for strip in strips]
model.advance(8001)
for strip in strips:
    changed = strip.update(model)
    assert changed == (not strip.reduced_motion)
assert [strip.texture_updates for strip in strips] == counts
frames = []
window.add_tick_callback(lambda *_: (frames.append(True), len(frames) < 3)[1])
window.queue_draw()
until(lambda: len(frames) >= 2)
scale = window.get_scale_factor()
assert scale == int(os.environ.get("CASSINI_SCREENSHOT_SCALE", "1"))
paintable, snapshot = Gtk.WidgetPaintable.new(window), Gtk.Snapshot.new()
snapshot.scale(scale, scale)
paintable.snapshot(snapshot, window.get_width(), window.get_height())
node = snapshot.to_node()
viewport = Graphene.Rect()
viewport.init(5 * scale, 5 * scale, 1120 * scale, 720 * scale)
texture = window.get_renderer().render_texture(node, viewport)
assert (texture.get_width(), texture.get_height()) == (1120 * scale, 720 * scale)
assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
# Re-bake on theme change; data remains bounded, and a detached view frees it.
for strip in strips:
    strip.configure(dark=not strip.dark, reduced_motion=strip.reduced_motion)
    assert strip.texture_updates == 2
    strip.clear()
    assert strip.pixels is None and strip.texture is None
# Real GTK mapping owns the actual native FFT source, not a fake tick callback.
while child := body.get_first_child():
    body.remove(child)
source = SpectroSource()
source.declare("mic", kind="microphone")
now = [0]
recorder = SimpleNamespace(spectrogram=source, _start_us=0)
hub = WindowSpectrograms(window)
views = [
    SpectrogramStrip(kind="pill", lanes=("me", "others")),
    SpectrogramStrip(kind="row", lanes=("me",)),
    SpectrogramStrip(kind="row", lanes=("others",)),
]
for strip in views:
    hub.bind(strip, "actual-native", lambda columns: NativeFeed(recorder, columns, clock=lambda: now[0]))
    assert not source.active, "An unparented view must not subscribe"
for strip in views:
    body.append(strip)
until(lambda: source.active and len(source.subscribers) == 1 and bool(hub.tick_id))
first_model = hub.sessions["actual-native"].model
pcm = array("h", [round(2000 * math.sin(2 * math.pi * 900 * n / 48000)) for n in range(24000)]).tobytes()
now[0] = 500
source.observe("mic", pcm, 0, kind="microphone", rate=48000, channels=1, start_ms=0, now_ms=500, level=0.2)
until(lambda: any(first_model.bands["me"]) and all(strip.texture is not None for strip in views))
window.set_visible(False)
until(lambda: not source.active and not source.subscribers and hub.tick_id == 0)
assert all(strip.texture is None and strip.model is None for strip in views)
analysed = source.calls
source.observe("mic", pcm, 500, kind="microphone", rate=48000, channels=1, start_ms=0, now_ms=1000, level=0.2)
assert source.calls == analysed, "A hidden window must perform no analysis"
window.present()
until(lambda: source.active and len(source.subscribers) == 1 and bool(hub.tick_id))
assert hub.sessions["actual-native"].model is not first_model
source.finish()
until(lambda: not source.subscribers and hub.tick_id == 0)
# A status refresh cannot re-open an ended capture's subscription.
hub.bind(views[0], "actual-native", lambda columns: NativeFeed(recorder, columns, clock=lambda: now[0]))
assert not source.subscribers and hub.tick_id == 0
hub.close()
hub.close()
window.destroy()
print("D36 GSK passed: two lanes, wrap/fade, states, themes, fractional motion, sweep and texture lifetime")
print(
    "D36 mapped lifetime passed: one native feed, shared models, FFT delivery, hidden zero-analysis, cold remap and finished unsubscribe"
)
