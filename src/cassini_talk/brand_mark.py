# SPDX-License-Identifier: GPL-3.0-or-later
"""Saturn brand mark and lowercase cassini.talk wordmark lockup (L-04, L-15)."""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

RESOURCE_PREFIX = "/io/codemyriad/CassiniTalk/brand/mark"
NAV_MARK_RESOURCE = f"{RESOURCE_PREFIX}/saturn-nav.svg"
MID_MARK_RESOURCE = f"{RESOURCE_PREFIX}/saturn-mid.svg"
FULL_MARK_RESOURCE = f"{RESOURCE_PREFIX}/saturn-full.svg"
FLAT_MARK_RESOURCE = f"{RESOURCE_PREFIX}/mark-flat.svg"

WORDMARK_TEXT = "cassini.talk"
WORDMARK_FONT_PX = 18
WORDMARK_FONT_WEIGHT = 500
NAV_MARK_HEIGHT_PX = 26
MID_MARK_THRESHOLD_PX = 40
MARK_ASPECT_NUM = 184
MARK_ASPECT_DEN = 120


def mark_dimensions(height_px: int = NAV_MARK_HEIGHT_PX) -> tuple[int, int]:
    """Return (width_px, height_px) preserving the 184:120 Saturn mark aspect ratio."""
    h = max(12, int(height_px))
    return round(h * MARK_ASPECT_NUM / MARK_ASPECT_DEN), h


def mark_resource_for_height(height_px: int = NAV_MARK_HEIGHT_PX) -> str:
    """Select saturn-nav at header size (<= 40px) and saturn-mid when larger (L-04, Mark.dc.html)."""
    return MID_MARK_RESOURCE if int(height_px) > MID_MARK_THRESHOLD_PX else NAV_MARK_RESOURCE


class BrandMark(Gtk.Widget):
    """Saturn mark rendered from GResource SVG at exact 184:120 aspect ratio."""

    def __init__(self, height_px: int = NAV_MARK_HEIGHT_PX):
        width_px, resolved_h = mark_dimensions(height_px)
        super().__init__(
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            can_target=False,
        )
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Cassini talk"])
        self.height_px = resolved_h
        self.width_px = width_px
        self.resource_path = mark_resource_for_height(resolved_h)
        if hasattr(Gtk, "Svg"):
            self.paintable = Gtk.Svg.new_from_resource(self.resource_path)
        else:
            self.paintable = Gdk.Texture.new_from_resource(self.resource_path)
        self.set_size_request(width_px, resolved_h)

    def do_measure(self, orientation: Gtk.Orientation, _for_size: int) -> tuple[int, int, int, int]:
        dim = self.width_px if orientation == Gtk.Orientation.HORIZONTAL else self.height_px
        return dim, dim, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w = self.get_width() or self.width_px
        h = self.get_height() or self.height_px
        if w > 0 and h > 0:
            self.paintable.snapshot(snapshot, float(w), float(h))


class BrandLockup(Gtk.Box):
    """In-app Saturn mark paired with lowercase medium-weight cassini.talk wordmark (L-15)."""

    def __init__(self, mark_height_px: int = NAV_MARK_HEIGHT_PX, *, spacing: int = 8):
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=spacing,
            valign=Gtk.Align.CENTER,
        )
        self.add_css_class("brand-lockup")
        self.mark = BrandMark(mark_height_px)
        self.mark.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        self.wordmark = Gtk.Label(label=WORDMARK_TEXT, valign=Gtk.Align.CENTER)
        self.wordmark.add_css_class("brand-wordmark")
        self.append(self.mark)
        self.append(self.wordmark)
