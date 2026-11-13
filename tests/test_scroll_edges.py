# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""L-32 (D-1011): GNOME header lines appear only once content is scrolled under them."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

STYLE_CSS = Path(__file__).resolve().parents[1] / "src/style.css"
RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
# Header surfaces: the bars over the sidebar list, the meeting/search pane, and the pinned title blocks.
HEADER_CLASSES = ("library-header", "meeting-topbar", "meeting-full-header")
LINE_RE = re.compile(r"(?:^|;)\s*(border(?:-bottom)?(?:-color)?|box-shadow)\s*:\s*([^;]+)")


def _rules():
    text = re.sub(r"/\*.*?\*/", "", STYLE_CSS.read_text(), flags=re.S)
    for selectors, body in RULE_RE.findall(text):
        for selector in selectors.split(","):
            yield selector.strip(), body


def _paints_line(prop, value):
    value = value.strip()
    if prop == "box-shadow":
        return value != "none"
    return "transparent" not in value and value not in ("0", "none")


def test_header_lines_are_drawn_only_in_the_scrolled_state():
    seen = set()
    for selector, body in _rules():
        last = selector.split()[-1] if selector else ""
        header = next((name for name in HEADER_CLASSES if f".{name}" in last), None)
        if header is None:
            continue
        seen.add(header)
        for prop, value in LINE_RE.findall(body):
            if _paints_line(prop, value):
                assert ".scrolled" in last, f"{selector} draws a permanent line ({prop}: {value.strip()})"
    assert seen == set(HEADER_CLASSES)


def test_scrolled_state_restores_the_line():
    scrolled = {selector.split()[-1] for selector, body in _rules() if ".scrolled" in selector and "border" in body}
    assert {".library-header.scrolled", ".meeting-topbar.scrolled", ".meeting-full-header.scrolled"} <= scrolled


@pytest.mark.parametrize("value, expected", [(0.0, False), (24.0, True)])
def test_recording_page_puts_the_scroll_line_under_its_pinned_title_block(value, expected):
    gi = pytest.importorskip("gi")
    for name, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
        gi.require_version(name, version)
    from gi.repository import GLib

    from cassini_talk.recording_page import RecordingPage

    page = SimpleNamespace(
        _closed=False,
        _scroll_source=7,
        _vadj=SimpleNamespace(get_value=lambda: value),
        full_header=Mock(),
        top_bar=Mock(),
    )
    assert RecordingPage._apply_scrolled(page) == GLib.SOURCE_REMOVE
    assert page._scroll_source == 0
    toggled = page.full_header.add_css_class if expected else page.full_header.remove_css_class
    toggled.assert_called_once_with("scrolled")
    page.top_bar.add_css_class.assert_not_called()
