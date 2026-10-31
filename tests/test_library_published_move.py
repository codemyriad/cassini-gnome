# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - fixture dependencies precede encoder imports.
"""Draft safety: D15 must not retire routing after moving only a file's cache.

R59 is unresolved. These guards retain the original Library/pending source;
paired publication and its crash matrix replace the guards before readiness.
"""

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication requires GStreamer", allow_module_level=True)

from library_v2_support import simple_session

from cassini_viewer import library_location as location
from cassini_viewer.library import LibraryCollectionScanner
from cassini_viewer.library_v2 import finalize


def setup(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(old))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    sid, directory = simple_session(old, 0.1)
    return old, new, sid, directory


def test_move_never_retires_a_published_file_by_moving_just_its_cache(tmp_path, monkeypatch):
    old, new, sid, directory = setup(tmp_path, monkeypatch)
    result = finalize.finalize(old, sid)
    file = old / result.name
    before = file.read_bytes()
    with pytest.raises(ValueError, match="paired move"):
        location.request(new)
    assert location.primary() == old and directory.exists() and not new.exists()
    (meeting,) = LibraryCollectionScanner().scan().meetings
    assert meeting.audio_file == file and not meeting.published_missing
    assert file.read_bytes() == before


def test_stop_after_move_keeps_the_published_source_routed_until_pair_can_move(tmp_path, monkeypatch):
    old, new, sid, directory = setup(tmp_path, monkeypatch)
    location.request(new)  # A recording/saving meeting was already deferred here.
    result = finalize.finalize(old, sid)
    state = location.process()
    assert len(state["pending"]) == 1 and state["pending"][0]["state"] == "failed"
    assert location.primary() == new and directory.exists()
    (meeting,) = LibraryCollectionScanner().scan().meetings
    assert meeting.audio_file == old / result.name and not meeting.published_missing
    assert not (new / ".cassini/sessions" / directory.name).exists()
