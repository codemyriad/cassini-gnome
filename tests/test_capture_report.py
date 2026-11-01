# SPDX-License-Identifier: GPL-3.0-or-later
"""Source accounting survives actual publication and cache-free file reading."""

import base64
import hashlib
import json
import shutil

import pytest

from cassini_talk.capture_report import from_session, record_capture, stored
from cassini_talk.library import LibraryScanner
from cassini_talk.library_publication import capture_parent, publish_stopped
from cassini_talk.meeting_store import read_metadata, update_metadata
from cassini_talk.recording_session import BrowserHost


def captured(root, *, gap=False):
    host = BrowserHost(capture_parent(root))
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="synthetic"))
    host.handle(
        dict(type="sources", discoveredSources=[dict(sourceTrackId="missing", kind="remote", label="Missing voice")])
    )
    host.handle(
        dict(
            type="track",
            id="zeros",
            sourceTrackId="zeros",
            kind="remote",
            label="Quiet voice",
            sampleRate=48000,
            channels=1,
        )
    )
    host.handle(dict(type="audio", id="zeros", sequence=0, offsetMs=0, pcm=base64.b64encode(bytes(96000)).decode()))
    if gap:
        host.handle(
            dict(type="audio", id="zeros", sequence=1, offsetMs=1500, pcm=base64.b64encode(bytes(9600)).decode())
        )
    host.handle(dict(type="stop", captureId="synthetic", reason="user"))
    return host.session


def test_capture_record_survives_portable_publication_and_cache_removal(tmp_path):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Real publication requires GStreamer")
    root = tmp_path / "Library"
    session = captured(root)
    pcm = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.glob("*.wav")}
    published = publish_stopped(session)
    (meeting,) = LibraryScanner(root).scan().meetings
    record = stored(meeting.metadata)
    assert {s["sourceTrackId"]: s["captureOutcome"] for s in record["sources"]} == {
        "missing": "neverAudio",
        "zeros": "captured",
    }
    assert record["events"][-1]["reason"] == "user"
    assert record["tracks"][0]["receivedFrames"] == 48000
    assert record["problemKind"] == "capture"
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.glob("*.wav")} == pcm
    shutil.rmtree(session.directory)
    (cache_free,) = LibraryScanner(root).scan().meetings
    assert cache_free.audio_file.name == published.name
    assert stored(cache_free.metadata) == record


def test_record_is_idempotent_and_preserves_names_and_future_fields(tmp_path):
    session = captured(tmp_path / "Library")
    path = session.directory
    update_metadata(
        path,
        title="User title",
        recordingDetails=dict(
            future="kept",
            captureRecord=dict(
                version=1,
                future="kept",
                sources=[dict(sourceTrackId="zeros", future="kept", captureExpected=False)],
                tracks=[],
            ),
        ),
    )
    assert record_capture(path, session.manifest)
    current = read_metadata(path)
    assert current["title"] == "User title" and current["recordingDetails"]["future"] == "kept"
    record = stored(current)
    assert record["future"] == "kept"
    assert next(s for s in record["sources"] if s["sourceTrackId"] == "zeros")["future"] == "kept"
    assert next(s for s in record["sources"] if s["sourceTrackId"] == "zeros").get("captureExpected", True) is True
    before = (path / "meeting.json").read_bytes()
    assert not record_capture(path, session.manifest)
    assert (path / "meeting.json").read_bytes() == before
    update_metadata(path, recordingDetails=dict(captureRecord=dict(version=2, future="kept")))
    before = (path / "meeting.json").read_bytes()
    assert not record_capture(path, session.manifest)
    assert (path / "meeting.json").read_bytes() == before
    assert stored(read_metadata(path)) is None


def test_no_record_is_unknown_and_never_synthesizes_sources():
    assert from_session(dict(tracks=[])) is None
    assert stored(dict(recordingDetails={})) is None
    assert json.dumps(from_session(dict(captureOutcome=dict(sources=[]), tracks=[])))


def test_untrusted_record_shape_is_unknown_without_inventing_success(tmp_path):
    session = captured(tmp_path / "Library")
    valid = from_session(session.manifest)
    assert stored(dict(recordingDetails=dict(captureRecord=valid))) == valid
    import copy

    for mutation in ("version", "id", "duration", "huge-duration", "surrogate", "trackIds", "label"):
        value = copy.deepcopy(valid)
        if mutation == "version":
            value["version"] = True
        elif mutation == "id":
            value["sources"][0]["sourceTrackId"] = "é" * 101
        elif mutation == "duration":
            value["sources"][0]["audioMs"] = float("nan")
        elif mutation == "huge-duration":
            value["sources"][0]["audioMs"] = 10**1000
        elif mutation == "surrogate":
            value["sources"][0]["sourceTrackId"] = "\ud800"
        elif mutation == "trackIds":
            value["sources"][0]["trackIds"] = [dict(bad=True)]
        else:
            value["sources"][0]["label"] = None
        assert stored(dict(recordingDetails=dict(captureRecord=value))) is None


def test_seventy_announced_sources_are_not_pruned_by_portable_publication(tmp_path):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Real publication requires GStreamer")
    root = tmp_path / "Library"
    host = BrowserHost(capture_parent(root))
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="synthetic"))
    sources = [dict(sourceTrackId=f"root-{i}", kind="remote", label=f"Voice {i}") for i in range(70)]
    host.handle(dict(type="sources", discoveredSources=sources))
    host.handle(dict(type="track", id="only-wav", sourceTrackId="root-0", kind="remote", sampleRate=48000, channels=1))
    host.handle(dict(type="audio", id="only-wav", sequence=0, offsetMs=0, pcm=base64.b64encode(bytes(96000)).decode()))
    host.handle(dict(type="stop", captureId="synthetic"))
    publish_stopped(host.session)
    (meeting,) = LibraryScanner(root).scan().meetings
    record = stored(meeting.metadata)
    assert len(record["sources"]) == 70 and len(record["tracks"]) == 1
    assert sum(row["captureOutcome"] == "neverAudio" for row in record["sources"]) == 69
    assert len(list(host.session.directory.glob("*.wav"))) == 1


def test_recording_details_keep_gaps_and_end_after_session_cache_is_deleted(tmp_path):
    pytest.importorskip("numpy")
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        pytest.skip("Real publication requires GStreamer")
    from cassini_talk.capture_details import read_details
    from cassini_talk.strings import text

    root = tmp_path / "Library"
    session = captured(root, gap=True)
    publish_stopped(session)
    (meeting,) = LibraryScanner(root).scan().meetings
    report = read_details(meeting)
    gaps = dict(report.health)[text("details.health.gaps")]
    assert "24,000 frames" in gaps and "0.5 s" in gaps
    assert report.ended.startswith("Stopped by you")
    shutil.rmtree(session.directory)
    (meeting,) = LibraryScanner(root).scan().meetings
    assert not (meeting.directory / "session.json").exists()
    after = read_details(meeting)
    assert dict(after.health)[text("details.health.gaps")] == gaps
    assert after.ended == report.ended and after.spans == report.spans
    assert after.folder == report.folder == str(root)
    assert not any("session.json" in note for note in after.notes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("frames", True),
        ("frames", -1),
        ("sampleRate", 0),
        ("startOffsetMs", float("nan")),
        ("kind", []),
        ("id", None),
    ],
)
def test_file_only_details_ignore_invalid_track_facts_without_opening_audio(tmp_path, field, value):
    from dataclasses import replace

    from cassini_talk.capture_details import read_details

    session = captured(tmp_path / "Library")
    (meeting,) = LibraryScanner(tmp_path / "Library").scan().meetings
    record = from_session(session.manifest)
    record["tracks"][0][field] = value
    meeting = replace(meeting, tracks=(), metadata=dict(meeting.metadata, recordingDetails=dict(captureRecord=record)))
    shutil.rmtree(session.directory)
    report = read_details(meeting)
    assert report.spans == ()
    assert report.ended.startswith("Stopped by you")
    assert not meeting.directory.exists()  # Reporting never materializes audio/cache paths.
