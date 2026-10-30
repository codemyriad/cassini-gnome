# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from cassini_viewer.meeting_text import item_markup, speaking_shares
from cassini_viewer.portable import Word


def test_r12_shares_use_item_durations_and_first_speaking_ties():
    words = [Word("a", 0, 1, "a"), Word("b", 1, 2, "b"), Word("c", 2, 3, "c")]
    assert speaking_shares(words, ["a", "b", "c"]) == dict(a=34, b=33, c=33)
    assert speaking_shares([Word("a", 0, 10, "a")], ["a"]) == {"a": 100}
    assert speaking_shares([Word("a", 0, 0, "zero")], ["a"]) == {"a": 0}
    fixture = [Word("a", 0, 23, "a"), Word("b", 0, 19, "b"), Word("c", 0, 11, "c")]
    assert speaking_shares(fixture, ["a", "b", "c"]) == dict(a=43, b=36, c=21)


def test_r12_current_item_boundaries_future_dimming_and_escaping():
    words = [Word("a", 0, 10, "Past <item>"), Word("a", 10, 20, "Entire current sentence"), Word("a", 20, 30, "Future")]
    value = item_markup(words, 10)
    assert '<span background="#fbe7a1" foreground="#202024">Entire current sentence</span>' in value
    assert '<span alpha="60%">Future</span>' in value
    assert "Past &lt;item&gt;" in value
    assert item_markup(words, 20).count("background=") == 1
    assert "background=" not in item_markup(words, 30)


def test_external_opus_and_folder_share_reading_model_without_writes(tmp_path):
    from test_library import ROOT, capture

    from cassini_viewer.open_meeting import load_open_meeting

    session = capture(tmp_path)
    assert load_open_meeting(session.directory / "session.json").tracks
    path = tmp_path / "file.opus"
    data = (ROOT / "tests/conformance/opus/001-minimal-v1.opus").read_bytes()
    path.write_bytes(data)
    meeting = load_open_meeting(path)
    assert meeting.words and meeting.turns and meeting.audio_file == path
    assert path.read_bytes() == data
    assert not (tmp_path / "meeting.json").exists()


def test_reading_paragraphs_bound_layout_without_losing_timed_items():
    from cassini_viewer.meeting_text import reading_turns

    words = [Word("one", i * 100, i * 100 + 100, str(i)) for i in range(10000)]
    turns = reading_turns(words)
    assert [w for turn in turns for w in turn.words] == words
    assert all(len(t.words) <= 100 for t in turns)


def test_d38_quotes_are_display_only_and_pair_within_a_turn():
    from cassini_viewer.transcript import build_turns, quote_items, turn_text

    texts = ['He said "hello', 'world" then "bye".', 'orphan"']
    assert quote_items(texts) == ("He said “hello", "world” then “bye”.", "orphan")
    assert turn_text(['"', "Hello"]) == "Hello"
    assert turn_text(["Already “paired”", "don't change"]) == "Already “paired” don't change"
    words = [Word("a", 0, 100, 'Hello"'), Word("b", 2000, 2100, '"Goodbye')]
    assert [t.text for t in build_turns(words)] == ["Hello", "Goodbye"]
    assert [w.text for w in words] == ['Hello"', '"Goodbye']


def test_d38_quote_markup_keeps_original_item_links_and_timings():
    words = [Word("a", 0, 10, '"'), Word("a", 10, 20, "<Hello>")]
    value = item_markup(words, 10)
    assert "item:0" not in value
    assert value == '<a href="item:1"><span background="#fbe7a1" foreground="#202024">&lt;Hello&gt;</span></a>'
    paired = [Word("a", 0, 10, '"Hello'), Word("a", 10, 20, 'world"')]
    assert "“Hello" in item_markup(paired, 0) and "world”" in item_markup(paired, 10)
    assert paired[0].text == '"Hello' and paired[1].text == 'world"'


def test_d38_markdown_cleans_turn_text_without_changing_timed_items():
    from copy import deepcopy

    from cassini_viewer.export_model import markdown

    model = dict(
        title="Quote test",
        startMs=None,
        durationMs=None,
        speakers=[dict(id="a", name="A")],
        items=[
            dict(speaker="a", startMs=0, endMs=100, text='Say "hello" then'),
            dict(speaker="a", startMs=100, endMs=200, text='goodbye"'),
        ],
        notes=[],
    )
    before = deepcopy(model)
    assert "Say “hello” then goodbye\n" in markdown(model)
    assert model == before


@pytest.mark.parametrize(
    "words",
    [
        # Quote opens on item 100 and closes on item 101 of one speaker's run.
        [Word("a", i * 10, i * 10 + 10, '"opened' if i == 99 else 'closed"' if i == 100 else "w") for i in range(150)],
        # Quote opens at 1 s and closes at 31 s, past the 30 s paragraph bound.
        [Word("a", 1000, 1500, 'He said "wait'), Word("a", 31000, 31500, 'now" then')],
    ],
)
def test_d38_quotes_pair_across_reading_paragraphs_of_one_speaker_turn(words):
    # Regression (review 118 #2): reading_turns' 100-item/30 s split used to
    # make each paragraph pair alone, deleting both halves of the quote.
    from cassini_viewer.meeting_text import reading_turns
    from cassini_viewer.transcript import turn_text

    turns = reading_turns(words)
    assert len(turns) == 2
    whole = turn_text(w.text for w in words)
    assert " ".join(t.text for t in turns) == whole
    assert "“" in whole and "”" in whole
    shown = "".join(item_markup(t.words, 0, values=t.items) for t in turns)
    assert "“" in shown and "”" in shown
    assert [w.text for w in words] == [w.text for t in turns for w in t.words]


def test_d38_quote_pairing_stops_at_a_speaker_change():
    from cassini_viewer.meeting_text import reading_turns

    turns = reading_turns([Word("a", 0, 10, 'Hi "x'), Word("b", 10, 20, 'y" ok')])
    assert [t.text for t in turns] == ["Hi x", "y ok"]


def test_d38_dropped_quote_with_whitespace_displays_nothing():
    # Regression (review 118 #6): ' "' used to survive as a blank item and a
    # whitespace-only link.
    from cassini_viewer.transcript import quote_items, turn_text

    assert quote_items(["Hello", ' "', "world"]) == ("Hello", "", "world")
    assert turn_text(['" ', "Hello"]) == "Hello"
    words = [Word("a", 0, 10, "Hello"), Word("a", 10, 20, ' "'), Word("a", 20, 30, "world")]
    assert item_markup(words, 100) == '<a href="item:0">Hello</a> <a href="item:2">world</a>'
