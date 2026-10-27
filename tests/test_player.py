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
    assert player._pipeline.get_property("uri") is None
    player.stop()
