# SPDX-License-Identifier: GPL-3.0-or-later
"""R64 lanes: the write-down and the D63 gap map reserve the same speaker ids.

Both read every track session.json or the file's x tracks list, readable,
unreadable or missing (source_ownership.speaker_lanes on library.track_rows).
"""

from dataclasses import replace

import pytest
from test_library import write_metadata
from test_turn_breaks import grouped, rejoined, stored_track, words_of

from cassini_talk.library import LibraryScanner, turn_gaps
from cassini_talk.recording_session import RecordingSession
from cassini_talk.search_index import search_turns
from cassini_talk.source_ownership import gap_ranges
from cassini_talk.transcription import prepare_tracks

# Source s: a sounds over [0, 1000); b overlaps it with a 300 ms gap at [300, 600).
A = ("a", "s", [(0, 16000)])
B = ("b", "s", [(200, 1600), (600, 1600)])
B_GAP = ((4800, 9600),)
ACROSS = [(200, 280), (620, 700)]


def recorded(tmp_path, tracks, *, missing=(), unreadable=()):
    """A scanned folder; `missing` track files are deleted, `unreadable` ones are not WAV."""
    session = RecordingSession(tmp_path, "Lanes")
    for identifier, source, _appends in tracks:
        session.add_track(identifier, identifier.upper(), "remote", 16000, 1, source_track_id=source)
    for identifier, _source, appends in tracks:
        for at, frames in appends:
            session.append(identifier, b"\x01\x00" * frames, at)
    session.finish()
    write_metadata(session.directory)
    for track in session.manifest["tracks"]:
        path = session.directory / track["file"]
        if track["id"] in missing:
            path.unlink()
        elif track["id"] in unreadable:
            path.write_bytes(b"not a wav")
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    return meeting


def same_lanes(meeting, silent=()):
    """prepare_tracks' speaker ids are turn_gaps' keys, each holding its own tracks' ranges."""
    jobs, _speakers = prepare_tracks(meeting, silent=lambda track: track.id in silent)
    metadata = dict(recordingDetails=dict(writingDown=dict(silentTracks=sorted(silent))))
    gaps = turn_gaps(meeting.tracks, metadata, meeting.extensions, (), meeting.listed_tracks)
    own = {}
    for job, track in zip(jobs, meeting.tracks, strict=True):
        own.setdefault(job["speaker"], []).extend(() if job.get("silent") else gap_ranges(track.gaps))
    assert {speaker: gaps.get(speaker) for speaker in own} == {s: tuple(sorted(r)) for s, r in own.items()}
    # The other entries are the ids of listed tracks without a job; they hold no ranges.
    assert not any(gaps[speaker] for speaker in gaps.keys() - own.keys())
    return {job["captureTrackId"]: job["speaker"] for job in jobs}, gaps


def every_grouper(speaker, gaps):
    """Turn sizes from build_turns, reading_turns, the Markdown export, a seam rejoin and Search turns."""
    words = words_of(speaker, ACROSS)
    search = [len(turn.words) for turn in search_turns(words, gaps)]
    return (*grouped(speaker, words, gaps), rejoined(speaker, words, gaps), search)


def lanes_meeting(tmp_path):
    """Codex #183: a missing track whose source id is s:overlap:1, beside two overlapping s tracks."""
    return recorded(tmp_path, [A, B, ("c", "s:overlap:1", [(0, 1600)])], missing={"c"})


def test_a_missing_tracks_id_moves_the_overlap_lane_in_the_write_down_and_the_gap_map(tmp_path):
    meeting = lanes_meeting(tmp_path)
    assert [track.id for track in meeting.tracks] == ["a", "b"]
    lanes, gaps = same_lanes(meeting)
    assert lanes == {"a": "track-s", "b": "track-s:overlap:2"}
    assert gaps == meeting.turn_gaps == {"track-s": (), "track-s:overlap:1": (), "track-s:overlap:2": B_GAP}
    # b's words split at its own gap in every grouper; the missing source's stay whole.
    assert every_grouper("track-s:overlap:2", gaps) == ([1, 1],) * 5
    assert every_grouper("track-s:overlap:1", gaps) == ([2],) * 5
    assert every_grouper("track-s", gaps) == ([2],) * 5


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_a_listed_track_this_reader_cannot_read_keeps_its_id(tmp_path, how):
    meeting = recorded(tmp_path, [A, B, ("c", "s:overlap:1", [(0, 1600)])], **{how: {"c"}})
    assert same_lanes(meeting)[0] == {"a": "track-s", "b": "track-s:overlap:2"}


def test_an_unreadable_x_row_keeps_its_id(tmp_path):
    meeting = recorded(tmp_path, [A, B])
    assert same_lanes(meeting)[0] == {"a": "track-s", "b": "track-s:overlap:1"}
    # A root file's working copy: the file lists a track the session dropped, in a row this reader cannot place.
    bad = dict(stored_track("c", gaps=[], startOffsetMs="0"), sourceTrackId="s:overlap:1")
    copy = replace(meeting, extensions={"codemyriad.io": {"tracks": [bad]}})
    lanes, gaps = same_lanes(copy)
    assert lanes == {"a": "track-s", "b": "track-s:overlap:2"}
    assert every_grouper("track-s:overlap:2", gaps) == ([1, 1],) * 5


def test_a_silent_track_keeps_its_id_and_takes_no_lane(tmp_path):
    # b is silent: the later overlapping d takes the first lane, whose id no listed track has.
    d = ("d", "s", [(250, 1600), (650, 1600)])
    e = ("e", "s:overlap:2", [(0, 1600)])
    meeting = recorded(tmp_path, [A, B, d, e])
    lanes, gaps = same_lanes(meeting, silent={"b"})
    assert lanes == {"a": "track-s", "b": "track-s", "d": "track-s:overlap:1", "e": "track-s:overlap:2"}
    assert gaps["track-s:overlap:1"] == ((5600, 10400),) and gaps["track-s"] == ()
    assert same_lanes(meeting)[0] == {
        "a": "track-s",
        "b": "track-s:overlap:1",
        "d": "track-s:overlap:3",
        "e": "track-s:overlap:2",
    }


def test_voice_suffixed_source_ids_and_their_lanes_agree(tmp_path):
    # Sources s and s~1, each with two overlapping tracks; a missing s~1:overlap:1 track.
    tilde = [("t1", "s~1", [(0, 16000)]), ("t2", "s~1", [(200, 1600), (600, 1600)])]
    meeting = recorded(tmp_path, [A, B, *tilde, ("m", "s~1:overlap:1", [(0, 1600)])], missing={"m"})
    lanes, gaps = same_lanes(meeting)
    assert lanes == {"a": "track-s", "b": "track-s:overlap:1", "t1": "track-s~1", "t2": "track-s~1:overlap:2"}
    assert gaps["track-s~1:overlap:1"] == () and gaps["track-s~1:overlap:2"] == B_GAP
    for speaker in ("track-s~1:overlap:2", "track-s:overlap:1"):
        assert every_grouper(speaker, gaps) == ([1, 1],) * 5, speaker
    # A D24 voice of the missing source's id keeps that id's empty entry, never a lane's ranges.
    assert every_grouper("track-s~1:overlap:1~1", gaps) == ([2],) * 5
