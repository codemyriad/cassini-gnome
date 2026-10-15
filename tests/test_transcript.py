# SPDX-License-Identifier: GPL-3.0-or-later

from cassini_viewer import transcript
from cassini_viewer.portable import Word


def w(speaker, start, end, text):
    return Word(speaker, start, end, text)


WORDS = [
    w("a", 0, 400, "So"),
    w("a", 400, 800, "I"),
    w("a", 800, 1200, "think"),
    w("a", 1200, 1600, "we"),
    w("a", 1600, 2000, "ship."),
    w("b", 900, 1100, "Yeah."),  # an interjection during a's turn
    w("a", 2100, 2500, "Then"),
    w("c", 2600, 3000, "Agreed,"),
    w("c", 3000, 3400, "totally."),
]


def test_turns_keep_file_order_and_group_runs():
    turns = transcript.build_turns(WORDS)
    assert [t.speaker for t in turns] == ["a", "b", "a", "c"]
    assert turns[0].text == "So I think we ship."
    assert turns[3].first_word == 7
    assert turns[3].end_ms == 3400


def test_short_overlapping_turn_becomes_aside():
    turns = transcript.build_turns(WORDS)
    host, aside = turns[0], turns[1]
    assert aside.host is host
    assert host.asides == [(2, aside)]  # after "think", which started at 800
    assert turns[3].host is None  # does not overlap: its own paragraph


def test_visible_turns_hide_speakers_and_release_asides():
    turns = transcript.build_turns(WORDS)
    assert [t.index for t in transcript.visible_turns(turns, set())] == [0, 2, 3]
    assert [t.index for t in transcript.visible_turns(turns, {"a"})] == [1, 3]


def test_current_turn_prefers_latest_start():
    turns = transcript.build_turns(WORDS)
    assert transcript.current_turn(turns, 1000).index == 0  # aside folds into its host
    assert transcript.current_turn(turns, 2700).index == 3
    assert transcript.current_turn(turns, 99_999) is None


def test_word_at():
    turn = transcript.build_turns(WORDS)[0]
    assert turn.word_at(-1) == -1
    assert turn.word_at(850) == 2


def test_find_matches_is_case_insensitive_and_respects_hidden():
    turns = transcript.build_turns(WORDS)
    assert transcript.find_matches(turns, "YEAH") == [(1, 0)]
    assert transcript.find_matches(turns, "yeah", {"b"}) == []
    assert transcript.find_matches(turns, "  ") == []


def test_format_time():
    assert transcript.format_time(0) == "0:00"
    assert transcript.format_time(98_000) == "1:38"
    assert transcript.format_time(3_723_000) == "1:02:03"


def test_exports():
    turns = transcript.build_turns(WORDS)
    text = transcript.export_text(turns, str.upper, "Standup")
    assert text.splitlines()[2] == "[0:00] A: So I think we ship."
    vtt = transcript.export_webvtt(turns, str.upper)
    assert vtt.startswith("WEBVTT\n")
    assert "00:00:00.900 --> 00:00:01.100\n<v B>Yeah." in vtt
