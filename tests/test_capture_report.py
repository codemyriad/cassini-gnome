# SPDX-License-Identifier: GPL-3.0-or-later
"""Source accounting survives actual publication and cache-free file reading."""

import base64
import hashlib
import importlib.util
import json
import locale
import shutil
from pathlib import Path

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
    from cassini_talk.capture_details import read_details

    before = read_details(meeting)
    assert len(before.sources) == 70
    assert all(row.line in before.plain_text() for row in before.sources)
    shutil.rmtree(host.session.directory)
    after = read_details(LibraryScanner(root).scan().meetings[0])
    assert after.sources == before.sources
    assert all(row.line in after.plain_text() for row in after.sources)


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
    assert locale.localize("24000", grouping=True) + " frames" in gaps and "0.5 s" in gaps
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


def _capture_record_with_concurrent_writer(root, actor):
    """Run the lock schedule in a subprocess so a regression cannot wedge pytest."""
    import faulthandler
    import os
    import threading
    from pathlib import Path

    from cassini_talk import library_edits
    from cassini_talk.library_v2 import journal
    from cassini_talk.transcription import REFERENCE

    root = Path(root)
    os.environ["CASSINI_LIBRARY_DIR"] = str(root)
    stopped = captured(root)
    other = captured(root)
    update_metadata(stopped.directory, recordingDetails=dict(future="kept"))
    library_owned, recorder_waiting = threading.Event(), threading.Event()
    editor_done, recorder_done = threading.Event(), threading.Event()
    errors = []
    enter = journal.LibraryLock.__enter__

    def scheduled_enter(lock):
        name = threading.current_thread().name
        if name == "capture-facts":
            # Old record_capture already owns meeting_store.LOCK here. The
            # corrected one requests this Library lock before taking LOCK.
            recorder_waiting.set()
        value = enter(lock)
        if name == "other-writer":
            library_owned.set()
            assert recorder_waiting.wait(3), "Capture writer never requested the Library lock"
        return value

    journal.LibraryLock.__enter__ = scheduled_enter

    def edit():
        try:
            outer = library_edits.transaction if actor == "edit" else library_edits.mutation
            with outer(other.directory):
                if actor == "edit":
                    update_metadata(other.directory, title="Edited while stopping", notes=[dict(atMs=0, text="Kept")])
                else:
                    update_metadata(
                        other.directory, transient=True, transcript=dict(status="running", reference=REFERENCE)
                    )
        except BaseException as error:
            errors.append(error)
        finally:
            editor_done.set()

    def stop():
        try:
            assert record_capture(stopped.directory, stopped.manifest)
        except BaseException as error:
            errors.append(error)
        finally:
            recorder_done.set()

    editor = threading.Thread(target=edit, name="other-writer", daemon=True)
    recorder = threading.Thread(target=stop, name="capture-facts", daemon=True)
    editor.start()
    assert library_owned.wait(3), "Other writer never acquired the Library lock"
    recorder.start()
    if not editor_done.wait(3) or not recorder_done.wait(3):
        faulthandler.dump_traceback(all_threads=True)
        raise AssertionError("Stop capture facts deadlocked with another writer in the same Library")
    editor.join(1)
    recorder.join(1)
    assert not editor.is_alive() and not recorder.is_alive(), "Metadata writers did not exit"
    if errors:
        raise errors[0]
    metadata = read_metadata(stopped.directory)
    assert metadata["recordingDetails"]["future"] == "kept"
    assert stored(metadata) == from_session(stopped.manifest)
    updated = read_metadata(other.directory)
    if actor == "edit":
        assert updated["title"] == "Edited while stopping"
        assert updated["notes"] == [dict(atMs=0, text="Kept")]
    else:
        assert updated["transcript"] == dict(status="running", reference=REFERENCE)


@pytest.mark.parametrize("actor", ["edit", "transcription"])
def test_capture_facts_and_another_library_writer_share_one_lock_order(tmp_path, actor):
    import multiprocessing

    process = multiprocessing.get_context("spawn").Process(
        target=_capture_record_with_concurrent_writer, args=(str(tmp_path / "Library"), actor)
    )
    process.start()
    try:
        process.join(10)  # Wait for process exit, never a fixed readiness delay.
        assert not process.is_alive(), "Concurrent capture-facts process exceeded its completion deadline"
        assert process.exitcode == 0, "Concurrent metadata writers failed; see child traceback"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)
            if process.is_alive():
                process.kill()
                process.join(3)


@pytest.mark.parametrize("initial_event", [True, False])
@pytest.mark.parametrize("cause", ["frameError", "lifecycle", "future.cause", None])
def test_legacy_fallback_cause_survives_portable_publication_without_session_cache(
    tmp_path, monkeypatch, cause, initial_event
):
    from cassini_talk.capture_details import read_details

    # This regression judges facts crossing the real portable-file publisher,
    # not the NumPy/GStreamer mixer. Supply its known one-second silent codec
    # output while leaving tagging, journal commit and cache-free reading real.
    from cassini_talk.library_v2 import mixdown
    from cassini_talk.recording_session import RecordingSession
    from cassini_talk.strings import text

    spec = importlib.util.spec_from_file_location(
        "silent_opus_fixture", Path(__file__).parents[1] / "ux/library-vectors/assets/build.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = tmp_path
    generator.build("encoded.opus", 1000)
    encoded = tmp_path / "encoded.opus"

    def sample_count(spans):
        assert [(s.start_ms, s.length, s.info.sample_rate, s.anchors) for s in spans] == [
            (0.0, 24000, 48000, ()),
            (500.0, 24000, 48000, ()),
        ]
        return 48000

    def encode(spans, output, choice, cancel=None):
        count = sample_count(spans)
        shutil.copyfile(encoded, output)
        return count

    monkeypatch.setattr(mixdown, "total_samples", sample_count)
    monkeypatch.setattr(mixdown, "encode", encode)
    root = tmp_path / "Library"
    session = RecordingSession(capture_parent(root), source="chromium")
    session.add_track("remote", "Voice", "remote", 48000, 1)
    if initial_event:
        with session.change([dict(type="othersSource", source="browser", reason="start", atMs=0)]):
            pass
    session.append("remote", bytes(48000), 0)
    session.end_track("remote")
    loss = dict(type="othersSource", source="system", reason="lost", atMs=500)
    if cause is not None:
        loss.update(cause=cause, detail="bounded saved producer explanation")
    with session.change([loss]):
        session.add_track("system", "Computer sound", "system", 48000, 1, label_source="device")
    session.append("system", bytes(48000), 500)
    session.finish(reason="user", at_ms=1000)
    assert "captureOutcome" not in session.manifest and "refusals" not in session.manifest
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.glob("*.wav")}
    published = publish_stopped(session)
    (meeting,) = LibraryScanner(root).scan().meetings
    record = stored(meeting.metadata)
    assert record is not None and record["sources"] == []
    assert next(e for e in record["events"] if e.get("reason") == "lost") == loss
    expected = text("details.othersFallback.cause." + (cause if cause == "frameError" else "other"))
    cached = read_details(meeting)
    assert expected in cached.plain_text()
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.directory.glob("*.wav")} == before
    shutil.rmtree(session.directory)
    (file_only,) = LibraryScanner(root).scan().meetings
    assert file_only.audio_file.name == published.name
    assert not (file_only.directory / "session.json").exists()
    assert stored(file_only.metadata) == record
    report = read_details(file_only)
    assert report.spans == cached.spans
    assert expected in report.plain_text()
    assert text("details.timelineUnknown") not in report.notes


@pytest.mark.parametrize(
    "event",
    [
        dict(type="othersSource", source="system", reason="lost", atMs=0, cause="transportClosed"),
        dict(type="selection", me=True, others=False, reason="user", atMs=1),
    ],
)
def test_source_events_are_portable_without_an_announced_source_inventory(event):
    record = from_session(dict(tracks=[], events=[event]))
    assert record is not None and record["sources"] == [] and record["events"] == [event]
    record["events"][0]["atMs"] = 999
    assert event["atMs"] != 999


def test_terminal_or_malformed_event_alone_does_not_invent_a_capture_inventory():
    assert from_session(dict(tracks=[], events=[dict(type="end", reason="user", atMs=10)])) is None
    assert from_session(dict(tracks=[], events=[dict(type="othersSource", atMs=-1)])) is None


@pytest.mark.parametrize(
    "route",
    [
        dict(source="system", reason="mixedSiteSignal", site="Synthetic meeting"),
        dict(source="future-source", reason="future-reason", site="future-site"),
    ],
)
def test_signal_system_route_survives_publication_cache_removal_and_metadata_edit(tmp_path, monkeypatch, route):
    from cassini_talk.capture_details import read_details
    from cassini_talk.library_v2 import edit, mixdown
    from cassini_talk.recording_session import RecordingSession
    from cassini_talk.strings import text

    # Controlled codec output, as the cause-provenance regression above; the
    # publisher, tags, journal, portable reader and edit remain production code.
    spec = importlib.util.spec_from_file_location(
        "route_codec_fixture", Path(__file__).parents[1] / "ux/library-vectors/assets/build.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = tmp_path
    generator.build("encoded.opus", 1000)

    def count(spans):
        assert [(s.length, s.info.sample_rate) for s in spans] == [(48000, 48000)]
        return 48000

    def encode(spans, output, choice, cancel=None):
        total = count(spans)
        shutil.copyfile(tmp_path / "encoded.opus", output)
        return total

    monkeypatch.setattr(mixdown, "total_samples", count)
    monkeypatch.setattr(mixdown, "encode", encode)
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    session = RecordingSession(capture_parent(root), source="gnome")
    session.add_track("system", "Computer sound", "system", 48000, 1, label_source="device")
    session.append("system", bytes(96000), 0)
    with session.change([dict(type="othersSource", source="system", reason="start", atMs=0)]):
        pass
    update_metadata(session.directory, recordingDetails=dict(othersRoute=route))
    session.finish(reason="callEnded", at_ms=1000)
    published = publish_stopped(session)
    shutil.rmtree(session.directory)
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.audio_file.name == published.name and meeting.metadata["recordingDetails"]["othersRoute"] == route
    expected = text("details.othersRoute.mixedSite", site="Synthetic meeting")
    assert (expected in read_details(meeting).notes) == (route["reason"] == "mixedSiteSignal")
    edit.edit_meeting(
        root, "Recording", session.manifest["id"], dict(title="Renamed synthetic signal call", titleSource="user")
    )
    (after,) = LibraryScanner(root).scan().meetings
    assert after.metadata["recordingDetails"]["othersRoute"] == route
    assert (expected in read_details(after).plain_text()) == (route["reason"] == "mixedSiteSignal")
