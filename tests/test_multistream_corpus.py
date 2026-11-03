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
CORPUS = json.loads((ROOT / "corpus.json").read_text())


def absent(present, end, margin):
    """Ms ranges between sorted present ones, `margin` clear of each edge (FIR context)."""
    edges = [0, *(edge for window in present for edge in window), end]
    pairs = zip(edges[::2], edges[1::2], strict=True)
    return [(left + margin, right - margin) for left, right in pairs if right - left > 2 * margin]


def test_shared_public_corpus_keeps_source_spans_calls_and_word_attribution_separate(tmp_path):
    np = pytest.importorskip("numpy")
    from cassini_talk.diarization_audio import rejoin

    spec = importlib.util.spec_from_file_location("shared_multistream_corpus", ROOT / "make.py")
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    corpus.make(tmp_path)
    snapshot = LibraryScanner(tmp_path).scan()
    meetings = {meeting.id: meeting for meeting in snapshot.meetings}
    call, other = CORPUS["sessions"]
    first, second = meetings[call["id"]], meetings[other["id"]]
    assert (first.duration_ms, second.duration_ms) == (call["durationMs"], other["durationMs"])
    assert first.directory != second.directory
    assert (len(first.tracks), len(second.tracks)) == (len(call["tracks"]), len(other["tracks"]))
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for meeting in meetings.values()
        for path in meeting.directory.iterdir()
        if path.suffix == ".wav" or path.name == "session.json"
    }
    jobs, speakers = prepare_tracks(first)
    groups = {group["id"]: group for group in group_tracks(jobs)}
    members = {}
    for track in call["tracks"]:
        group = {"microphone": "me", "system": "others"}.get(track["kind"], f"track-{track.get('sourceTrackId')}")
        members.setdefault(group, []).append(track)
    assert set(groups) == set(speakers) == set(members) == {"me", "others", "track-source-a", "track-source-b"}
    for group, tracks in members.items():
        assert groups[group]["tracks"] == [f"track-{track['id']}.wav" for track in tracks]
    source_a = members["track-source-a"]
    assert groups["track-source-a"]["durationMs"] == max(track["endMs"] for track in source_a)
    assert set(prepare_tracks(second)[1]) == {f"track-{other['tracks'][0]['sourceTrackId']}"}
    assert all(speakers[key]["name"] is None for key in ("track-source-a", "track-source-b"))

    by_id = {job["id"]: job for job in jobs}
    expected_tracks = CORPUS["expected"]["sessions"][call["id"]]["tracks"]
    present = sorted(
        (left // 16, right // 16) for track in source_a for left, right in expected_tracks[track["id"]]["presence16k"]
    )
    end = groups["track-source-a"]["durationMs"]
    with rejoin([by_id[f"track-{track['id']}.wav"] for track in source_a], scratch=tmp_path / "scratch") as (
        stream,
        frames,
        _digest,
    ):
        samples = np.frombuffer(stream.read(), dtype="<f4")
        assert frames == end * 16
        # Known absent regions stay absent in actual recogniser/diarizer input.
        # Keep probes away from FIR edge context at physical span boundaries.
        quiet = absent(present, end, 1000)
        assert quiet
        for left, right in quiet:
            assert not np.any(samples[left * 16 : right * 16]), (left, right)
        for left, right in present:
            assert np.max(np.abs(samples[left * 16 : right * 16])) > 0.01, (left, right)

    expected = {f"track-{track['id']}.wav": group for group, tracks in members.items() for track in tracks}

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
