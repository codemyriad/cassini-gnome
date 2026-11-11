# SPDX-License-Identifier: GPL-3.0-or-later
"""Four hours of one speaker interrupted by 899 brief replies."""

from cassini_talk.portable import Word


def long_turn_words():
    words = []
    for group in range(900):
        start = group * 16000
        words.extend(Word("person-0", start + i * 200, start + (i + 1) * 200, "example") for i in range(80))
        if group < 899:
            words.append(Word("person-1", start + 15900, start + 16100, "yes"))
    return tuple(words)
