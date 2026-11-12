# SPDX-License-Identifier: GPL-3.0-or-later
"""Synchronous real-widget Search checks, run in Boards' private display."""

from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace

from gi.repository import Gio, Gtk

from cassini_talk.library import CaptureTrack, Meeting
from cassini_talk.search_index import SearchHit
from cassini_talk.search_page import HitItem, SearchPage


def check_playability_bindings():
    track = CaptureTrack(Path("/fixture/track.wav"), 48000, 48000, 0, True)
    meeting = Meeting(
        id="capture",
        directory=Path("/fixture/Recording-capture"),
        title="Synthetic English meeting",
        started_at_ms=None,
        duration_ms=1000,
        has_audio=True,
        tracks=(track,),
        lifecycle="recording",
    )
    hit = SearchHit(meeting.key, -1, "", 0, "", "", "", title_markup="Synthetic English meeting")
    model = Gio.ListStore.new(HitItem)
    model.append(HitItem(hit, meeting))
    selection = Gtk.SingleSelection.new(model)
    app = SimpleNamespace(voice_store=SimpleNamespace(match_signature=lambda: ()))
    window = SimpleNamespace(now_ms=1793107080000, tz="UTC", get_application=lambda: app, _stop_playback=lambda: None)
    page = SimpleNamespace(
        selection=selection,
        hit=None,
        _meetings={meeting.key: meeting},
        _turns={meeting.key: ()},
        window=window,
        _context_signature=None,
        context=Gtk.Box(),
        play_from=lambda: None,
        open_meeting=lambda: None,
    )
    page._selected = MethodType(SearchPage._selected, page)
    states = [
        (meeting, False),
        (replace(meeting, lifecycle="saving"), False),
        (replace(meeting, lifecycle="saved", tracks=(replace(track, frames=0),)), False),
        (replace(meeting, lifecycle="saved", tracks=(replace(track, start_offset_ms=None),)), False),
        (replace(meeting, lifecycle="saved"), True),
        (replace(meeting, lifecycle="saved", tracks=(replace(track, start_offset_ms=None),)), False),
        (replace(meeting, audio_file=Path("/fixture/published.opus")), True),
        (replace(meeting, audio_file=Path("/fixture/published.opus"), has_audio=False), False),
    ]
    for current, expected in states:
        page._meetings[meeting.key] = current
        page._selected()
        assert page.play.get_sensitive() is expected, (current.lifecycle, current.tracks, current.audio_file)
        previous = page.play
        page._selected()
        assert page.play is previous, "Unchanged context must preserve focused controls"
    print("PASS: title-only Search playability and context refresh use real GTK buttons")
