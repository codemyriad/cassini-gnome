# SPDX-License-Identifier: GPL-3.0-or-later

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

from cassini_viewer.player import Player  # noqa: E402


def test_missing_element_is_named_instead_of_playing(monkeypatch):
    if Gst.ElementFactory.find("playbin") is None:
        pytest.skip("GStreamer playbin unavailable")
    player = Player()
    errors = []
    player.connect("error", lambda _player, text: errors.append(text))
    find = Gst.ElementFactory.find
    monkeypatch.setattr(Gst.ElementFactory, "find", lambda name: None if name == "wavparse" else find(name))
    player.load("file:///nonexistent/track-1.wav")
    assert len(errors) == 1
    assert errors[0].startswith("Missing GStreamer elements: wavparse. Install the GStreamer base and good plugins")
    assert not player._pipeline.get_property("uri")
    player.stop()


def needs_playbin():
    if Gst.ElementFactory.find("playbin") is None:
        pytest.skip("GStreamer playbin unavailable")


def test_failed_load_forgets_the_previous_file_and_play_refuses(monkeypatch):
    needs_playbin()
    player = Player()
    errors = []
    player.connect("error", lambda _player, text: errors.append(text))
    player.load("file:///nonexistent/earlier.opus", 5000)
    assert player.loaded
    find = Gst.ElementFactory.find
    monkeypatch.setattr(Gst.ElementFactory, "find", lambda name: None if name == "wavparse" else find(name))
    player.load("file:///nonexistent/track-1.wav", 7000)
    assert len(errors) == 1
    assert not player.loaded
    assert not player._pipeline.get_property("uri")
    assert player.duration == 0
    assert player._pipeline.get_state(0)[1] == Gst.State.NULL

    player.play()
    assert errors == [errors[0], errors[0]]
    assert not player.playing
    assert player._tick_id == 0
    assert player._pipeline.get_state(0)[1] == Gst.State.NULL
    player.toggle()
    assert len(errors) == 3
    assert not player.playing
    player.shutdown()


def test_play_before_any_load_reports_an_error():
    needs_playbin()
    player = Player()
    errors = []
    player.connect("error", lambda _player, text: errors.append(text))
    player.play()
    assert len(errors) == 1
    assert not player.playing
    assert player._tick_id == 0
    player.shutdown()


def test_play_state_change_failure_is_an_error_not_a_phantom_playing_state():
    needs_playbin()
    player = Player()
    errors = []
    player.connect("error", lambda _player, text: errors.append(text))
    # The source cannot open a missing file, so the pipeline refuses to start.
    player.load("file:///nonexistent/missing.opus")
    errors.clear()
    player.play()
    assert len(errors) == 1
    assert errors[0]
    assert not player.playing
    assert player._tick_id == 0
    assert player._pipeline.get_state(0)[1] == Gst.State.NULL
    player.shutdown()
