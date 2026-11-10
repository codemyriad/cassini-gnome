# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reuse exact text measurements when virtual rows are recycled."""

from collections import OrderedDict
from hashlib import sha256

from gi.repository import Gtk, Pango, PangoCairo

# Bounded independently of transcript length. Only metrics and geometry keys
# survive a recycled widget, never widgets, layouts or transcript strings.
_MEASUREMENTS = OrderedDict()
_LIMIT = 4096


class TranscriptLabel(Gtk.Label):
    def _measurement_key(self, orientation, for_size):
        layout = self.get_layout()
        context = layout.get_context()
        attributes = layout.get_attributes()
        description = context.get_font_description()
        language = context.get_language()
        tabs = layout.get_tabs()
        matrix = context.get_matrix()
        settings = self.get_settings()
        content = sha256(layout.get_text().encode())
        content.update(b"\0")
        if attributes is not None:
            content.update(attributes.to_string().encode())
        return (
            content.digest(),
            orientation,
            for_size,
            description.to_string() if description else None,
            language.to_string() if language else None,
            context.get_font_map(),
            context.get_font_map().get_serial(),
            PangoCairo.context_get_resolution(context),
            context.get_base_dir(),
            context.get_gravity(),
            context.get_gravity_hint(),
            context.get_round_glyph_positions(),
            tuple(getattr(matrix, name) for name in ("xx", "xy", "yx", "yy", "x0", "y0")) if matrix else None,
            self.get_direction(),
            self.get_scale_factor(),
            tuple(
                settings.get_property(f"gtk-xft-{name}")
                for name in ("dpi", "antialias", "hinting", "hintstyle", "rgba")
            ),
            self.get_wrap(),
            self.get_wrap_mode(),
            self.get_ellipsize(),
            self.get_single_line_mode(),
            self.get_lines(),
            self.get_width_chars(),
            self.get_max_width_chars(),
            self.get_natural_wrap_mode(),
            self.get_justify(),
            tabs.to_string() if tabs else None,
        )

    def _measure_uncached(self, orientation, for_size):
        if (
            orientation == Gtk.Orientation.HORIZONTAL
            and for_size < 0
            and self.get_wrap()
            and self.get_wrap_mode() == Pango.WrapMode.WORD_CHAR
            and self.get_width_chars() < 0
            and self.get_max_width_chars() < 0
            and self.get_ellipsize() == Pango.EllipsizeMode.NONE
        ):
            # GTK measures WORD_CHAR at width zero to find the widest glyph.
            # That constructs a line for every character in every cached row.
            # These text columns can shrink: their container supplies the
            # viewport width and WORD_CHAR already handles narrow allocation.
            # Keep GTK's unwrapped natural width without that glyph-per-line
            # minimum-width layout. Actual height-for-width stays unchanged.
            layout = self.get_layout().copy()
            layout.set_width(-1)
            natural, _height = layout.get_pixel_size()
            return 0, natural, -1, -1
        minimum, natural, minimum_baseline, natural_baseline = Gtk.Label.do_measure(self, orientation, for_size)
        # The C horizontal vfunc leaves baseline output pointers untouched.
        # GI initializes its temporary outputs to zero rather than GTK's -1.
        if orientation == Gtk.Orientation.HORIZONTAL:
            minimum_baseline = natural_baseline = -1
        return minimum, natural, minimum_baseline, natural_baseline

    def do_measure(self, orientation, for_size):
        key = self._measurement_key(orientation, for_size)
        if key in _MEASUREMENTS:
            _MEASUREMENTS.move_to_end(key)
            return _MEASUREMENTS[key]
        result = self._measure_uncached(orientation, for_size)
        _MEASUREMENTS[key] = result
        if len(_MEASUREMENTS) > _LIMIT:
            _MEASUREMENTS.popitem(last=False)
        return result
