# SPDX-License-Identifier: GPL-3.0-or-later
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
from cassini_viewer.native_recorder import space_left  # noqa: E402
from cassini_viewer.recording_session import ProtocolError, RecordingSession  # noqa: E402


def test_space_estimate_keeps_larger_of_gib_or_two_percent_reserve(tmp_path):
    gib = 1024**3

    def stats(total, free):
        return lambda _: SimpleNamespace(f_frsize=1, f_blocks=total, f_bavail=free)

    assert space_left(tmp_path, 1024, stats(10 * gib, gib + 1024 * 60 * 29)) == (29, False)
    assert space_left(tmp_path, 1024, stats(100 * gib, 2 * gib)) == (0, True)
    assert space_left(tmp_path, 1024, stats(10 * gib, gib - 1)) == (0, True)


def test_native_named_tracks_remain_protocol_compatible_and_checkpointed(tmp_path):
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("microphone", "Me", "microphone", 48000, 1, filename="track-microphone.wav")
    session.append("microphone", bytes(960), 25)
    session.finish()
    assert (session.directory / "track-microphone.wav").stat().st_size == 1004
    assert session.manifest["tracks"][0]["startOffsetMs"] == 25
    assert session.manifest["tracks"][0]["ended"] is True
    with pytest.raises(ProtocolError):
        other = RecordingSession(tmp_path, source="gnome")
        other.add_track("system", None, "system", 48000, 1, filename="../outside.wav")
