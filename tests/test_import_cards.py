# SPDX-License-Identifier: GPL-3.0-or-later
"""L-25 W4.4-W4.6: drop classification from the shared types list, Importing… and
failed-import (Needs attention) list cards, without rebuilding meeting rows."""

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("GTK/Adwaita window integration runs in distro jobs", allow_module_level=True)
from gi.repository import Gio  # noqa: E402

from cassini_talk.library_window import LibraryWindow  # noqa: E402


class FakeRows:
    def __init__(self):
        self.children = []

    def insert(self, widget, index):
        self.children.insert(index, widget)

    def remove(self, widget):
        self.children.remove(widget)


class Card:
    def __init__(self, kind, *values):
        self.kind, self.values, self.parent = kind, values, None

    def get_parent(self):
        return self.parent


def window_with_cards():
    rows = FakeRows()
    window = SimpleNamespace(
        now_ms=1_793_107_080_000,
        tz="UTC",
        rows=rows,
        monitor=SimpleNamespace(_closed=False),
        snapshot=None,
        _source_snapshot=None,
        _active_imports={},
        _failed_imports={},
        _import_row_widgets=[],
        _rendered_imports=(),
        _rows_layout=None,
        _list_signature=("library", ()),
        opened=[],
    )
    window._day_heading = lambda title: Card("heading", title)

    def importing(stem, filename, started_ms):
        card = Card("importing", stem, filename)
        card.parent = rows
        return card

    def failed(stem, filename, failed_ms, raw):
        card = Card("failed", stem, filename, raw)
        card.parent = rows
        return card

    window._importing_row = importing
    window._failed_import_row = failed
    window.open_files = lambda files: window.opened.append(tuple(f.get_path() for f in files))
    for name in (
        "_sync_import_rows",
        "_clear_import_rows",
        "_begin_import_entries",
        "_finish_import_entries",
        "_add_failed_imports",
        "_dismiss_failed_import",
        "_retry_failed_import",
    ):
        setattr(window, name, getattr(LibraryWindow, name).__get__(window))
    window._queue_sidebar_scrolled = lambda: None
    return window


def kinds(window):
    return [(c.kind, *c.values[:2]) for c in window.rows.children if c.kind != "heading"]


def test_failed_import_keeps_a_needs_attention_card_until_retried_or_dismissed(tmp_path):
    window = window_with_cards()
    token = object()
    a, b = str(tmp_path / "Weekly sync.mp3"), str(tmp_path / "Board.m4a")
    window._begin_import_entries(token, (a, b))
    assert kinds(window) == [("importing", "Weekly sync", "Weekly sync.mp3"), ("importing", "Board", "Board.m4a")]

    window._finish_import_entries(token)
    window._add_failed_imports((b,))
    assert kinds(window) == [("failed", "Board", "Board.m4a")]

    # A new import of other files keeps the failed card below the active ones.
    other = object()
    window._begin_import_entries(other, (a,))
    assert kinds(window) == [("importing", "Weekly sync", "Weekly sync.mp3"), ("failed", "Board", "Board.m4a")]
    window._finish_import_entries(other)

    # Try again replaces the card with a new import of the same path.
    window._retry_failed_import(b)
    assert window.opened == [(b,)] and window._failed_imports == {}
    assert kinds(window) == []

    window._add_failed_imports((a, b))
    window._dismiss_failed_import(a)
    assert kinds(window) == [("failed", "Board", "Board.m4a")]
    # Starting the same file again removes its stale failed card.
    window._begin_import_entries(object(), (b,))
    assert kinds(window) == [("importing", "Board", "Board.m4a")]


def test_cancelled_import_leaves_no_failed_card(tmp_path):
    window = window_with_cards()
    token = object()
    window._begin_import_entries(token, (str(tmp_path / "talk.wav"),))
    window._finish_import_entries(token)
    assert kinds(window) == [] and window._failed_imports == {}


def test_real_failed_card_uses_catalog_badge_and_actions(tmp_path):
    import os

    from gi.repository import Gdk, Gtk

    if not any(os.environ.get(v) for v in ("DISPLAY", "WAYLAND_DISPLAY", "BROADWAY_DISPLAY")):
        pytest.skip("No display for real GTK widgets")
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("No display for real GTK widgets")
    from cassini_talk.strings import text

    window = SimpleNamespace(tz="UTC")
    row = LibraryWindow._failed_import_row(window, "Board", "Board.m4a", 1_793_107_080_000, str(tmp_path / "Board.m4a"))
    labels, buttons = [], []

    def walk(widget):
        child = widget.get_first_child()
        while child is not None:
            if hasattr(child, "get_label") and child.get_label():
                (buttons if isinstance(child, Gtk.Button) else labels).append(child.get_label())
            walk(child)
            child = child.get_next_sibling()

    walk(row)
    assert "failed-import-row" in row.get_css_classes()
    assert text("states.badge.needsAttention") in labels
    assert "Board.m4a" in labels and "Board" in labels
    assert text("common.tryAgain") in buttons


@pytest.mark.parametrize(
    "name, verdict",
    [
        ("meeting.opus", "valid"),
        ("talk.mp3", "valid"),
        ("talk.M4A", "valid"),
        ("talk.wav", "valid"),
        ("song.ogg", "valid"),
        ("Notes.zip", "valid"),
        ("clip.ogv", "invalid"),
        ("song.flac", "invalid"),
        ("video.mp4", "invalid"),
    ],
)
def test_drop_classification_follows_the_shared_types_list(tmp_path, name, verdict):
    result = LibraryWindow._classify_drop_file_sync(Gio.File.new_for_path(str(tmp_path / name)))
    assert result[0] == verdict
    if verdict == "invalid":
        assert result[1] == name
