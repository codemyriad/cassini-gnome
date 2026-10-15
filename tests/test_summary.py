# SPDX-License-Identifier: GPL-3.0-or-later

from cassini_viewer import summary

TEXT = """# Summary

Chris raised **phone dial-in** support at 9:30 (1:38).

## Action items

- [ ] Chris: try USB tethering [02:23]
- [x] Done thing
  continued here

## Topics
* Dropped connection (1:58) at 4:15
"""


def test_parse_blocks():
    blocks = summary.parse(TEXT)
    kinds = [(b.kind, b.level) for b in blocks]
    assert kinds == [
        ("heading", 1),
        ("paragraph", 0),
        ("heading", 2),
        ("item", 0),
        ("item", 0),
        ("heading", 2),
        ("item", 0),
    ]
    assert "<b>phone dial-in</b>" in blocks[1].markup
    assert blocks[1].markup.endswith('at 9:30 (<a href="seek:98000">1:38</a>).')
    assert blocks[1].seek_ms == 98_000
    assert blocks[3].checked is False and blocks[3].seek_ms == 143_000
    assert blocks[3].markup == "Chris: try USB tethering"
    assert blocks[4].checked is True and blocks[4].markup.endswith("continued here")
    assert blocks[6].checked is None and blocks[6].seek_ms == 118_000


def test_markup_is_escaped():
    markup, ms = summary.inline_markup("a < b & (9:99) 4:15")
    assert markup == "a &lt; b &amp; (9:99) 4:15" and ms is None
