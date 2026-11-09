#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture synthetic interjections with this checkout or an unmodified baseline.

Run in a private display like the other UI scripts. CASSINI_SCREENSHOT_SOURCE
may point to a baseline worktree; both captures use these exact timed words.
The underlying fixture audio is a tone/silence, not a speech recording.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use a private Xvfb display (build-aux/ux-screenshot.sh)")
ROOT = Path(os.environ.get("CASSINI_SCREENSHOT_SOURCE", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests/ui"))
sys.path.insert(0, str(ROOT / "tests"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from desktop_services import DesktopServices  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402
from painted import painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk.library import LibrarySpeaker  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_text import reading_turns, speaker_turns  # noqa: E402
from cassini_talk.portable import Word  # noqa: E402


def until(check):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if check():
            return
        time.sleep(0.01)
    assert check(), "Synthetic transcript did not settle"


# A brief reply lands inside one continuous sentence.
acknowledgment = [
    Word("ada", i * 500, (i + 1) * 500, word)
    for i, word in enumerate(
        [
            "The",
            "release",
            "candidate",
            "is",
            "ready",
            "for",
            "testing,",
            "and",
            "I",
            "will",
            "send",
            "the",
            "download",
            "link",
            "after",
            "this",
            "call.",
        ]
    )
] + [Word("ben", 3050, 3550, "Sounds good.")]
# A producer sorted each speaker's words by start time, shredding both sentences.
competing = [
    Word(speaker, i * 500 + offset, (i + 1) * 500 + offset, word)
    for speaker, offset, sentence in (
        ("ada", 0, "I can check the Linux build and report any problems today."),
        ("ben", 50, "I will test the browser connection and check the microphone settings."),
    )
    for i, word in enumerate(sentence.split())
]
scenarios = (("acknowledgment", acknowledgment), ("competing", competing))
output = Path(os.environ["CASSINI_SCREENSHOT_OUT"])
output.parent.mkdir(parents=True, exist_ok=True)
services = DesktopServices()
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    library = root / "library"
    library.mkdir()
    source = next(
        path.parent
        for path in (root / "fixtures/main/root").glob("*/session.json")
        if json.loads(path.read_text())["title"] == "Client call, Rossi & Partners"
    )
    shutil.move(str(source), library / source.name)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copy(ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas)
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(library),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    app = CassiniApplication("io.codemyriad.CassiniInterjectionsTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    until(lambda: not window._pending and window.get_mapped())
    window.set_default_size(1200, 850)
    base = next(m for m in window.snapshot.meetings if m.title == "Client call, Rossi & Partners")
    with patch.object(window.monitor, "callback", lambda _snapshot: None):
        for name, items in scenarios:
            words = tuple(sorted(items, key=lambda word: word.start_ms))
            meeting = replace(
                base,
                title="Synthetic sample — "
                + ("brief acknowledgment" if name == "acknowledgment" else "simultaneous speech"),
                speakers=(
                    LibrarySpeaker("ada", "Ada", "user", 0, 0),
                    LibrarySpeaker("ben", "Ben", "user", 3050 if name == "acknowledgment" else 50, 1),
                ),
                words=words,
                turns=reading_turns(words),
                listening_turns=speaker_turns(words),
                duration_ms=12000,
                notes=(),
                transcript="done",
                eta_minutes=None,
            )
            snapshot = replace(
                window._source_snapshot,
                meetings=tuple(meeting if m.key == meeting.key else m for m in window._source_snapshot.meetings),
            )
            window._source_snapshot = snapshot
            window.snapshot = snapshot
            window.open_meeting(meeting)
            page = window.meeting_page
            page.set_connected(True)
            page.recording_notice.set_visible(False)
            window.player.pause()
            window.player.seek(0)
            until(lambda page=page: len(page.bound) >= min(6, len(page.meeting.turns)) and page.get_mapped())
            until(lambda page=page: page.get_width() == window.get_width())
            # Binding can precede the frame that paints all row headers.
            # Wait for two frames so the captured node contains the full page.
            frames = []
            window.add_tick_callback(lambda *_, frames=frames: (frames.append(True), len(frames) < 2)[1])
            window.queue_draw()
            until(lambda frames=frames: len(frames) >= 2)
            target = output.with_name(output.stem + "-" + name + ".png")
            node = painted_node(window)
            assert window.get_renderer().render_texture(node, node.get_bounds()).save_to_png(str(target))
            print(f"{target}: {len(meeting.turns)} reading rows", flush=True)
            window.back_to_library()
    window.destroy()
