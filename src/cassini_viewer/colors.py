# colors.py
#
# Copyright 2026 Silvio Tomatis
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Speaker colours from the GNOME palette, with light and dark variants.

The same colours are defined as .speaker-N classes in style.css; keep them in
sync. Markup needs literal colours, widgets use the classes.
"""

PALETTE = [
    # (light, dark)
    ("#9141ac", "#c061cb"),  # purple
    ("#e66100", "#ffa348"),  # orange
    ("#1c71d8", "#62a0ea"),  # blue
    ("#26a269", "#57e389"),  # green
    ("#c01c28", "#f66151"),  # red
    ("#2190a4", "#5bc8af"),  # teal
    ("#c88800", "#f8e45c"),  # yellow
    ("#d56199", "#f599c2"),  # pink
    ("#986a44", "#cdab8f"),  # brown
    ("#6f8396", "#a6b5c4"),  # slate
]
UNKNOWN = ("#77767b", "#9a9996")


class SpeakerPalette:
    """Assigns each speaker a stable colour, in manifest order."""

    def __init__(self, speaker_ids=()):
        self._index: dict[str, int] = {}
        self.extend(speaker_ids)

    def extend(self, speaker_ids):
        for speaker_id in speaker_ids:
            if speaker_id not in self._index:
                self._index[speaker_id] = len(self._index) % len(PALETTE)

    def css_class(self, speaker_id) -> str:
        index = self._index.get(speaker_id)
        return "speaker-unknown" if index is None else f"speaker-{index}"

    def color(self, speaker_id, dark: bool) -> str:
        index = self._index.get(speaker_id)
        pair = UNKNOWN if index is None else PALETTE[index]
        return pair[1] if dark else pair[0]


def hex_color(rgba) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue)))
