# SPDX-License-Identifier: GPL-3.0-or-later
"""Status badge pill with per-kind currentColor glyph and token tones (W2.1: GNOME-COM-02, GNOME-COM-03)."""

from functools import cache

from gi.repository import Gdk, Graphene, Gsk, Gtk

# Proposed drawn metrics kept as named constants (W0.6 rule, Badge-GNOME.dc.html).
BADGE_HEIGHT_PX = 22
BADGE_PAD_X_PX = 8
BADGE_GAP_PX = 4
BADGE_RADIUS_PX = 999
BADGE_FONT_PX = 13
BADGE_FONT_WEIGHT = 600

BADGE_DOT_PX = 6
BADGE_SPINNER_PX = 10
BADGE_ALERT_PX = 11
BADGE_RECEIVED_PX = 11
BADGE_SPINNER_TRACK_ALPHA = 0.3

KIND_ALIASES: dict[str, str] = {
    "attention": "needsAttention",
}

KIND_GLYPH: dict[str, str] = {
    "recording": "dot",
    "importing": "spinner",
    "saving": "spinner",
    "transcribing": "spinner",
    "needsAttention": "alert",
    "interrupted": "alert",
    "received": "arrow",
}

GLYPH_SIZE_PX: dict[str, int] = {
    "dot": BADGE_DOT_PX,
    "spinner": BADGE_SPINNER_PX,
    "alert": BADGE_ALERT_PX,
    "arrow": BADGE_RECEIVED_PX,
}


def _circle_path(cx: float, cy: float, radius: float) -> Gsk.Path:
    builder = Gsk.PathBuilder.new()
    builder.add_circle(Graphene.Point().init(cx, cy), radius)
    return builder.to_path()


def _stroke(width: float, *, round_cap: bool = False, round_join: bool = False) -> Gsk.Stroke:
    stroke = Gsk.Stroke.new(width)
    if round_cap:
        stroke.set_line_cap(Gsk.LineCap.ROUND)
    if round_join:
        stroke.set_line_join(Gsk.LineJoin.ROUND)
    return stroke


@cache
def _glyph_primitives() -> dict[str, tuple]:
    spin_arc = Gsk.Path.parse("M8 2a6 6 0 0 1 6 6")
    alert_stem = Gsk.Path.parse("M8 4.8v3.8")
    received_arrow = Gsk.Path.parse("M12 4v10M8 10l4 4 4-4M4 16v3.5h16V16")
    assert spin_arc is not None and alert_stem is not None and received_arrow is not None
    return {
        "dot": (_circle_path(3.0, 3.0, 3.0),),
        "spinner": (
            _circle_path(8.0, 8.0, 6.0),
            spin_arc,
            _stroke(2.4),
            _stroke(2.4, round_cap=True),
        ),
        "alert": (
            _circle_path(8.0, 8.0, 6.5),
            alert_stem,
            _circle_path(8.0, 11.0, 0.95),
            _stroke(1.8),
            _stroke(1.9, round_cap=True),
        ),
        "arrow": (
            received_arrow,
            _stroke(2.4, round_cap=True, round_join=True),
        ),
    }


def normalize_badge_kind(kind: str) -> str:
    canonical = KIND_ALIASES.get(kind, kind)
    if canonical not in KIND_GLYPH:
        raise ValueError(f"Unknown status badge kind: {kind!r}")
    return canonical


def snapshot_badge_glyph(
    snapshot: Gtk.Snapshot,
    glyph_kind: str,
    size_px: int,
    color: Gdk.RGBA,
    *,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Append GSK vector nodes for glyph_kind painted in color (currentColor)."""
    box_w = size_px if width is None else width
    box_h = size_px if height is None else height
    if box_w <= 0 or box_h <= 0:
        return
    prims = _glyph_primitives()[glyph_kind]
    snapshot.save()
    if box_w != size_px or box_h != size_px:
        snapshot.translate(Graphene.Point().init((box_w - size_px) / 2.0, (box_h - size_px) / 2.0))
    if glyph_kind == "dot":
        (circle,) = prims
        snapshot.append_fill(circle, Gsk.FillRule.WINDING, color)
    elif glyph_kind == "spinner":
        ring, arc, ring_stroke, arc_stroke = prims
        track_color = Gdk.RGBA()
        track_color.red = color.red
        track_color.green = color.green
        track_color.blue = color.blue
        track_color.alpha = color.alpha * BADGE_SPINNER_TRACK_ALPHA
        scale = size_px / 16.0
        snapshot.scale(scale, scale)
        snapshot.append_stroke(ring, ring_stroke, track_color)
        snapshot.append_stroke(arc, arc_stroke, color)
    elif glyph_kind == "alert":
        ring, stem, dot, ring_stroke, stem_stroke = prims
        scale = size_px / 16.0
        snapshot.scale(scale, scale)
        snapshot.append_stroke(ring, ring_stroke, color)
        snapshot.append_stroke(stem, stem_stroke, color)
        snapshot.append_fill(dot, Gsk.FillRule.WINDING, color)
    else:
        arrow, arrow_stroke = prims
        scale = size_px / 24.0
        snapshot.scale(scale, scale)
        snapshot.append_stroke(arrow, arrow_stroke, color)
    snapshot.restore()


class BadgeGlyph(Gtk.Widget):
    """GSK vector glyph painted in currentColor with no Cairo runtime dependency."""

    def __init__(self, glyph_kind: str):
        if glyph_kind not in GLYPH_SIZE_PX:
            raise ValueError(f"Unknown badge glyph kind: {glyph_kind!r}")
        super().__init__(
            can_target=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
            css_classes=["badge-glyph", f"badge-glyph-{glyph_kind}"],
        )
        self.glyph_kind = glyph_kind
        self.size_px = GLYPH_SIZE_PX[glyph_kind]

    def do_measure(self, _orientation, _for_size):
        return (self.size_px, self.size_px, -1, -1)

    def do_snapshot(self, snapshot):
        snapshot_badge_glyph(
            snapshot,
            self.glyph_kind,
            self.size_px,
            self.get_color(),
            width=self.get_width(),
            height=self.get_height(),
        )


class StatusBadge(Gtk.Box):
    """Single-line status pill with leading glyph and tabular-numeral label."""

    def __init__(self, kind: str, text_value: str):
        canonical = normalize_badge_kind(kind)
        glyph_kind = KIND_GLYPH[canonical]
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=BADGE_GAP_PX,
            valign=Gtk.Align.CENTER,
            css_classes=["badge", f"badge-kind-{canonical}"],
        )
        self.kind = canonical
        self.glyph_kind = glyph_kind
        self.glyph_widget = BadgeGlyph(glyph_kind)
        self.label_widget = Gtk.Label(
            label=text_value,
            valign=Gtk.Align.CENTER,
            css_classes=["badge-label"],
        )
        self.append(self.glyph_widget)
        self.append(self.label_widget)

    def get_label(self) -> str:
        return self.label_widget.get_label()

    def set_label(self, value: str) -> None:
        self.label_widget.set_label(value)

    def get_text(self) -> str:
        return self.label_widget.get_text()

    def set_text(self, value: str) -> None:
        self.label_widget.set_text(value)
