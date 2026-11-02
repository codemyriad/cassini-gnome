# SPDX-License-Identifier: GPL-3.0-or-later
"""D61: a browser track's record-only clock facts reach session.json and x as received."""

import json

import pytest
from test_capture_record import declare, pcm

from cassini_talk.capture_sources import BrowserStreams
from cassini_talk.library_v2 import mixdown, xschema
from cassini_talk.recording_session import BrowserHost, clock_facts, document_generation

FACTS = dict(clockMethod="roundTrip", clockUncertaintyMs=9, clockClampMs=3)


@pytest.fixture(params=["direct", "selected"])
def host(tmp_path, request):
    host = BrowserHost(tmp_path)
    host.handle(dict(type="start", features=["source-outcomes-v1"], captureId="clock-facts"))
    if request.param == "selected":
        host.audio_sink = BrowserStreams(host.session)
    return host


def recorded(host, **fields):
    """Declare one browser span with `fields`, give it PCM, stop, and read session.json."""
    assert host.handle(dict(declare("x", "root"), **fields)) == dict(type="ack")
    assert host.handle(pcm("x", 4)) == dict(type="ack")
    host.handle(dict(type="stop"))
    manifest = json.loads((host.session.directory / "session.json").read_text())
    (track,) = manifest["tracks"]
    return manifest, track


def test_valid_clock_facts_are_copied_and_the_document_generation_never_is(host):
    manifest, track = recorded(host, documentGeneration="doc-1", **FACTS)
    assert {key: track.get(key) for key in FACTS} == FACTS
    assert "documentGeneration" not in json.dumps(manifest)
    # They travel with the logical track into x["codemyriad.io"].tracks.
    published = mixdown.session_spans(host.session.directory, manifest).tracks
    assert {key: published[0].get(key) for key in FACTS} == FACTS
    assert "documentGeneration" not in published[0]
    assert xschema.valid(published, xschema.SCHEMA["properties"]["tracks"])


def test_absent_clock_facts_are_omitted(host):
    _manifest, track = recorded(host)
    assert not set(FACTS) & set(track)


INVALID = [
    ("clockMethod", "getOutputTimestamp"),
    ("clockMethod", ""),
    ("clockMethod", None),
    ("clockMethod", 1),
    ("clockMethod", ["roundTrip"]),
    ("clockUncertaintyMs", 9.0),
    ("clockUncertaintyMs", 9.5),
    ("clockUncertaintyMs", True),
    ("clockUncertaintyMs", -1),
    ("clockUncertaintyMs", 86400001),
    ("clockUncertaintyMs", "9"),
    ("clockUncertaintyMs", None),
    ("clockClampMs", 0),
    ("clockClampMs", 3.0),
    ("clockClampMs", True),
    ("clockClampMs", 86400001),
    ("clockClampMs", None),
]


@pytest.mark.parametrize(("key", "value"), INVALID, ids=lambda v: repr(v))
def test_each_invalid_fact_is_omitted_on_its_own(host, key, value):
    _manifest, track = recorded(host, **dict(FACTS, **{key: value}))
    assert key not in track
    assert {k: track[k] for k in FACTS if k != key} == {k: v for k, v in FACTS.items() if k != key}


def test_the_bounds_are_kept_and_clamp_is_never_checked_against_uncertainty():
    assert clock_facts(dict(clockMethod="outputTimestamp", clockUncertaintyMs=0, clockClampMs=86400000)) == dict(
        clockMethod="outputTimestamp", clockUncertaintyMs=0, clockClampMs=86400000
    )
    assert clock_facts(dict(clockUncertaintyMs=86400000, clockClampMs=1)) == dict(
        clockUncertaintyMs=86400000, clockClampMs=1
    )


def test_a_document_generation_is_1_to_200_utf8_bytes():
    assert document_generation("d") == "d"
    assert document_generation("é" * 100) == "é" * 100
    for value in ("", "é" * 101, "x" * 201, None, 7, True, ["doc-1"]):
        assert document_generation(value) is None
