# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared public-speech corpus through scanner, speech inputs and publication."""

import hashlib
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from cassini_talk.diarization import group_tracks
from cassini_talk.library import LibraryScanner
from cassini_talk.meeting_store import read_metadata
from cassini_talk.transcription import TranscriptionQueue, prepare_tracks

ROOT = Path(__file__).parents[1] / "browser-protocol/conformance/multistream"


def test_shared_public_corpus_keeps_source_spans_calls_and_word_attribution_separate(tmp_path):
    np = pytest.importorskip("numpy")
    from cassini_talk.diarization_audio import rejoin

    spec = importlib.util.spec_from_file_location("shared_multistream_corpus", ROOT / "make.py")
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    corpus.make(tmp_path)
    snapshot = LibraryScanner(tmp_path).scan()
    meetings = {meeting.id: meeting for meeting in snapshot.meetings}
    first = meetings["05200000-0000-4000-8000-000000000001"]
    second = meetings["05200000-0000-4000-8000-000000000002"]
    assert (first.duration_ms, second.duration_ms) == (90000, 40000)
    assert first.directory != second.directory and len(first.tracks) == 8 and len(second.tracks) == 1
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for meeting in meetings.values()
        for path in meeting.directory.iterdir()
        if path.suffix == ".wav" or path.name == "session.json"
    }
    jobs, speakers = prepare_tracks(first)
    groups = {group["id"]: group for group in group_tracks(jobs)}
    assert set(groups) == set(speakers) == {"me", "others", "track-source-a", "track-source-b"}
    assert groups["track-source-a"]["tracks"] == ["track-a-short.wav", "track-a-long.wav", "track-a-return.wav"]
    assert groups["track-source-a"]["durationMs"] == 62000
    assert groups["track-source-b"]["tracks"] == ["track-b.wav", "track-b-return.wav"]
    assert groups["others"]["tracks"] == ["track-system.wav", "track-system-fallback.wav"]
    assert set(prepare_tracks(second)[1]) == {"track-other-call-source"}
    assert all(speakers[key]["name"] is None for key in ("track-source-a", "track-source-b"))

    by_id = {job["id"]: job for job in jobs}
    source_a = [by_id[identifier] for identifier in groups["track-source-a"]["tracks"]]
    with rejoin(source_a, scratch=tmp_path / "scratch") as (stream, frames, _digest):
        samples = np.frombuffer(stream.read(), dtype="<f4")
        assert frames == 62000 * 16
        # Known absent regions stay absent in actual recogniser/diarizer input.
        # Keep probes away from FIR edge context at physical span boundaries.
        for left, right in ((0, 29000), (33000, 39000), (42000, 47000), (51000, 59000)):
            assert not np.any(samples[left * 16 : right * 16]), (left, right)
        for left, right in ((30000, 32000), (40000, 41000), (48000, 50000), (60000, 62000)):
            assert np.max(np.abs(samples[left * 16 : right * 16])) > 0.01, (left, right)

    expected = {
        "track-mic.wav": "me",
        "track-system.wav": "others",
        "track-a-short.wav": "track-source-a",
        "track-a-long.wav": "track-source-a",
        "track-a-return.wav": "track-source-a",
        "track-b.wav": "track-source-b",
        "track-system-fallback.wav": "others",
        "track-b-return.wav": "track-source-b",
    }

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            for order, track in enumerate(tracks):
                # Fake only inference; exercise the real FIFO, attribution,
                # metadata transaction and transcript publisher for every span.
                at = track["offsetMs"] + 100
                notify(
                    dict(
                        type="progress",
                        trackId=track["id"],
                        trackOrder=order,
                        language="en",
                        etaMinutes=0,
                        words=[dict(text=track["id"], startMs=at, endMs=at + 100)],
                    )
                )
            return dict(diagnostics=[])

    completed = threading.Event()
    queue = TranscriptionQueue(
        lambda _directory: None,
        lambda _directory: completed.set(),
        root=tmp_path,
        bundle=tmp_path,
        client_factory=Client,
    )
    try:
        assert queue.enqueue(first.directory)
        assert completed.wait(10), "The corpus transcript was not published"
        assert read_metadata(first.directory)["transcript"]["status"] == "done"
        words = json.loads((first.directory / "transcript.json").read_text())["items"]
        assert len(words) == len(expected)
        assert {word["text"]: word["speaker"] for word in words} == expected
        assert not (second.directory / "transcript.json").exists()
    finally:
        queue.close()
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in before.items())
