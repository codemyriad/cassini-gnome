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
    w("b", 1900, 2200, "Yeah."),  # an interjection during a's turn
    w("a", 2100, 2500, "Then"),
    w("c", 2600, 3000, "Agreed,"),
    w("c", 3000, 3400, "totally."),
]


def test_turns_keep_file_order_and_group_runs():
    turns = transcript.build_turns(WORDS)
    assert [t.speaker for t in turns] == ["a", "b", "c"]
    assert turns[0].text == "So I think we ship. Then"
    assert turns[2].first_word == 7
    assert turns[2].end_ms == 3400


def test_short_overlapping_turn_becomes_aside():
    turns = transcript.build_turns(WORDS)
    host, aside = turns[0], turns[1]
    assert aside.host is host
    assert host.asides == [(4, aside)]  # at the original fragment seam, after "ship."
    assert turns[2].host is None  # does not overlap: its own paragraph


def test_visible_turns_show_all_participants_and_fold_asides():
    turns = transcript.build_turns(WORDS)
    assert [t.index for t in transcript.visible_turns(turns)] == [0, 2]


def test_current_turn_prefers_latest_start():
    turns = transcript.build_turns(WORDS)
    assert transcript.current_turn(turns, 1000).index == 0  # aside folds into its host
    assert transcript.current_turn(turns, 2700).index == 2
    assert transcript.current_turn(turns, 99_999) is None


def test_word_at():
    turn = transcript.build_turns(WORDS)[0]
    assert turn.word_at(-1) == -1
    assert turn.word_at(850) == 2


def test_find_matches_includes_interjections():
    turns = transcript.build_turns(WORDS)
    assert transcript.find_matches(turns, "YEAH") == [(1, 0)]
    assert transcript.find_matches(turns, "  ") == []


def test_format_time():
    assert transcript.format_time(0) == "0:00"
    assert transcript.format_time(98_000) == "1:38"
    assert transcript.format_time(3_723_000) == "1:02:03"


def test_exports():
    turns = transcript.build_turns(WORDS)
    text = transcript.export_text(turns, str.upper, "Standup")
    assert text.splitlines()[2] == "[0:00] A: So I think we ship. Then"
    vtt = transcript.export_webvtt(turns, str.upper)
    assert vtt.startswith("WEBVTT\n")
    assert "00:00:01.900 --> 00:00:02.200\n<v B>Yeah." in vtt


def test_rapid_clean_exchange_stays_separate():
    turns = transcript.build_turns(
        [w("a", 0, 1000, "Question?"), w("b", 1050, 1150, "Yes."), w("a", 1200, 2000, "Good.")]
    )
    assert [t.text for t in transcript.visible_turns(turns)] == ["Question?", "Yes.", "Good."]


def test_long_pause_and_massive_self_overlap_do_not_rejoin():
    for start in (3000, 500):
        turns = transcript.build_turns(
            [w("a", 0, 2000, "First"), w("b", 1900, 2200, "Right"), w("a", start, 4000, "Second")]
        )
        assert len(transcript.visible_turns(turns)) == 3


def test_sustained_double_talk_is_not_scattered_into_chips():
    words = []
    for i in range(12):
        words.extend([w("a", i * 300, (i + 1) * 300, f"a{i}"), w("b", i * 300 + 20, (i + 1) * 300 + 20, f"b{i}")])
    turns = transcript.build_turns(words)
    assert len(turns) == 2
    assert all(t.host is None for t in turns)
    assert [word for t in turns for word in t.words if word.speaker == "a"] == words[::2]
    assert sum(len(t.words) for t in turns) == len(words)


def test_word_count_does_not_disqualify_short_backchannel():
    words = [w("a", 0, 1000, "Before")]
    words += [w("b", 900 + i * 100, 1000 + i * 100, text) for i, text in enumerate(["Yes", "I", "think", "so"])]
    words += [w("a", 1100, 3000, "after")]
    turns = transcript.build_turns(words)
    assert turns[1].host is turns[0]
    assert turns[0].asides[0][0] == 0


def test_unsplit_host_does_not_swallow_another_turn():
    turns = transcript.build_turns([w("a", 0, 4000, "Speech"), w("b", 1500, 1800, "Right")])
    assert len(transcript.visible_turns(turns)) == 2


def test_search_and_export_keep_rejoined_words_and_asides_once():
    turns = transcript.build_turns(WORDS)
    assert transcript.find_matches(turns, "Then") == [(0, 5)]
    text = transcript.export_text(turns, str.upper)
    assert text.count("Then") == 1
    assert text.count("Yeah.") == 1


def test_short_fragments_weaving_through_host_are_competing_speech():
    words = []
    for i in range(5):
        words.extend([w("a", i * 200, (i + 1) * 200, "A"), w("b", i * 200 + 20, (i + 1) * 200 + 20, "B")])
    turns = transcript.build_turns(words)
    assert len(transcript.visible_turns(turns)) == 2


def test_overlap_badge_uses_word_intersections_not_paragraph_extent():
    turns = transcript.build_turns([w("a", 0, 500, "One"), w("a", 3000, 3500, "two"), w("b", 1000, 2000, "Reply")])
    assert all(not t.over for t in turns)
    turns = transcript.build_turns([w("a", 0, 2000, "One"), w("b", 1500, 2500, "Two")])
    assert turns[0].over == ["b"]
    assert turns[1].over == ["a"]
