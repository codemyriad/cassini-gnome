# SPDX-License-Identifier: GPL-3.0-or-later
import json
from concurrent.futures import CancelledError

import pytest
from test_library import capture

from cassini_viewer import transcript_publish
from cassini_viewer.meeting_store import update_metadata
from cassini_viewer.transcription import update_progress_metadata


def test_long_transcript_has_bounded_cumulative_snapshot_writes_and_stable_order(tmp_path, monkeypatch):
    written = []
    atomic = transcript_publish.atomic_json

    def observe(path, value):
        written.append(len(value["items"]))
        atomic(path, value)

    monkeypatch.setattr(transcript_publish, "atomic_json", observe)
    now = [0]
    publisher = transcript_publish.TranscriptPublisher(tmp_path, clock=lambda: now[0])
    for segment in range(1000):
        now[0] += 2
        words = [
            dict(startMs=(segment * 20 + index) * 100, endMs=(segment * 20 + index) * 100 + 90, text="word")
            for index in range(20)
        ]
        publisher.add(words, "me", 0, "en")
        publisher.flush()
    publisher.flush(force=True, complete=True)
    assert written[-1] == 20000 and sum(written) < 12 * 20000
    assert len(written) < 80, written
    items = json.loads((tmp_path / "transcript.json").read_text())["items"]
    assert len(items) == 20000 and items[-1]["endMs"] == 1999990
    assert all("_order" not in word for word in items)


def test_resuming_retains_longer_partial_until_caught_up_and_empty_completion_is_valid(tmp_path):
    partial = b'{"format":"cassini.words.v1","language":"en","items":[]}'
    path = tmp_path / "transcript.json"
    path.write_bytes(partial)
    publisher = transcript_publish.TranscriptPublisher(tmp_path, existing_count=100)
    publisher.add([dict(startMs=0, endMs=1, text="Only recomputed prefix")], "me", 0, "en")
    assert not publisher.flush(force=True) and path.read_bytes() == partial
    empty = transcript_publish.TranscriptPublisher(tmp_path, existing_count=100)
    assert empty.flush(force=True, complete=True)
    assert json.loads(path.read_text())["items"] == []


def test_repeated_eta_and_speakers_do_not_rewrite_metadata(tmp_path):
    session = capture(tmp_path)
    update_metadata(session.directory, transcript=dict(status="running", reference=None))
    speakers = dict(me=dict(id="me", name="Me", provenance="source", colourIndex=0))
    assert update_progress_metadata(session.directory, speakers, etaMinutes=1)
    path = session.directory / "meeting.json"
    before = path.read_bytes(), path.stat().st_mtime_ns
    for _ in range(100):
        assert not update_progress_metadata(session.directory, speakers, etaMinutes=1)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_cancel_during_serialization_keeps_the_published_snapshot(tmp_path, monkeypatch):
    from cassini_viewer import meeting_store

    path = tmp_path / "transcript.json"
    before = b'{"format":"cassini.words.v1","items":[]}\n'
    path.write_bytes(before)
    closed = False

    def check_active():
        if closed:
            raise CancelledError()

    dump = meeting_store.json.dump

    def serialize_then_close(*args, **kwargs):
        nonlocal closed
        dump(*args, **kwargs)
        closed = True

    monkeypatch.setattr(meeting_store.json, "dump", serialize_then_close)
    publisher = transcript_publish.TranscriptPublisher(tmp_path, check_active=check_active)
    publisher.add([dict(text="Late", startMs=0, endMs=1)], "others", 0, "en")
    with pytest.raises(CancelledError):
        publisher.flush(force=True, complete=True)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".transcript.json-*"))


@pytest.mark.parametrize(("identifier", "default_name"), [("me", "Me"), ("others", "The others")])
def test_progress_and_completion_keep_a_source_speaker_explicitly_unnamed(tmp_path, identifier, default_name):
    from cassini_viewer.meeting_store import read_metadata

    session = capture(tmp_path)
    cleared = dict(id=identifier, name=None, provenance="unnamed", colourIndex=0)
    update_metadata(session.directory, speakers=[cleared], transcript=dict(status="running", reference=None))
    planned = {identifier: dict(cleared, name=default_name, provenance="source")}
    assert update_progress_metadata(session.directory, planned, etaMinutes=1)
    assert read_metadata(session.directory)["speakers"] == [cleared]
    assert update_progress_metadata(
        session.directory, planned, transcript=dict(status="done", reference=None), etaMinutes=None
    )
    assert read_metadata(session.directory)["speakers"] == [cleared]
