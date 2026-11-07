# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain-pytest bar decisions: no GI imports, display, widgets or skips."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassini_talk.listening_bar_model import bar_layout, bar_state, excerpt, merge_choices

ORDER = ("ada", "ben", "cleo")


def listening(turns=(None, 1), speakers=(None, "ben"), *, finished=False, cued=False):
    def speaker_step(order, direction):
        assert tuple(order) == ORDER
        return speakers[direction > 0]

    return SimpleNamespace(
        active=True,
        finished=finished,
        cued=cued,
        step_target=lambda direction: turns[direction > 0],
        speaker_step_target=speaker_step,
    )


@pytest.mark.parametrize(
    "turns,speakers,expected",
    [
        ((None, 1), (None, "ben"), (False, True, False, True)),
        ((0, 2), ("ada", "cleo"), (True, True, True, True)),
        ((1, None), ("ben", None), (True, False, True, False)),
        ((None, None), (None, None), (False, False, False, False)),
    ],
)
def test_turn_and_speaker_endpoints_including_target_index_zero(turns, speakers, expected):
    state = bar_state(listening(turns, speakers), ORDER, editable=True, busy=False)
    assert state.visible
    assert (state.previous_turn, state.next_turn, state.previous_speaker, state.next_speaker) == expected


@pytest.mark.parametrize("editable,busy,enabled", [(True, False, True), (False, False, False), (True, True, False)])
def test_readonly_or_pending_edit_keeps_navigation_but_disables_edits(editable, busy, enabled):
    state = bar_state(listening(), ORDER, editable=editable, busy=busy)
    assert state.edits_enabled is enabled
    assert state.next_turn and state.next_speaker


@pytest.mark.parametrize(
    "finished,cued,visible,caption",
    [(False, False, False, "common.play"), (True, False, True, "listen.again"), (False, True, True, "common.play")],
)
def test_finished_and_paused_cue_play_controls(finished, cued, visible, caption):
    state = bar_state(listening(finished=finished, cued=cued), ORDER, editable=True, busy=False)
    assert state.again_visible is visible
    assert state.again_label == caption


def test_stopping_clears_controls_without_asking_for_a_target():
    state = bar_state(SimpleNamespace(active=False, finished=False, cued=False), ORDER, editable=True, busy=False)
    assert not any(
        (
            state.visible,
            state.previous_turn,
            state.next_turn,
            state.previous_speaker,
            state.next_speaker,
            state.edits_enabled,
            state.merge_enabled,
            state.again_visible,
        )
    )


@pytest.mark.parametrize(
    "stage,editable,expected",
    [
        (0, True, (True, True, True, ())),
        (1, True, (False, True, True, ())),
        (2, True, (False, False, True, ("name", "merge"))),
        (3, True, (False, False, False, ("name", "merge", "previous-speaker", "next-speaker"))),
        (0, False, (True, False, True, ())),
        (2, False, (False, False, True, ())),
        (3, False, (False, False, False, ("previous-speaker", "next-speaker"))),
    ],
)
def test_staged_collapse_preserves_speaker_navigation_on_readonly_meetings(stage, editable, expected):
    layout = bar_layout(stage, editable=editable)
    assert (layout.excerpt, layout.edit_buttons, layout.speaker_buttons, layout.menu_actions) == expected


def meeting():
    speakers = tuple(
        SimpleNamespace(id=s, name=s.title(), talk_ms=t)
        for s, t in (("ben", 9000), ("ada", 3000), ("cleo", 700), ("dana", 5000))
    )
    turns = tuple(
        SimpleNamespace(index=i, speaker=s, text=words)
        for i, (s, words) in enumerate(
            (
                ("cleo", "one two three four five six seven"),
                ("ada", "eight nine ten eleven twelve thirteen fourteen fifteen sixteen"),
                ("ben", "Agreed"),
                ("ada", "Thanks"),
                ("ben", "Welcome"),
            )
        )
    )
    return SimpleNamespace(speakers=speakers, listening_turns=turns)


def test_merge_choices_are_neighbour_first_then_talk_time_and_deduplicated():
    model = meeting()
    neighbours, rest = merge_choices(model, model.listening_turns[1])
    assert [(s.id, suffix) for s, suffix in neighbours] == [
        ("cleo", "speakers.mergeInto.speaksBefore"),
        ("ben", "speakers.mergeInto.speaksAfter"),
    ]
    assert [s.id for s in rest] == ["dana"]
    neighbours, rest = merge_choices(model, model.listening_turns[3])
    assert [s.id for s, _suffix in neighbours] == ["ben"]
    assert [s.id for s in rest] == ["dana", "cleo"]


def test_excerpt_has_five_previous_words_and_eight_current_words():
    model = meeting()
    assert excerpt(model, model.listening_turns[0]) == "one two three four five six seven"
    assert (
        excerpt(model, model.listening_turns[1])
        == '<span alpha="55%">three four five six seven │ </span>eight nine ten eleven twelve thirteen fourteen fifteen'
    )


def test_excerpt_escapes_transcript_markup_in_both_speakers():
    model = meeting()
    model.listening_turns[0].text = "<b>one & two</b>"
    model.listening_turns[1].text = '<span alpha="0">three & four</span>'
    assert excerpt(model, model.listening_turns[0]) == "&lt;b&gt;one &amp; two&lt;/b&gt;"
    assert excerpt(model, model.listening_turns[1]) == (
        '<span alpha="55%">&lt;b&gt;one &amp; two&lt;/b&gt; │ </span>'
        '&lt;span alpha="0"&gt;three &amp; four&lt;/span&gt;'
    )


def test_single_speaker_can_be_named_but_cannot_be_merged():
    controller = listening()
    controller.speaker_step_target = lambda *_: None
    state = bar_state(controller, ("ada",), editable=True, busy=False)
    assert state.edits_enabled and not state.merge_enabled


def test_model_import_and_decisions_work_when_gi_is_forbidden():
    script = """
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class NoToolkit(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "gi" or fullname.startswith("gi."):
            raise AssertionError("Bar decisions must not import GI")
sys.meta_path.insert(0, NoToolkit())
from cassini_talk.listening_bar_model import bar_layout
assert bar_layout(3, editable=False).menu_actions == ("previous-speaker", "next-speaker")
assert "gi" not in sys.modules
"""
    subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1] / "src")],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
