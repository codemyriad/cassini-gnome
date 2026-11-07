# SPDX-License-Identifier: GPL-3.0-or-later
"""Untrusted browser label provenance survives the product recording paths."""

import json

import pytest
from test_capture_record import declare, pcm, stop

from cassini_talk.capture_report import from_session, record_capture, stored
from cassini_talk.capture_sources import BrowserStreams
from cassini_talk.meeting_store import read_metadata
from cassini_talk.recording_session import BrowserHost
from cassini_talk.source_report import sources


@pytest.fixture(params=["direct", "selected"])
def host(tmp_path, request):
    host = BrowserHost(tmp_path)
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="capture-a"))
    if request.param == "selected":
        host.audio_sink = BrowserStreams(host.session)
    yield host
    host.close()


def test_dom_snapshot_survives_retry_replacement_and_portable_details(host):
    registration = dict(declare("first", "receiver"), label="Hypatia\u0000\u200f", labelSource="dom")
    host.handle(registration)
    host.handle(pcm("first", 48000))
    # An idempotent retry cannot rewrite the first accepted snapshot or reset PCM.
    host.handle(dict(registration, label="Changed page text", labelSource="browser"))
    assert host.sequences["first"] == 1
    assert host.handle(dict(type="trackEnd", id="first", reason="replaced", droppedFrames=0, endOffsetMs=1000)) == dict(
        type="ack"
    )
    host.handle(dict(registration, id="second"))
    host.handle(pcm("second", 48000, offset=1000))
    stop(
        host,
        discoveredSources=[dict(sourceTrackId="receiver", kind="remote", label="Late summary", labelSource="browser")],
    )
    manifest = json.loads((host.session.directory / "session.json").read_text())
    assert [(t["label"], t["labelSource"]) for t in manifest["tracks"]] == [("Hypatia", "dom")] * 2
    (source,) = manifest["captureOutcome"]["sources"]
    assert (source["label"], source["labelSource"], source["trackIds"]) == ("Hypatia", "dom", ["first", "second"])
    assert source["captureOutcome"] == "captured" and source["audioMs"] == 2000
    assert record_capture(host.session.directory, manifest)
    metadata = read_metadata(host.session.directory)
    report = stored(metadata)
    assert report == from_session(manifest)
    assert [t["labelSource"] for t in report["tracks"]] == ["dom", "dom"]
    # Details can render the portable report with no session cache or source files.
    (fact,) = sources({}, metadata, live=False)
    assert fact.name == "Hypatia" and fact.label_source == "dom"
    assert "Hypatia" in fact.line
    assert not record_capture(host.session.directory, manifest)


def test_dom_source_ledger_keeps_failed_and_never_declared_sources(host):
    host.handle(
        dict(
            type="sources",
            discoveredSources=[
                dict(sourceTrackId="missing", kind="remote", label="Ibn al-Haytham", labelSource="dom", failed=True),
                dict(sourceTrackId="quiet", kind="remote", label="Hypatia", labelSource="dom"),
            ],
        )
    )
    host.handle(dict(declare("one", "quiet"), label="Hypatia", labelSource="dom"))
    stop(host)
    report = from_session(host.session.manifest)
    assert {s["sourceTrackId"]: (s["labelSource"], s["captureOutcome"]) for s in report["sources"]} == {
        "missing": ("dom", "failed"),
        "quiet": ("dom", "neverAudio"),
    }
    assert report["tracks"][0]["labelSource"] == "dom"


def test_dom_selected_spans_keep_provenance_across_deselection(host):
    if host.audio_sink is None:
        host.audio_sink = BrowserStreams(host.session)
    host.handle(dict(declare("one", "receiver"), label="Hypatia", labelSource="dom"))
    host.handle(pcm("one", 48000))
    host.audio_sink.select(me=True, others=False, at=1000)
    host.audio_sink.select(me=True, others=True, at=1500)
    host.handle(pcm("one", 48000, sequence=1, offset=1500))
    stop(host)
    tracks = host.session.manifest["tracks"]
    assert len(tracks) == 2
    assert [(t["label"], t["labelSource"], t["sourceTrackId"]) for t in tracks] == [("Hypatia", "dom", "receiver")] * 2
    assert host.session.manifest["captureOutcome"]["sources"][0]["trackIds"] == [t["id"] for t in tracks]


@pytest.mark.parametrize("value", [17, True, [], {}, "device", "user", "recovered", "verified-participant"])
@pytest.mark.parametrize("boundary", ["track", "sources", "stop"])
def test_reserved_or_invalid_browser_provenance_is_refused_without_admission(host, value, boundary):
    seen = dict(host.session.capture_record.seen)
    source = dict(sourceTrackId="receiver", kind="remote", label="Hypatia", labelSource=value)
    message = (
        dict(declare("one", "receiver"), labelSource=value)
        if boundary == "track"
        else dict(
            type=boundary,
            discoveredSources=[source],
            captureId="capture-a",
        )
    )
    response = host.handle(message)
    assert response["type"] == "error" and response["code"] == "source"
    assert host.session.capture_record.seen == seen
    assert host.session.manifest["tracks"] == []
    assert host.session.writers == {}
    assert not host.session.finished


@pytest.mark.parametrize("selected", [False, True], ids=["direct", "selected"])
def test_dom_provenance_survives_real_publication_and_cache_free_details(tmp_path, monkeypatch, selected):
    import importlib.util
    import shutil
    from pathlib import Path

    from cassini_talk.capture_details import read_details
    from cassini_talk.library import LibraryScanner
    from cassini_talk.library_publication import capture_parent, publish_stopped
    from cassini_talk.library_v2 import mixdown

    # Use the known silent codec fixture; publication, tagging and cache-free
    # reading stay real without introducing NumPy into minimal environments.
    spec = importlib.util.spec_from_file_location(
        "dom_silent_opus", Path(__file__).parents[1] / "ux/library-vectors/assets/build.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = tmp_path
    generator.build("encoded.opus", 1000)
    encoded = tmp_path / "encoded.opus"

    def sample_count(spans):
        assert [(s.start_ms, s.length, s.info.sample_rate) for s in spans] == [(0.0, 48000, 48000)]
        return 48000

    def encode(spans, output, choice, cancel=None):
        count = sample_count(spans)
        shutil.copyfile(encoded, output)
        return count

    monkeypatch.setattr(mixdown, "total_samples", sample_count)
    monkeypatch.setattr(mixdown, "encode", encode)
    root = tmp_path / "Library"
    host = BrowserHost(capture_parent(root))
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="capture-a"))
    if selected:
        host.audio_sink = BrowserStreams(host.session)
    assert host.handle(dict(declare("one", "receiver"), label="Hypatia", labelSource="dom")) == dict(type="ack")
    assert host.handle(pcm("one", 48000)) == dict(type="ack")
    assert stop(host)["type"] == "stopped"
    published = publish_stopped(host.session)
    shutil.rmtree(host.session.directory)
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.audio_file.name == published.name
    assert not (meeting.directory / "session.json").exists()
    record = stored(meeting.metadata)
    assert [(t["label"], t["labelSource"]) for t in record["tracks"]] == [("Hypatia", "dom")]
    assert [(s["label"], s["labelSource"]) for s in record["sources"]] == [("Hypatia", "dom")]
    details = read_details(meeting)
    assert details.sources[0].label_source == "dom"
    assert "Hypatia" in details.plain_text()


def test_dom_failed_writer_creation_retains_provenance_on_retry(host, monkeypatch):
    import errno

    from cassini_talk.recording_session import WAVWriter

    def no_header(_writer):
        raise OSError(errno.ENOSPC, "Generated DOM-source header failure")

    with monkeypatch.context() as patch:
        patch.setattr(WAVWriter, "_header", no_header)
        reply = host.handle(dict(declare("failed", "receiver"), label="Hypatia", labelSource="dom"))
    assert reply["code"] == "io" and reply["scope"] == "source"
    assert not host.session.writers
    assert host.handle(dict(declare("retry", "receiver"), label="Hypatia", labelSource="dom")) == dict(type="ack")
    assert host.handle(pcm("retry", 48000)) == dict(type="ack")
    assert stop(host)["type"] == "stopped"
    report = from_session(host.session.manifest)
    (source,) = report["sources"]
    assert (source["label"], source["labelSource"], source["captureOutcome"]) == ("Hypatia", "dom", "failed")
    assert source["trackIds"] == ["retry"]
    assert report["tracks"][0]["labelSource"] == "dom"
    assert host.session.manifest["problemKind"] == "io"
