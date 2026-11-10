# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep seek links independent while letting Pango draw continuous text runs."""

import re

_SPAN = re.compile(r"<(/?)span\b([^>]*)>")
_SEAM = re.compile(r'</a> (?=<a href="(?:item:\d+|aside:\d+:\d+)">)')


def bridge_link_spaces(markup, colour):
    """Style only generated, inkless separators; leave links and words intact.

    GtkLabel assigns each link an explicit foreground. Unstyled spaces split
    otherwise identical glyph runs into one render node per word. Matching the
    next link's foreground and alpha lets Pango join those runs again.
    """
    rgb = "#" + "".join(f"{min(65535, max(0, round(channel * 65535))):04x}" for channel in colour[:3])
    opacity = min(65535, max(1, round(colour[3] * 65535)))

    spans, cursor = [], 0

    def seam(match):
        nonlocal cursor
        for tag in _SPAN.finditer(markup, cursor, match.start()):
            if tag[1]:
                spans.pop()
            else:
                spans.append("background=" in tag[2])
        cursor = match.end()
        # Pango uses separate background runs: changing their separators can
        # change the fill at glyph boundaries, even with identical text ink.
        if any(spans):
            return match[0]
        following = markup[match.end() : markup.index("</a>", match.end())]
        if 'alpha="60%"' in following:
            alpha = ' alpha="60%"'
        elif opacity < 65535:
            alpha = f' alpha="{opacity}"'
        else:
            alpha = ""
        return f'</a><span foreground="{rgb}"{alpha}> </span>'

    return _SEAM.sub(seam, markup)
