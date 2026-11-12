#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""W2.1 status badge screenshot fixture: one Library row per badge kind in light and dark."""

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

if os.environ.get("CASSINI_SCREENSHOT_ISOLATED") != "1" or os.environ.get("WAYLAND_DISPLAY"):
    raise SystemExit("Use build-aux/ux-screenshot.sh")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib  # noqa: E402
from painted import apply_screenshot_scheme, painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk import tokens  # noqa: E402
from cassini_talk.library import LibrarySnapshot, Meeting  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_state import Badge, MeetingError  # noqa: E402
from cassini_talk.status_badge import (  # noqa: E402
    BADGE_GAP_PX,
    BADGE_HEIGHT_PX,
    GLYPH_SIZE_PX,
    KIND_GLYPH,
    StatusBadge,
)


@dataclass(frozen=True, kw_only=True)
class ImportingFixtureMeeting(Meeting):
    """Preview row for the W2.1 Importing glyph/tone before W2.2 adds lifecycle='importing'."""

    def badge(self, *, live_ms=0):
        _ = live_ms
        return Badge("importing", "Importing…", "grey")

    def row_texts(self):
        return ("", None)


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
    assert not expired, description() if callable(description) else description


def find_badges(widget):
    found = [widget] if isinstance(widget, StatusBadge) else []
    child = widget.get_first_child()
    while child:
        found.extend(find_badges(child))
        child = child.get_next_sibling()
    return found


def rgba_hex(rgba) -> str:
    return f"#{round(rgba.red * 255):02X}{round(rgba.green * 255):02X}{round(rgba.blue * 255):02X}"


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    subprocess.run([sys.executable, str(ROOT / "ux/library-vectors/make.py"), str(root / "fixtures")], check=True)
    schemas = root / "schemas"
    schemas.mkdir()
    shutil.copyfile(
        ROOT / "data/io.codemyriad.CassiniTalk.gschema.xml", schemas / "io.codemyriad.CassiniTalk.gschema.xml"
    )
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    is_dark = os.environ.get("CASSINI_SCREENSHOT_SCHEME") == "dark"
    os.environ.update(
        GSETTINGS_SCHEMA_DIR=str(schemas),
        CASSINI_LIBRARY_DIR=str(root / "fixtures/main/root"),
        CASSINI_SCREENSHOT_ACTIVITY=str(root / "fixtures/main/activity.json"),
        CASSINI_SCREENSHOT_NOW="1793107080000",
    )
    apply_screenshot_scheme()
    app = CassiniApplication("com.gocassini.W2BadgesTest", "test", "development")
    app.register(None)
    app.activate()
    window = app.get_active_window()
    window.set_default_size(1130, 730)
    until(
        window,
        lambda: not window._pending and window.get_width() == 1120 and window.get_height() == 720,
        "Initial Library window mapped at 1120x720",
    )

    by_title = {m.title: m for m in window.snapshot.meetings}
    transcribing = replace(by_title["Client call, Rossi & Partners"], quote_line=None)
    template = replace(by_title["Budget review"], quote_line=None)
    base_ms = 1793106000000

    live = replace(
        by_title["Weekly sync, product team"],
        started_at_ms=1793106312000,
        lifecycle="recording",
        transcript="none",
        speakers=(),
        quote_line=None,
    )

    saving = replace(
        template,
        id="b0000000-0000-4000-8000-000000000001",
        directory=root / "w2-saving",
        collection_key="w2-saving",
        title="Partner handoff",
        started_at_ms=base_ms - 600_000,
        lifecycle="saving",
        transcript="none",
    )
    transcribing_today = replace(
        transcribing,
        directory=root / "w2-transcribing",
        collection_key="w2-transcribing",
        started_at_ms=base_ms - 1_200_000,
    )
    attention = replace(
        template,
        id="b0000000-0000-4000-8000-000000000002",
        directory=root / "w2-attention",
        collection_key="w2-attention",
        title="Q4 roadmap review",
        started_at_ms=base_ms - 1_800_000,
        lifecycle="saved",
        transcript="failed",
        error=MeetingError(id="transcribe-failed", action="retry"),
    )
    interrupted = replace(
        by_title["Design crit"],
        directory=root / "w2-interrupted",
        collection_key="w2-interrupted",
        started_at_ms=base_ms - 2_400_000,
        quote_line=None,
    )
    received = replace(
        template,
        id="b0000000-0000-4000-8000-000000000003",
        directory=root / "w2-received",
        collection_key="w2-received",
        title="Field interview, Milan",
        started_at_ms=base_ms - 3_000_000,
        origin="received",
        lifecycle="saved",
        transcript="done",
    )
    importing_fields = {f.name: getattr(template, f.name) for f in template.__dataclass_fields__.values()}
    importing_fields.update(
        id="b0000000-0000-4000-8000-000000000004",
        directory=root / "w2-importing",
        collection_key="w2-importing",
        title="Workshop audio drop",
        started_at_ms=base_ms - 3_600_000,
    )
    importing = ImportingFixtureMeeting(**importing_fields)

    # Freeze background scan callbacks and crash-recovery projection while keeping CASSINI_SCREENSHOT_ACTIVITY live.
    window._library_scanned = lambda _s: None
    app.library_recovery = None
    fixture_snapshot = LibrarySnapshot(
        meetings=(live, saving, transcribing_today, attention, interrupted, received, importing),
        unavailable=False,
    )
    window._source_snapshot = fixture_snapshot
    window._pending_selection = attention.key
    window._updated(fixture_snapshot)
    until(
        window,
        lambda: len(find_badges(window.rows)) == 7 and all(b.get_height() > 0 for b in find_badges(window.rows)),
        lambda: (
            f"All 7 status badges allocated in sidebar rows (got {[(b.kind, b.get_height()) for b in find_badges(window.rows)]})"
        ),
    )

    badges = find_badges(window.rows)
    assert [b.kind for b in badges] == [
        "recording",
        "saving",
        "transcribing",
        "needsAttention",
        "interrupted",
        "received",
        "importing",
    ]

    expected_fg = {
        "recording": tokens.token("record-ink", dark=is_dark),
        "saving": tokens.token("neutral-ink", dark=is_dark),
        "transcribing": tokens.token("neutral-ink", dark=is_dark),
        "needsAttention": tokens.token("warn-ink", dark=is_dark),
        "interrupted": tokens.token("warn-ink", dark=is_dark),
        "received": tokens.token("ink-2", dark=is_dark),
        "importing": tokens.token("neutral-ink", dark=is_dark),
    }
    for badge_widget in badges:
        assert badge_widget.get_height() == BADGE_HEIGHT_PX, (badge_widget.kind, badge_widget.get_height())
        expected_glyph = KIND_GLYPH[badge_widget.kind]
        expected_px = GLYPH_SIZE_PX[expected_glyph]
        assert badge_widget.glyph_widget.get_width() == expected_px
        assert badge_widget.glyph_widget.get_height() == expected_px
        _, glyph_bounds = badge_widget.glyph_widget.compute_bounds(badge_widget)
        _, label_bounds = badge_widget.label_widget.compute_bounds(badge_widget)
        gap = round(label_bounds.origin.x - (glyph_bounds.origin.x + glyph_bounds.size.width))
        assert gap == BADGE_GAP_PX, (badge_widget.kind, gap)
        assert rgba_hex(badge_widget.glyph_widget.get_color()) == expected_fg[badge_widget.kind]
        assert rgba_hex(badge_widget.label_widget.get_color()) == expected_fg[badge_widget.kind]

    # Verify selected-row invariance: Needs attention is selected and keeps warn-ink, not row inherit.
    selected_row = window.rows.get_selected_row()
    assert selected_row is not None and selected_row.meeting.key == attention.key
    selected_badge = find_badges(selected_row)[0]
    assert rgba_hex(selected_badge.glyph_widget.get_color()) == expected_fg["needsAttention"]

    node = painted_node(window)
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
    window.close()
print("W2.1 status badges GUI passed")
