# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import replace
from pathlib import Path

import pytest

from cassini_talk.library import Meeting

BASE = Meeting(
    id="00000000-0000-4000-8000-000000000001",
    directory=Path("/unused"),
    title="Meeting",
    started_at_ms=0,
    duration_ms=2820000,
    quote_line="“A quote.”",
)


@pytest.mark.parametrize("source", ["chromium", "chrome", "firefox", "browser", "browser-host"])
def test_browser_capture_has_listening_meta_and_capture_path(source):
    meeting = replace(BASE, lifecycle="recording", capture_source=source)
    assert meeting.row_texts() == ("Listening inside your browser", "Your microphone and the meeting in your browser")


@pytest.mark.parametrize("phase", ["recording", "saving"])
def test_other_captures_and_saving_have_no_row_texts(phase):
    assert replace(BASE, lifecycle=phase, capture_source="gnome").row_texts() == (None, None)


def test_transcribing_has_no_meta_and_omits_line_for_unknown_duration():
    meeting = replace(BASE, transcript="running")
    assert meeting.row_texts() == (None, "47 min · the audio is ready to play")
    assert replace(meeting, duration_ms=None).row_texts() == (None, None)


def test_other_states_keep_regular_meta_and_quote():
    assert BASE.row_texts() == ("47 min", "“A quote.”")
