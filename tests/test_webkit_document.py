# SPDX-License-Identifier: GPL-3.0-or-later
"""The HTML emitter must preserve word atoms and D84 placement, including chips."""

import json
from html.parser import HTMLParser

import pytest

from cassini_talk.meeting_text import reading_turns
from cassini_talk.portable import Word
from cassini_talk.webkit_document import document


class Atoms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.rows, self.words = [], []
        self.atom = None
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "article":
            self.rows.append([])
        if tag == "button" and attrs.get("class") == "cassini-word":
            self.atom = [int(attrs["id"][1:]), int(attrs["data-start"]), ""]

    def handle_data(self, value):
        if self.atom is not None:
            self.atom[2] += value

    def handle_endtag(self, tag):
        if tag == "button" and self.atom is not None:
            self.words.append(self.atom)
            self.rows[-1].append(self.atom[0])
            self.atom = None


# Use the same pinned vector directory as the existing D84 gate.
from test_render_vector import VECTORS  # noqa: E402


@pytest.mark.parametrize("path", sorted(VECTORS.glob("*.expected.json")), ids=lambda p: p.stem)
def test_html_emits_each_viewer_atom_at_the_right_place(path):
    vector = json.loads(path.read_text())
    body = json.loads(path.with_name(vector["fixture"]).read_text())
    words = [Word(w["speaker"], w["startMs"], w["endMs"], w["text"], text_is_stored=True) for w in body["items"]]
    html = document(reading_turns(words), str)
    parsed = Atoms(html)
    assert parsed.rows == [[i for member in row["members"] for i in member["wordIndices"]] for row in vector["rows"]]
    assert len(parsed.words) == len(words)
    for index, start, value in parsed.words:
        assert value == words[index].text
        assert start == words[index].start_ms


def test_transcript_text_cannot_create_markup_or_network_resources():
    hostile = '</script><img src="https://example.invalid/private"><script>alert(1)</script>'
    html = document(reading_turns([Word("a", 0, 300, hostile, text_is_stored=True)]), lambda _: hostile)
    assert html.count("<script>") == 1
    assert "<img" not in html
    assert "connect-src &#39;none&#39;" in html
    assert Atoms(html).words[0][2] == hostile
