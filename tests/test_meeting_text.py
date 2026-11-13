# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from cassini_talk.meeting_text import item_markup, speaking_shares
from cassini_talk.portable import Word


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
    assert '<span background="#BDDCE4" foreground="#191B1F">Entire current sentence</span>' in value
    dark_value = item_markup(words, 10, dark=True)
    assert '<span background="#24708A" foreground="#E8E9EC">Entire current sentence</span>' in dark_value
    assert '<span alpha="60%">Future</span>' in value
    assert "Past &lt;item&gt;" in value
    assert item_markup(words, 20).count("background=") == 1
    assert "background=" not in item_markup(words, 30)


def test_external_opus_and_folder_share_reading_model_without_writes(tmp_path):
    from test_library import ROOT, capture

    from cassini_talk.open_meeting import load_open_meeting

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
    from cassini_talk.meeting_text import reading_turns

    words = [Word("one", i * 100, i * 100 + 100, str(i)) for i in range(10000)]
    turns = reading_turns(words)
    assert [w for turn in turns for w in turn.words] == words
    assert all(len(t.words) <= 140 for t in turns)


def test_d38_quotes_are_display_only_and_pair_within_a_turn():
    from cassini_talk.transcript import build_turns, quote_items, turn_text

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
    assert value == '<a href="item:1"><span background="#BDDCE4" foreground="#191B1F">&lt;Hello&gt;</span></a>'
    paired = [Word("a", 0, 10, '"Hello'), Word("a", 10, 20, 'world"')]
    assert "“Hello" in item_markup(paired, 0) and "world”" in item_markup(paired, 10)
    assert paired[0].text == '"Hello' and paired[1].text == 'world"'


def test_d38_markdown_cleans_turn_text_without_changing_timed_items():
    from copy import deepcopy

    from cassini_talk.export_model import markdown

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
    from cassini_talk.meeting_text import reading_turns
    from cassini_talk.transcript import turn_text

    turns = reading_turns(words)
    assert len(turns) == 2
    whole = turn_text(w.text for w in words)
    assert " ".join(t.text for t in turns) == whole
    assert "“" in whole and "”" in whole
    shown = "".join(item_markup(t.words, 0, values=t.items) for t in turns)
    assert "“" in shown and "”" in shown
    assert [w.text for w in words] == [w.text for t in turns for w in t.words]


def test_d38_quote_pairing_stops_at_a_speaker_change():
    from cassini_talk.meeting_text import reading_turns

    turns = reading_turns([Word("a", 0, 10, 'Hi "x'), Word("b", 10, 20, 'y" ok')])
    assert [t.text for t in turns] == ["Hi x", "y ok"]


def test_d38_dropped_quote_with_whitespace_displays_nothing():
    # Regression (review 118 #6): ' "' used to survive as a blank item and a
    # whitespace-only link.
    from cassini_talk.transcript import quote_items, turn_text

    assert quote_items(["Hello", ' "', "world"]) == ("Hello", "", "world")
    assert turn_text(['" ', "Hello"]) == "Hello"
    words = [Word("a", 0, 10, "Hello"), Word("a", 10, 20, ' "'), Word("a", 20, 30, "world")]
    assert item_markup(words, 100) == '<a href="item:0">Hello</a> <a href="item:2">world</a>'


@pytest.mark.parametrize("origin", ["own", "received"])
def test_cached_turn_speaker_resolves_merge_chain_and_safe_missing_label(tmp_path, origin):
    from dataclasses import replace

    from cassini_talk.library import LibrarySpeaker, Meeting
    from cassini_talk.strings import text

    dana = LibrarySpeaker("dana", "Dana <&>", "user", 0, 2)
    meeting = Meeting(
        id="test",
        directory=tmp_path,
        title="Meeting",
        started_at_ms=None,
        duration_ms=1000,
        origin=origin,
        speakers=(dana,),
        metadata={"speakerMerges": [{"from": "host", "into": "retired"}, {"from": "retired", "into": "dana"}]},
    )
    assert meeting.speaker_for("host") is dana
    assert meeting.speaker_for("retired") is dana
    assert meeting.speaker_for("dana") is dana
    # A stale listed source must still use its final merge target.
    stale = replace(meeting, speakers=(LibrarySpeaker("host", "Old name", "user", 0, 0), dana))
    assert stale.speaker_for("host") is dana
    missing = meeting.speaker_for("unlisted")
    assert missing.id == "unlisted" and missing.provenance == "unnamed"
    assert missing.name == text("common.speaker.unnamed", n=2)
    # Broken/missing targets and invalid/cyclic overlays remain safe to render.
    for entries in (
        None,
        [None],
        [{"from": "host", "into": "absent"}],
        [{"from": "host", "into": "retired"}, {"from": "retired", "into": "host"}],
    ):
        fallback = replace(meeting, metadata={"speakerMerges": entries}).speaker_for("host")
        assert fallback.name == missing.name
    assert meeting.speakers == (dana,)  # Resolution never mutates the source model.


def test_preview_chips_resolve_and_deduplicate_stale_and_unlisted_speakers(tmp_path):
    from dataclasses import replace

    from cassini_talk.library import LibrarySpeaker, Meeting, PreviewTurn
    from cassini_talk.strings import text

    old = LibrarySpeaker("host", "Old host", "user", 0, 0)
    dana = LibrarySpeaker("dana", "Dana", "user", 100, 1)
    ben = LibrarySpeaker("ben", "Ben", "user", 200, 2)
    meeting = Meeting(
        id="preview",
        directory=tmp_path,
        title="Meeting",
        started_at_ms=None,
        duration_ms=1000,
        speakers=(old, dana, ben),
        preview_turns=(PreviewTurn("host", 0, "Hello"), PreviewTurn("missing", 300, "Yes")),
        metadata={"speakerMerges": [{"from": "host", "into": "dana"}]},
    )
    chips = meeting.preview_speakers
    assert [s.id for s in chips] == ["dana", "ben", "missing"]
    assert chips[0] is dana and chips[1] is ben
    assert chips[2].name == text("common.speaker.unnamed", n=4)
    changed = replace(meeting, metadata={"speakerMerges": [{"from": "host", "into": "ben"}]})
    assert changed.preview_speakers[0] is ben
    assert meeting.speakers == (old, dana, ben)


def test_reading_rejoins_interrupted_sentence_and_keeps_named_seekable_reply():
    from cassini_talk.meeting_text import reading_markup, reading_turns, reading_word

    words = [
        Word("a", 0, 1000, "We should"),
        Word("b", 900, 1200, "Yes <&>."),
        Word("a", 1000, 2000, "ship it."),
        Word("c", 2200, 2600, "Agreed."),
    ]
    rows = reading_turns(words)
    assert [t.text for t in rows] == ["We should ship it.", "Agreed."]
    host = rows[0]
    assert host.word_indices == [0, 2]
    assert host.asides[0][0] == 0
    aside = host.asides[0][1]
    assert aside.host is host and aside.word_indices == [1]
    markup = reading_markup(host, 950, lambda speaker: "Ben <&>")
    assert markup.index("We should") < markup.index("Ben &lt;&amp;&gt;") < markup.index("ship it.")
    assert 'href="aside:0:0"' in markup and 'href="item:1"' in markup
    assert 'background="#BDDCE4" foreground="#191B1F">Yes &lt;&amp;&gt;.' in markup
    dark_markup = reading_markup(host, 950, lambda speaker: "Ben <&>", dark=True)
    assert 'background="#24708A" foreground="#E8E9EC">Yes &lt;&amp;&gt;.' in dark_markup
    assert reading_word(host, "aside:0:0") is words[1]
    assert reading_word(host, "item:1") is words[2]
    for uri in ("item:-1", "aside:-1:0", "aside:1:0", "aside:0:-1", "other:0"):
        with pytest.raises(ValueError):
            reading_word(host, uri)
    # Scanner refresh compares models; the aside's host back-reference cannot
    # recurse through dataclass equality.
    assert rows == reading_turns(words)


def test_reading_clean_exchange_and_competing_speech_stay_separate():
    from cassini_talk.meeting_text import reading_turns

    clean = [Word("a", 0, 1000, "Question?"), Word("b", 1050, 1150, "Yes."), Word("a", 1200, 2000, "Good.")]
    assert [t.text for t in reading_turns(clean)] == ["Question?", "Yes.", "Good."]
    woven = []
    for i in range(12):
        woven.extend([Word("a", i * 300, (i + 1) * 300, f"a{i}"), Word("b", i * 300 + 20, (i + 1) * 300 + 20, f"b{i}")])
    rows = reading_turns(woven)
    assert len(rows) == 2 and not any(t.asides for t in rows)
    assert rows[0].words == woven[::2] and rows[1].words == woven[1::2]
    assert rows[0].word_indices == list(range(0, 24, 2))
    assert rows[1].word_indices == list(range(1, 24, 2))


def test_reading_split_places_aside_once_and_preserves_original_indices():
    from cassini_talk.meeting_text import reading_turns

    words = [Word("a", i * 100, i * 100 + 100, str(i)) for i in range(205)]
    reply = Word("b", 9950, 10250, "Right.")
    # File order contains the interruption; sorting words before grouping
    # would lose this seam. A chip must not create a new paragraph.
    words.insert(100, reply)
    rows = reading_turns(words)
    assert [len(t.words) for t in rows] == [205]
    assert [len(t.asides) for t in rows] == [1]
    assert rows[0].asides[0][0] == 99
    owners = [owner for row in rows for owner in [row] + [aside for _, aside in row.asides]]
    assert all(owner.first_word == owner.word_indices[0] for owner in owners)
    indexed = sorted((i, word) for owner in owners for i, word in zip(owner.word_indices, owner.words, strict=True))
    assert [i for i, _ in indexed] == list(range(len(words)))
    assert all(word is words[i] for i, word in indexed)


def test_find_in_meeting_matches_turns_asides_and_renders_active_underline():
    from cassini_talk.meeting_text import (
        build_find_row_highlights,
        find_transcript_matches,
        reading_markup,
        reading_markup_signature,
        reading_turns,
    )
    from cassini_talk.tokens import search_hit_colours, token

    words = [
        Word("a", 0, 1000, "Plan"),
        Word("a", 1000, 2000, "migration & rollout"),
        Word("b", 1200, 1500, "Migration?"),
        Word("a", 2000, 3000, "migration."),
    ]
    rows = reading_turns(words)
    assert len(rows) == 1 and len(rows[0].asides) == 1
    assert find_transcript_matches(rows, "   ") == ()
    matches = find_transcript_matches(rows, "MIGRATION")
    assert [(m.row_index, m.start_ms, m.spans) for m in matches] == [
        (0, 1000, (((0, -1), 1, 0, 9),)),
        (0, 1200, (((0, 0), 0, 0, 9),)),
        (0, 2000, (((0, -1), 2, 0, 9),)),
    ]
    phrase = find_transcript_matches(rows, "plan migration")
    assert len(phrase) == 1 and len(phrase[0].spans) == 2
    highlights_0 = build_find_row_highlights(matches, 0)
    highlights_1 = build_find_row_highlights(matches, 1)
    assert reading_markup_signature(
        rows[0], -1, str.upper, row_highlights=highlights_0.get(0)
    ) != reading_markup_signature(rows[0], -1, str.upper, row_highlights=highlights_1.get(0))
    markup_0 = reading_markup(rows[0], -1, str.upper, row_highlights=highlights_0.get(0))
    active_bg, hit_fg = search_hit_colours(dark=False, selected=True)
    passive_bg, _ = search_hit_colours(dark=False, selected=False)
    accent = token("accent", dark=False)
    assert (
        f'<span background="{active_bg}" foreground="{hit_fg}" underline="single" underline_color="{accent}">migration</span> &amp; rollout'
        in markup_0
    )
    assert f'<span background="{passive_bg}" foreground="{hit_fg}">Migration</span>?' in markup_0
    assert f'<span background="{passive_bg}" foreground="{hit_fg}">migration</span>.' in markup_0
    markup_1 = reading_markup(rows[0], -1, str.upper, row_highlights=highlights_1.get(0))
    assert (
        f'<span background="{active_bg}" foreground="{hit_fg}" underline="single" underline_color="{accent}">Migration</span>?'
        in markup_1
    )
