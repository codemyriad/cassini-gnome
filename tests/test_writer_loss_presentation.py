# SPDX-License-Identifier: GPL-3.0-or-later
"""D69.2: computer-side (writer) loss is kept in the file but shown quietly.

Real sessions written by RecordingSession, scanned by the Library and read
by Recording details. The file keeps D69 as written (problemKind io, end
reason error, the stored sentence); only the presentation is calm.
"""

import pytest

from cassini_talk.capture_details import read_details
from cassini_talk.library import scan_library
from cassini_talk.recording_session import RecordingSession
from cassini_talk.strings import text
from cassini_talk.ux_formats import details_duration, position

RATE = 48000
STORED = "because the computer could not keep up"


def record(tmp_path, *spans, input_gap_frames=0):
    """One microphone source; each span loses `writer` frames to the writer."""
    session = RecordingSession(tmp_path, "Weekly sync")
    offset = 0
    for index, writer in enumerate(spans):
        identifier = "microphone" if index == 0 else f"microphone-span-{index}"
        filename = "track-microphone.wav" if index == 0 else None
        session.add_track(identifier, "Me", "microphone", RATE, 1, filename=filename)
        session.append_native(identifier, b"\x01\x00" * RATE * 2, offset)
        if writer:
            session.pad_native(identifier, writer_frames=writer)
        if index == 0 and input_gap_frames:
            session.pad_native(identifier, gap_frames=input_gap_frames)
        if index < len(spans) - 1:
            session.end_track(identifier)
        offset += 5000
    session.finish(reason="user", at_ms=offset)
    assert session.manifest["problemKind"] == "io" and STORED in session.manifest["problem"]
    assert session.manifest["events"][-1]["reason"] == "error", "the file keeps D69 as written"
    (meeting,) = scan_library(tmp_path).meetings
    return session, meeting


def busy_line(milliseconds):
    return text(
        "details.sources.computerBusy", source=text("common.yourMicrophone"), duration=details_duration(milliseconds)
    )


def test_writer_only_loss_shows_as_a_normal_recording(tmp_path):
    session, meeting = record(tmp_path, RATE)  # 1 s of writer loss
    assert meeting.interrupted_at_ms is None
    assert meeting.badge() is None, "no Interrupted badge, no amber"
    report = read_details(meeting)
    assert text("details.health.problem") not in dict(report.health), "no reported issue"
    end = session.manifest["events"][-1]["atMs"]
    assert report.ended == position(int(end)), "no error end, only the time"
    assert report.computer_busy == (busy_line(1000),)
    assert STORED not in report.plain_text(), "the stored sentence is never shown"
    assert busy_line(1000) in report.plain_text()


@pytest.mark.parametrize("frames,shown", [(RATE * 999 // 1000, False), (RATE, True), (RATE * 3 // 2, True)])
def test_the_line_appears_from_one_second_of_a_sources_summed_loss(tmp_path, frames, shown):
    _session, meeting = record(tmp_path, frames)
    assert meeting.badge() is None
    milliseconds = frames * 1000 // RATE
    assert read_details(meeting).computer_busy == ((busy_line(milliseconds),) if shown else ())


def test_two_spans_of_one_source_are_summed_before_the_threshold(tmp_path):
    # 600 ms + 600 ms in two spans of the microphone (turned off and on again).
    _session, meeting = record(tmp_path, RATE * 6 // 10, RATE * 6 // 10)
    assert read_details(meeting).computer_busy == (busy_line(1200),)


def test_writer_loss_with_a_real_gap_still_shows_the_gap(tmp_path):
    _session, meeting = record(tmp_path, RATE, input_gap_frames=RATE)  # and a 1 s input gap
    assert meeting.interrupted_at_ms is not None
    assert meeting.badge().kind == "interrupted", "the gap is presented as today"
    report = read_details(meeting)
    assert dict(report.health)[text("details.health.problem")] == "io"
    assert report.ended != position(int(_session.manifest["events"][-1]["atMs"]))
    assert report.computer_busy == (busy_line(1000),)


def test_a_short_input_gap_does_not_turn_writer_loss_into_an_interruption(tmp_path):
    _session, meeting = record(tmp_path, RATE, input_gap_frames=RATE // 2)
    assert meeting.badge() is None
