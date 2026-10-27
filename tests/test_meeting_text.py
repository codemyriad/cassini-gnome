# SPDX-License-Identifier: GPL-3.0-or-later
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
