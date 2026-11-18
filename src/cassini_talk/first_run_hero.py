# SPDX-License-Identifier: GPL-3.0-or-later
"""D105 (L-71) first-run waveform and line glyphs (COMPONENTS.md section 8, HARMONY section 10).

The waveform sits beside the Cassini mark and is as tall as it (40 px). It is the
hero's second-line grey, never red (red means recording), with rounded 3 px bars,
and reads as a stretch of conversation: four phrases, three pauses that drop to
short bars (one a sharp cut, two easing in and out), fading out at the end. The
bars are the ones drawn on board P2FirstRun-Desktop (canvas 1794987301-5bc7), so
the geometry is data and a pure function the tests check without a display.
"""

from __future__ import annotations

from functools import cache

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Graphene, Gsk, Gtk  # noqa: E402

BAR_WIDTH_PX = 3
BAR_GAP_PX = 3
WAVE_HEIGHT_PX = 40  # as tall as the mark (HERO_MARK_HEIGHT_PX)
# Heights (px) and opacities, bar by bar, as drawn on P2FirstRun-Desktop (GNOME and Mac).
DRAWN_HEIGHTS = (
    7, 22, 12, 23, 22, 25, 30, 19, 30, 24, 14, 15, 22, 8, 10, 15, 25, 19, 23, 18,
    19, 8, 6, 6, 8, 13,  # pause 1: easing in and out
    14, 18, 17, 14, 19, 15, 11, 13, 11, 16, 22, 22, 16, 12, 13, 8, 15, 20, 15, 25, 18,
    18, 6, 12,  # pause 2: the sharp cut
    19, 15, 15, 17, 16, 9, 17, 25, 27, 32, 19, 18, 35, 16, 16, 15, 22,
    10, 6, 6, 8, 17,  # pause 3: easing in and out
    22, 23, 19, 15, 8, 11, 6, 6, 6, 6, 6, 6,  # last phrase, fading out
)  # fmt: skip
DRAWN_ALPHAS = (
    1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
    0.93, 0.83, 0.78, 0.78, 0.83, 0.93,
    1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
    0.93, 0.78, 0.93,
    1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
    0.85, 0.78, 0.76, 0.81, 0.9,
    0.87, 0.83, 0.8, 0.77, 0.73, 0.7, 0.67, 0.63, 0.6, 0.57, 0.53, 0.5,
)  # fmt: skip
WAVE_BARS = len(DRAWN_HEIGHTS)
WAVE_WIDTH_PX = WAVE_BARS * BAR_WIDTH_PX + (WAVE_BARS - 1) * BAR_GAP_PX
FADE_TAIL_BARS = 12  # the last phrase fading out; a narrow window keeps it whole
MIN_WAVE_BARS = FADE_TAIL_BARS + 8  # fewest bars a narrow window shrinks the wave to
MIN_WAVE_WIDTH_PX = MIN_WAVE_BARS * BAR_WIDTH_PX + (MIN_WAVE_BARS - 1) * BAR_GAP_PX


def bar_count(width_px: int) -> int:
    """How many whole bars (with gaps) fit in width_px, never more than were drawn."""
    width_px = max(0, int(width_px))
    return min(WAVE_BARS, (width_px + BAR_GAP_PX) // (BAR_WIDTH_PX + BAR_GAP_PX))


def bars(count: int = WAVE_BARS) -> list[tuple[int, float]]:
    """(height, alpha) per bar. Fewer than drawn: cut from the body, keep the fade-out tail."""
    count = max(0, min(WAVE_BARS, int(count)))
    drawn = list(zip(DRAWN_HEIGHTS, DRAWN_ALPHAS, strict=True))
    if count == WAVE_BARS:
        return drawn
    if count <= FADE_TAIL_BARS:
        return drawn[WAVE_BARS - count :]
    return drawn[: count - FADE_TAIL_BARS] + drawn[WAVE_BARS - FADE_TAIL_BARS :]


class HeroWave(Gtk.Widget):
    """Decorative grey waveform on the mark's row. Static: no animation, no timers."""

    def __init__(self):
        super().__init__(
            css_classes=["hero-wave"],
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.START,
            can_target=False,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        self._geometry = None

    def do_measure(self, orientation, _for_size):
        # Natural width is the drawn wave; the minimum is a few bars, so a narrow
        # window shrinks the wave (whole bars only) instead of growing the window.
        if orientation == Gtk.Orientation.HORIZONTAL:
            return MIN_WAVE_WIDTH_PX, WAVE_WIDTH_PX, -1, -1
        return WAVE_HEIGHT_PX, WAVE_HEIGHT_PX, -1, -1

    def _bars(self, width):
        if self._geometry is None or self._geometry[0] != width:
            self._geometry = (width, bars(bar_count(width)))
        return self._geometry[1]

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        # The colour comes from CSS (`.hero-wave { color: var(--cx-ink-3) }`), never a literal (HARMONY 10.1).
        colour = self.get_color()
        x = 0
        for bar_h, alpha in self._bars(width):
            rgba = colour.copy()
            rgba.alpha = colour.alpha * alpha
            rect = Graphene.Rect().init(x, (height - bar_h) / 2, BAR_WIDTH_PX, bar_h)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(rect, BAR_WIDTH_PX / 2)  # fully rounded ends, as drawn
            snapshot.push_rounded_clip(rounded)
            snapshot.append_color(rgba, rect)
            snapshot.pop()
            x += BAR_WIDTH_PX + BAR_GAP_PX


# Outline glyphs as drawn (24 px viewBox, round caps and joins): the accent shield that
# opens the first-run privacy line (D105 §5, was a lock) and the change-folder pencil.
GLYPHS = {
    "shield": ("M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z", 2.0),
    "pencil": ("M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4", 2.2),
}


@cache
def _glyph(name):
    path, width = GLYPHS[name]
    stroke = Gsk.Stroke.new(width)
    stroke.set_line_cap(Gsk.LineCap.ROUND)
    stroke.set_line_join(Gsk.LineJoin.ROUND)
    return Gsk.Path.parse(path), stroke


class LineGlyph(Gtk.Widget):
    """A drawn outline glyph in currentColor (CSS), decorative to assistive technology."""

    def __init__(self, name: str, size_px: int, *, css_classes=(), **kwargs):
        super().__init__(
            can_target=False,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
            css_classes=["line-glyph", f"line-glyph-{name}", *css_classes],
            **kwargs,
        )
        self.name = name
        self.size_px = size_px

    def do_measure(self, _orientation, _for_size):
        return self.size_px, self.size_px, -1, -1

    def do_snapshot(self, snapshot):
        path, stroke = _glyph(self.name)
        scale = self.size_px / 24.0
        snapshot.save()
        snapshot.translate(
            Graphene.Point().init((self.get_width() - self.size_px) / 2, (self.get_height() - self.size_px) / 2)
        )
        snapshot.scale(scale, scale)
        snapshot.append_stroke(path, stroke, self.get_color())
        snapshot.restore()
