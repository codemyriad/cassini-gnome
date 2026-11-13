#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""W2.1/W2.2 status badge screenshot fixture: one Library row per badge kind in light, dark, and Italian."""

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
from gi.repository import Gio, GLib, Gtk  # noqa: E402
from painted import apply_screenshot_scheme, painted_node  # noqa: E402

Gio.resources_register(Gio.Resource.load(str(Path(os.environ["CASSINI_TEST_BUILD"]) / "src/cassini-talk.gresource")))
from cassini_talk import tokens, ux_formats  # noqa: E402
from cassini_talk.i18n import language  # noqa: E402
from cassini_talk.library import LibrarySnapshot, Meeting  # noqa: E402
from cassini_talk.main import CassiniApplication  # noqa: E402
from cassini_talk.meeting_state import MeetingError, import_entry_badge  # noqa: E402
from cassini_talk.status_badge import (  # noqa: E402
    BADGE_GAP_PX,
    BADGE_HEIGHT_PX,
    GLYPH_SIZE_PX,
    KIND_GLYPH,
    StatusBadge,
)
from cassini_talk.strings import text  # noqa: E402


@dataclass(frozen=True, kw_only=True)
class ImportingFixtureMeeting(Meeting):
    """Transient import-entry row exercising W0.4 entryVectors import_entry_badge()."""

    def badge(self, *, live_ms=0):
        _ = live_ms
        return import_entry_badge({"importing": True})

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
    assert [b.get_label() for b in badges] == [
        text("states.badge.recording", liveBadge="12:48"),
        text("states.badge.saving"),
        ux_formats.eta(4),
        text("states.badge.needsAttention"),
        text("states.badge.interrupted"),
        text("states.badge.received"),
        text("states.badge.importing"),
    ]
    if language() == "it":
        assert [b.get_label() for b in badges] == [
            "Registrazione 12:48",
            "Salvataggio…",
            "In trascrizione · 4 min",
            "Richiede attenzione",
            "Interrotta",
            "Importata",
            "Importazione…",
        ]
    else:
        assert [b.get_label() for b in badges] == [
            "Recording 12:48",
            "Saving…",
            "Transcribing · 4 min",
            "Needs attention",
            "Interrupted",
            "Imported",
            "Importing…",
        ]

    expected_label_fg = {
        "recording": tokens.token("record-ink", dark=is_dark),
        "saving": tokens.token("ink-2", dark=is_dark),
        "transcribing": tokens.token("ink-2", dark=is_dark),
        "needsAttention": tokens.token("warn-ink", dark=is_dark),
        "interrupted": tokens.token("warn-ink", dark=is_dark),
        "received": tokens.token("ink-2", dark=is_dark),
        "importing": tokens.token("ink-2", dark=is_dark),
    }
    expected_glyph_fg = {
        **expected_label_fg,
        "transcribing": tokens.token("accent", dark=is_dark),
    }
    for badge_widget in badges:
        assert badge_widget.glyph_widget.get_accessible_role() == Gtk.AccessibleRole.PRESENTATION
        assert not badge_widget.glyph_widget.get_can_target()
        assert badge_widget.get_height() == BADGE_HEIGHT_PX, (badge_widget.kind, badge_widget.get_height())
        expected_glyph = KIND_GLYPH[badge_widget.kind]
        expected_px = GLYPH_SIZE_PX[expected_glyph]
        assert badge_widget.glyph_widget.get_width() == expected_px
        assert badge_widget.glyph_widget.get_height() == expected_px
        _, glyph_bounds = badge_widget.glyph_widget.compute_bounds(badge_widget)
        _, label_bounds = badge_widget.label_widget.compute_bounds(badge_widget)
        gap = round(label_bounds.origin.x - (glyph_bounds.origin.x + glyph_bounds.size.width))
        assert gap == BADGE_GAP_PX, (badge_widget.kind, gap)
        assert rgba_hex(badge_widget.glyph_widget.get_color()) == expected_glyph_fg[badge_widget.kind]
        assert rgba_hex(badge_widget.label_widget.get_color()) == expected_label_fg[badge_widget.kind]

    # Verify alias normalization and live label mutation API on StatusBadge.
    alias_badge = StatusBadge("attention", "Initial")
    assert alias_badge.kind == "needsAttention"
    alias_badge.set_label("Recording 1:05")
    assert alias_badge.get_label() == "Recording 1:05"
    assert alias_badge.get_text() == "Recording 1:05"

    # Verify interrupted position lives in the detail preview, outside the badge (W0.4 / L-26).
    def preview_labels():
        out = []

        def walk(widget):
            if isinstance(widget, Gtk.Label):
                out.append(widget.get_label())
            child = widget.get_first_child()
            while child:
                walk(child)
                child = child.get_next_sibling()

        walk(window.meeting_page.lines)
        return out

    def find_row_by_key(target_key):
        cursor = window.rows.get_first_child()
        while cursor:
            if getattr(cursor, "meeting", None) is not None and cursor.meeting.key == target_key:
                return cursor
            cursor = cursor.get_next_sibling()
        raise AssertionError(f"Missing row for {target_key!r}")

    window.rows.select_row(find_row_by_key(interrupted.key))
    expected_interrupted_detail = text("meeting.interruptedAt", position="00:41:12")
    assert expected_interrupted_detail in preview_labels()

    # Restore Needs attention selection for screenshot comparability and verify selected-row invariance.
    window.rows.select_row(find_row_by_key(attention.key))
    selected_row = window.rows.get_selected_row()
    assert selected_row is not None and selected_row.meeting.key == attention.key
    selected_badge = find_badges(selected_row)[0]
    assert rgba_hex(selected_badge.glyph_widget.get_color()) == expected_glyph_fg["needsAttention"]

    node = painted_node(window)
    texture = window.get_renderer().render_texture(node, node.get_bounds())
    assert texture.save_to_png(os.environ["CASSINI_SCREENSHOT_OUT"])
    window.close()
print("W2.2 status badges GUI passed")
