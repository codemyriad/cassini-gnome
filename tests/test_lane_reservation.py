# SPDX-License-Identifier: GPL-3.0-or-later
"""R64 lanes: the write-down and the D63 gap map reserve the same speaker ids.

Both read every track session.json or the file's x tracks list, readable,
unreadable or missing (source_ownership.speaker_lanes on library.track_rows).
A session.json track keeps its place in the lanes when its file goes missing.
"""

import json
import wave
from dataclasses import replace

import pytest
from test_library import write_metadata
from test_turn_breaks import grouped, rejoined, stored_track, words_of

from cassini_talk.library import LibraryScanner, turn_gaps
from cassini_talk.library_v2.mixdown import session_spans
from cassini_talk.recording_session import RecordingSession
from cassini_talk.search_index import search_turns
from cassini_talk.source_ownership import gap_ranges
from cassini_talk.transcription import REFERENCE, prepare_tracks

# Source s: a sounds over [0, 1000); b overlaps it with a 300 ms gap at [300, 600).
A = ("a", "s", [(0, 16000)])
B = ("b", "s", [(200, 1600), (600, 1600)])
B_GAP = ((4800, 9600),)
ACROSS = [(200, 280), (620, 700)]
# c overlaps both from 100 ms, with a 200 ms gap of its own at [700, 900).
C = ("c", "s", [(100, 9600), (900, 1600)])
C_GAP = ((11200, 14400),)
ACROSS_C = [(650, 690), (950, 990)]


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


def same_lanes(meeting, silent=(), others=None):
    """prepare_tracks' speaker ids are turn_gaps' keys, each holding its own tracks' ranges."""
    jobs, _speakers = prepare_tracks(meeting, silent=lambda track: track.id in silent)
    metadata = dict(recordingDetails=dict(writingDown=dict(silentTracks=sorted(silent))))
    gaps = turn_gaps(meeting.tracks, metadata, meeting.extensions, (), meeting.lane_rows)
    own = {}
    for job, track in zip(jobs, meeting.tracks, strict=True):
        own.setdefault(job["speaker"], []).extend(() if job.get("silent") else gap_ranges(track.gaps))
    assert {speaker: gaps.get(speaker) for speaker in own} == {s: tuple(sorted(r)) for s, r in own.items()}
    # The other entries are the ids of listed tracks without a job; only a missing one's stored gaps fill one.
    assert {speaker: gaps[speaker] for speaker in gaps.keys() - own.keys() if gaps[speaker]} == (others or {})
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


def published_x(meeting):
    """The file's x["codemyriad.io"] as publishing this folder writes it (library_v2.mixdown)."""
    manifest = json.loads((meeting.directory / "session.json").read_text())
    return {"codemyriad.io": {"tracks": session_spans(meeting.directory, manifest).tracks, "meeting": {}}}


def test_a_lane_track_that_goes_missing_after_the_write_down_moves_no_lane(tmp_path):
    meeting = recorded(tmp_path, [A, C, B])
    lanes, gaps = same_lanes(meeting)
    assert lanes == {"a": "track-s", "c": "track-s:overlap:1", "b": "track-s:overlap:2"}
    assert gaps == {"track-s": (), "track-s:overlap:1": C_GAP, "track-s:overlap:2": B_GAP}
    x = published_x(meeting)
    meeting.tracks[1].path.unlink()
    (after,) = LibraryScanner(tmp_path).scan().meetings
    assert [track.id for track in after.tracks] == ["a", "b"]
    # c's stored words keep its own gap and b's theirs; writing down again gives b the same id.
    assert after.turn_gaps == gaps
    assert same_lanes(after, others={"track-s:overlap:1": C_GAP})[0] == {"a": "track-s", "b": "track-s:overlap:2"}
    assert every_grouper("track-s:overlap:2", after.turn_gaps) == ([1, 1],) * 5
    assert grouped("track-s:overlap:1", words_of("track-s:overlap:1", ACROSS_C), gaps) == ([1, 1],) * 3
    # The window and an opened file read the file's x tracks: the same map as the Library's,
    # so b's words across c's gap stay one turn there too.
    window = turn_gaps((), {}, x)
    assert window == after.turn_gaps
    assert grouped("track-s:overlap:2", words_of("track-s:overlap:2", ACROSS_C), window) == ([2],) * 3


def test_a_missing_track_written_down_without_its_file_keeps_the_files_lanes(tmp_path):
    # c was gone before the first write-down; the file publishing it later lists c with its frames.
    meeting = recorded(tmp_path, [A, C, B])
    x = published_x(meeting)
    meeting.tracks[1].path.unlink()
    (after,) = LibraryScanner(tmp_path).scan().meetings
    lanes, gaps = same_lanes(replace(after, extensions=x), others={"track-s:overlap:1": C_GAP})
    assert lanes == {"a": "track-s", "b": "track-s:overlap:2"}
    assert turn_gaps((), {}, x) == gaps == after.turn_gaps


def test_a_running_write_down_gives_no_gap_breaks_until_its_silent_tracks_are_written(tmp_path):
    # b is silent, so d takes the first lane; meeting.json lists b only once the write-down is done.
    d = ("d", "s", [(250, 1600), (650, 1600)])
    meeting = recorded(tmp_path, [A, B, d])
    lanes, done = same_lanes(meeting, silent={"b"})
    assert lanes["d"] == "track-s:overlap:1" and done["track-s:overlap:1"] == ((5600, 10400),)
    for status in ("queued", "running"):
        write_metadata(meeting.directory, transcript=dict(status=status, reference=REFERENCE))
        (live,) = LibraryScanner(tmp_path).scan().meetings
        # Never b's gap on d's words: no breaks at all.
        assert live.turn_gaps == {}, status
        assert every_grouper("track-s:overlap:1", live.turn_gaps) == ([2],) * 5, status
    writing = dict(
        wallDurationMs=1,
        resumed=False,
        speakerSeparation=dict(status="skipped", reason="notApplicable"),
        silentTracks=["b"],
    )
    write_metadata(
        meeting.directory,
        transcript=dict(status="done", reference=REFERENCE),
        recordingDetails=dict(writingDown=writing),
    )
    (finished,) = LibraryScanner(tmp_path).scan().meetings
    assert finished.turn_gaps == done


def test_the_files_x_tracks_drop_an_empty_span_without_an_offset_as_the_scanner_does(tmp_path):
    meeting = recorded(tmp_path, [A, B])
    path = meeting.directory / "session.json"
    manifest = json.loads(path.read_text())
    a = manifest["tracks"][0]
    with wave.open(str(meeting.directory / "track-empty.wav"), "wb") as empty:
        empty.setnchannels(1)
        empty.setsampwidth(2)
        empty.setframerate(16000)
    a["spans"] = [{key: a.pop(key) for key in ("file", "sampleRate", "channels", "frames", "startOffsetMs")}]
    a["spans"].append(dict(file="track-empty.wav", sampleRate=16000, channels=1, frames=0))
    path.write_text(json.dumps(manifest))
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    lanes, gaps = same_lanes(meeting)
    assert lanes == {"a": "track-s", "b": "track-s:overlap:1"} and gaps["track-s:overlap:1"] == B_GAP
    assert turn_gaps((), {}, published_x(meeting)) == gaps


def test_a_span_that_goes_missing_keeps_its_tracks_lane_extent(tmp_path):
    # c sounds over [100, 700) and, in a second span, [900, 1000); d at [800, 900) fits only b's lane.
    c, e, d = ("c", "s", [(100, 9600)]), ("e", "s", [(900, 1600)]), ("d", "s", [(800, 1600)])
    meeting = recorded(tmp_path, [A, c, B, e, d])
    path = meeting.directory / "session.json"
    manifest = json.loads(path.read_text())
    keys = ("file", "sampleRate", "channels", "frames", "startOffsetMs")
    rows = {track["id"]: track for track in manifest["tracks"]}
    rows["c"]["spans"] = [{key: rows[name].pop(key) for key in keys} for name in ("c", "e")]
    manifest["tracks"].remove(rows["e"])
    path.write_text(json.dumps(manifest))
    (meeting,) = LibraryScanner(tmp_path).scan().meetings
    expected = {"a": "track-s", "c": "track-s:overlap:1", "b": "track-s:overlap:2", "d": "track-s:overlap:2"}
    assert same_lanes(meeting)[0] == expected
    meeting.tracks[1].spans[1].path.unlink()
    (after,) = LibraryScanner(tmp_path).scan().meetings
    assert len(after.tracks[1].spans) <= 1
    assert same_lanes(after)[0] == expected
