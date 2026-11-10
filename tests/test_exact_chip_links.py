# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic parts of an exact-renderer chip retain its first timed word."""
import re

from cassini_talk.meeting_text import reading_markup, reading_word
from cassini_talk.portable import Word
from cassini_talk.transcript import Turn


def test_chip_synthetic_hits_and_words_have_original_timing():
    host = Turn(0, "a", [Word("a", 0, 1000, "Hello"), Word("a", 1000, 2000, "again")], 0, 0, 2000)
    reply = Turn(1, "b", [Word("b", 400, 600, "Sounds"), Word("b", 600, 900, "good.")], 1, 400, 900)
    host.asides = [(0, reply)]
    markup = reading_markup(host, 500, lambda s: {"a": "Ada", "b": "Ben"}[s], chip_links=True)
    links = re.findall(r'<a href="([^"]+)">(.*?)</a>', markup)
    for uri, value in links:
        if "Ben:" in value or value in (" ", ")"):
            assert reading_word(host, uri) is reply.words[0]
    assert reading_word(host, "aside:0:1") is reply.words[1]
    assert markup.count('href="aside:0:0"') == 4
    # No default/standalone markup change in this opt-in feature.
    assert '<a href="aside:0:0">(<b>' not in reading_markup(host, 500, lambda s: s)
