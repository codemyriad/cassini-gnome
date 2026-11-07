# SPDX-License-Identifier: GPL-3.0-or-later
"""D70 bar, using the real span controller and offline GStreamer player."""

from types import SimpleNamespace

import pytest
from test_listen import TURNS, player, until  # noqa: F401

from cassini_talk.listen import ListenController
from cassini_talk.listening_bar import ListeningBar, merge_choices


def meeting(editable=True):
    speakers = tuple(
        SimpleNamespace(id=s, name=s.title(), talk_ms=t) for s, t in (("ben", 9000), ("ada", 3000), ("cleo", 700))
    )
    return SimpleNamespace(speakers=speakers, listening_turns=TURNS, metadata_editable=editable)


def test_merge_menu_uses_current_turn_neighbours_and_deduplicates():
    neighbours, rest = merge_choices(meeting(), TURNS[1])
    assert [s.id for s, _suffix in neighbours] == ["cleo", "ben"]
    assert not rest
    neighbours, rest = merge_choices(meeting(), TURNS[3])
    assert [s.id for s, _suffix in neighbours] == ["ben"]
    assert [s.id for s in rest] == ["cleo"]


def test_cue_after_merge_never_starts_audio(player):  # noqa: F811
    controller = ListenController(lambda: player)
    controller.set_turns(TURNS)
    assert controller.start("ada", 1, play=False)
    assert controller.active and controller.index == 1
    assert not player.playing
    assert player.position == 5500
    assert controller.step_target(1) == 2
    assert controller.step_target(-1) == 0
    player.rendered.clear()
    player.play()
    until(lambda: controller.finished, "cue then play")
    assert player.position == 20000
    controller.stop()


def test_bar_endpoints_readonly_and_collapse(player):  # noqa: F811
    controller = ListenController(lambda: player)
    model = meeting(False)
    page = SimpleNamespace(
        window=SimpleNamespace(listen=controller),
        player=player,
        meeting=model,
        _speaker_future=None,
        _speaker_rows={},
        stop_listening=controller.stop,
        name_speakers=lambda *_a, **_kw: None,
    )
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("GTK display unavailable")
    bar = ListeningBar(page)
    controller.connect("changed", lambda *_: bar.update())
    controller.connect("stopped", lambda *_: bar.update())
    controller.set_turns(TURNS)
    assert controller.start("ada", 0, play=False)
    assert bar.get_reveal_child()
    assert bar.name.get_label() == "Ada"
    assert "1" in bar.turn.get_label()
    assert not bar.buttons["previousTurn"].get_sensitive()
    assert bar.buttons["nextTurn"].get_sensitive()
    bar._collapse(3)
    assert not bar.buttons["name"].get_visible()
    assert not bar.merge.get_visible()
    assert not bar.excerpt.get_visible()
    assert not bar.buttons["nextSpeaker"].get_visible()
    assert bar.buttons["nextTurn"].get_visible() and bar.stop.get_visible()
    assert not bar.actions.lookup_action("merge").get_enabled()
    assert bar.more_model.get_n_items() == 2
    controller.start("ada", 5, play=False)
    assert not bar.buttons["nextTurn"].get_sensitive()
    assert bar.buttons["previousTurn"].get_sensitive()
    player.play()
    until(lambda: controller.finished, "last turn")
    assert bar.buttons["again"].get_visible()
    controller.stop()
    assert not bar.get_reveal_child()


def test_dialog_sample_preserves_the_paused_cue_and_restores_its_position(player):  # noqa: F811
    controller = ListenController(lambda: player)
    controller.set_turns(TURNS)
    assert controller.start("ada", 1, play=False)
    saved = player.position
    assert controller.cued
    assert player.play_range(100, 800)
    until(lambda: not player.playing, "dialog sample end")
    assert controller.cued and controller.index == 1
    player.clear_range()
    player.seek(saved)
    assert not player.playing and player.position == saved
    assert controller.cued and controller.step_target(1) == 2
    controller.stop()
