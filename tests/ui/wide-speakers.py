#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Many/long speaker names must not raise the real window's minimum width."""

import cProfile
import io
import os
import pstats
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.export_dialog import ExportDialog  # noqa: E402
from cassini_talk.library import LibrarySpeaker, PreviewTurn  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_text import reading_turns  # noqa: E402
from cassini_talk.portable import Word  # noqa: E402


def until(window, predicate, description):
    """Observe GTK frames; the timer only bounds failure, with no sleeps."""
    if predicate():
        return
    loop = GLib.MainLoop()
    expired = False

    def tick(*_):
        if predicate():
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def timeout():
        nonlocal expired
        expired = True
        loop.quit()
        return GLib.SOURCE_REMOVE

    callback = window.add_tick_callback(tick)
    timer = GLib.timeout_add_seconds(8, timeout)
    window.queue_draw()
    loop.run()
    if expired:
        window.remove_tick_callback(callback)
    else:
        GLib.source_remove(timer)
    assert not expired, f"{description}: width={window.get_width()}, minimum={minimum(window)}"


def minimum(widget):
    return widget.measure(Gtk.Orientation.HORIZONTAL, -1).minimum


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(root / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    app = CassiniApplication("com.gocassini.WideSpeakersTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(window, lambda: not window._pending and window.get_mapped(), "Library scan")
    base = window.selected
    speakers = tuple(
        LibrarySpeaker(
            id=f"speaker-{i}",
            name="Voce non identificata" if i < 8 else ("LongName" * 30 if i == 13 else f"Guest {i}"),
            provenance="recognised" if i >= 8 else None,
            first_spoken_ms=i * 8160,
            colour_index=i,
        )
        for i in range(14)
    )
    words = tuple(Word(speakers[(i // 24) % 14].id, i * 340, i * 340 + 300, f"word{i}") for i in range(6288))
    turns = reading_turns(words)
    many = replace(
        base,
        title="Fourteen speakers",
        speakers=speakers,
        words=words,
        turns=turns,
        preview_turns=tuple(PreviewTurn(s.id, s.first_spoken_ms, "A generated excerpt.") for s in speakers[:3]),
        duration_ms=36 * 60 * 1000,
    )
    one = replace(many, speakers=speakers[:1], preview_turns=many.preview_turns[:1])
    window._selected(window.rows, SimpleNamespace(meeting=one))
    narrow = minimum(window)
    window._selected(window.rows, SimpleNamespace(meeting=many))
    wide = minimum(window)
    print(f"Library minimum: one speaker {narrow}px; fourteen including long names {wide}px", flush=True)
    assert wide <= narrow, (narrow, wide)
    window.set_default_size(1200, 720)
    until(window, lambda: window.get_width() >= 1180, "Enlarge Library")
    window.set_default_size(980, 720)
    until(window, lambda: window.get_width() <= 980, "Shrink Library with fourteen speakers selected")
    print(f"Library resized back to {window.get_width()}px", flush=True)
    chips = []

    def collect(widget):
        if widget.has_css_class("speaker-chip"):
            chips.append(widget)
        child = widget.get_first_child()
        while child:
            collect(child)
            child = child.get_next_sibling()

    collect(window.preview)
    assert len(chips) == 14
    until(window, lambda: len({chip.get_allocation().y for chip in chips}) > 1, "Speaker chips wrap onto rows")
    paintable = Gtk.WidgetPaintable.new(window)
    painted = []

    def snapshot():
        image = Gtk.Snapshot.new()
        paintable.snapshot(image, window.get_width(), window.get_height())
        node = image.to_node()
        if node is None:
            return False
        painted.append(node)
        return True

    until(window, snapshot, "Narrow Library painted")
    node = painted[-1]
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])

    profile = cProfile.Profile()
    started = time.perf_counter()
    profile.enable()
    window.open_meeting(many)
    page = window.meeting_page
    until(window, lambda: bool(page.bound) and page.scroller.get_width() > 0, "Transcript rows laid out")
    page_minimum = minimum(page)
    profile.disable()
    print(
        f"Transcript open/bind: {(time.perf_counter() - started) * 1000:.1f}ms; {len(turns)} turns, {len(words)} words"
    )
    report = io.StringIO()
    pstats.Stats(profile, stream=report).sort_stats("cumulative").print_stats(15)
    print(report.getvalue())
    assert page_minimum <= 980, page_minimum
    assert minimum(page.speakers) <= 260, minimum(page.speakers)
    # Include an unbroken long name in a bound transcript row.
    page.update(replace(many, turns=tuple(replace(turn, speaker=speakers[-1].id) for turn in turns)))
    assert minimum(page) <= 980, minimum(page)
    dialog = ExportDialog(window, many)
    assert minimum(dialog.names) <= 360, minimum(dialog.names)
    dialog.executor.shutdown(wait=True)
    window.close()
print("Wide speaker names GUI passed")
