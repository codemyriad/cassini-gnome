# SPDX-License-Identifier: GPL-3.0-or-later
"""L-27 first-run hero wave (COMPONENTS.md section 8, HARMONY section 10).

Square 4 px bars in the Cassini accent, fading at both ends on desktop. The bar
count and height formula are PROPOSED upstream, so the drawn values live in the
named constants below and the geometry is a pure function the tests can check
without a display.
"""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Graphene, Gtk  # noqa: E402

BAR_WIDTH_PX = 4  # L-27: square 4 px bars
BAR_GAP_PX = 4
WAVE_BARS = 64  # drawn first-run count (canvas FirstRun-GNOME); formula PROPOSED upstream
WAVE_HEIGHT_PX = 56
WAVE_WIDTH_PX = WAVE_BARS * BAR_WIDTH_PX + (WAVE_BARS - 1) * BAR_GAP_PX
MIN_WAVE_BARS = 8  # fewest bars a narrow window shrinks the wave to
MIN_WAVE_WIDTH_PX = MIN_WAVE_BARS * BAR_WIDTH_PX + (MIN_WAVE_BARS - 1) * BAR_GAP_PX
MIN_BAR_PX = BAR_WIDTH_PX  # a silent bar is still a square
FADE_FRACTION = 0.22  # share of bars fading at each end (desktop: both ends)


def bar_count(width_px: int) -> int:
    """How many whole bars (with gaps) fit in width_px."""
    width_px = max(0, int(width_px))
    return (width_px + BAR_GAP_PX) // (BAR_WIDTH_PX + BAR_GAP_PX)


def bar_heights(count: int, height_px: int = WAVE_HEIGHT_PX) -> list[int]:
    """Deterministic speech-like envelope, snapped to whole pixels, never below a square bar."""
    heights = []
    for i in range(max(0, count)):
        level = 0.55 + 0.25 * math.sin(i * 0.9) + 0.2 * math.sin(i * 2.3 + 1.0)
        level = min(1.0, max(0.0, level))
        heights.append(max(MIN_BAR_PX, round(level * height_px)))
    return heights


def bar_alphas(count: int, fade_fraction: float = FADE_FRACTION) -> list[float]:
    """Linear fade at both ends (desktop). Symmetric, 1.0 across the middle."""
    if count <= 0:
        return []
    fade = max(1.0, count * fade_fraction)
    return [min(1.0, (i + 1) / fade, (count - i) / fade) for i in range(count)]


class HeroWave(Gtk.Widget):
    """Decorative wave beside the first-run Record pill. Static: no animation, no timers."""

    def __init__(self, width_px: int = WAVE_WIDTH_PX, height_px: int = WAVE_HEIGHT_PX):
        super().__init__(
            css_classes=["hero-wave"],
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.START,
            can_target=False,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        self.width_px = width_px
        self.height_px = height_px
        self._geometry = None

    def do_measure(self, orientation, _for_size):
        # Natural width is the drawn wave; the minimum is a few bars, so a narrow
        # window shrinks the wave (whole bars only) instead of growing the window.
        if orientation == Gtk.Orientation.HORIZONTAL:
            return min(self.width_px, MIN_WAVE_WIDTH_PX), self.width_px, -1, -1
        return self.height_px, self.height_px, -1, -1

    def _bars(self, width, height):
        if self._geometry is None or self._geometry[0] != (width, height):
            count = bar_count(width)
            self._geometry = ((width, height), bar_heights(count, height), bar_alphas(count))
        return self._geometry[1], self._geometry[2]

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        # The colour comes from CSS (`.hero-wave { color: var(--cx-accent) }`), never a literal (HARMONY 10.1).
        colour = self.get_color()
        heights, alphas = self._bars(width, height)
        x = 0
        for bar_h, alpha in zip(heights, alphas, strict=True):
            rgba = colour.copy()
            rgba.alpha = colour.alpha * alpha
            rect = Graphene.Rect().init(x, (height - bar_h) / 2, BAR_WIDTH_PX, bar_h)
            snapshot.append_color(rgba, rect)
            x += BAR_WIDTH_PX + BAR_GAP_PX
