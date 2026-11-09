# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep seek links independent while letting Pango draw continuous text runs."""

import re

_SEAM = re.compile(r'</a> (?=<a href="(?:item:\d+|aside:\d+:\d+)">)')


def bridge_link_spaces(markup, colour):
    """Style only generated, inkless separators; leave links and words intact.

    GtkLabel assigns each link an explicit foreground. Unstyled spaces split
    otherwise identical glyph runs into one render node per word. Matching the
    next link's foreground and alpha lets Pango join those runs again.
    """
    rgb = "#" + "".join(f"{round(channel * 65535):04x}" for channel in colour[:3])
    opacity = round(colour[3] * 65535)

    def seam(match):
        following = markup[match.end() : markup.index("</a>", match.end())]
        alpha = "60%" if 'alpha="60%"' in following else str(opacity)
        return f'</a><span foreground="{rgb}" alpha="{alpha}"> </span>'

    return _SEAM.sub(seam, markup)
