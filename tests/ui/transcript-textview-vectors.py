#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""D84 projection and Unicode seek offsets through the real GtkTextBuffer."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from cassini_talk.meeting_text import reading_turns, row_caption  # noqa: E402
from cassini_talk.portable import Word  # noqa: E402
from cassini_talk.transcript import row_text  # noqa: E402
from cassini_talk.transcript_textview import TranscriptTextView  # noqa: E402

Gtk.init()
page = SimpleNamespace(player=SimpleNamespace(position=0))
count = 0
for path in sorted((ROOT / "tests/ux/interjections").glob("*.expected.json")):
    vector = json.loads(path.read_text())
    body = json.loads(path.with_name(vector["fixture"]).read_text())
    words = [
        Word(
            i["speaker"], i["startMs"], i["endMs"], i["text"], i.get("lowConfidenceSpeaker", False), text_is_stored=True
        )
        for i in body["items"]
    ]
    rows = reading_turns(words)
    meeting = SimpleNamespace(turns=rows, speakers=(), speaker_for=lambda speaker: SimpleNamespace(name=speaker))
    view = TranscriptTextView(page)
    view.set_meeting(meeting)
    expected = "".join(
        (row_caption(row, str) + " · " if row_caption(row, str) else "") + row_text(row)[0] + "\n" for row in rows
    )
    actual = view.buffer.get_text(view.buffer.get_start_iter(), view.buffer.get_end_iter(), True)
    assert actual == expected, path.name
    for start, end, word in view.ranges:
        value = view.buffer.get_text(view.buffer.get_iter_at_offset(start), view.buffer.get_iter_at_offset(end), True)
        assert value == word.text, (path.name, value, word.text)
        assert view.word_at_offset(start) is word
        assert view.word_at_offset(end - 1) is word
    assert len(view.ranges) == sum(bool(w.text) for w in words)
    count += 1
# Character offsets must survive multibyte stored words and empty atoms.
words = [
    Word("a", 0, 300, "café", text_is_stored=True),
    Word("a", 300, 400, "", text_is_stored=True),
    Word("a", 400, 800, "🛰️", text_is_stored=True),
]
meeting = SimpleNamespace(
    turns=reading_turns(words), speakers=(), speaker_for=lambda speaker: SimpleNamespace(name=speaker)
)
view = TranscriptTextView(page)
view.set_meeting(meeting)
assert [view.word_at_offset(a).text for a, _b, _word in view.ranges] == ["café", "🛰️"]
print(f"PASS {count} D84 GtkTextBuffer projections, seek ranges, Unicode and empty atoms", flush=True)
