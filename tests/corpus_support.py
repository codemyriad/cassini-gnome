# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared multistream corpus (protocol conformance/multistream), materialized once per module.

Tests take every value they compare with from the vendored corpus.json:
`constants` and the hand-written `expected` block. Only inference is faked: the
real speech pipeline, diarization worker and queue run through
test_silent_tracks.in_process_child with a fake recognizer, VAD and diarizer.
"""

import importlib.util
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "browser-protocol/conformance/multistream"
CORPUS = json.loads((ROOT / "corpus.json").read_text())
C = CORPUS["constants"]
SESSIONS = {session["id"]: session for session in CORPUS["sessions"]}
CALL, OTHER, REJOINED = SESSIONS
EXPECTED = CORPUS["expected"]
RATE = 16000


def job_id(track):
    """The prepared job id (the WAV name) of a corpus track id."""
    return f"track-{track}.wav"


def person(track):
    """The expected block's person key of a corpus track: me, others or its source."""
    return {"microphone": "me", "system": "others"}.get(track["kind"], track.get("sourceTrackId"))


def speaker(key):
    """GNOME's speaker id for an expected person key."""
    return key if key in ("me", "others") else f"track-{key}"


def tracks(session=CALL):
    return {track["id"]: track for track in SESSIONS[session]["tracks"]}


def expected(session=CALL):
    return EXPECTED["sessions"][session]


def overlaps(window, ranges):
    """Whether a half-open ms window overlaps any of the half-open ranges."""
    left, right = window
    return any(left < b and a < right for a, b in ranges)


@dataclass
class Corpus:
    root: Path
    meetings: dict  # session id -> scanned Meeting
    jobs: dict  # session id -> prepare_tracks jobs, by job id
    groups: dict  # session id -> group_tracks rows, by group id


def materialize(destination):
    spec = importlib.util.spec_from_file_location("shared_multistream_corpus", ROOT / "make.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.make(destination)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """Materialized once per test module and removed after it, so later tests keep the tmpfs."""
    pytest.importorskip("numpy")
    from cassini_talk.diarization import group_tracks
    from cassini_talk.library import LibraryScanner
    from cassini_talk.transcription import prepare_tracks

    root = tmp_path_factory.mktemp("corpus")
    materialize(root)
    meetings = {meeting.id: meeting for meeting in LibraryScanner(root).scan().meetings}
    jobs, groups = {}, {}
    for identifier, meeting in meetings.items():
        prepared, _speakers = prepare_tracks(meeting)
        jobs[identifier] = {job["id"]: job for job in prepared}
        groups[identifier] = {group["id"]: group for group in group_tracks(prepared)}
    yield Corpus(root, meetings, jobs, groups)
    shutil.rmtree(root.absolute(), ignore_errors=True)


def copy_meeting(corpus, destination, session=CALL):
    """A fresh Library holding one corpus meeting: WAVs hard-linked, session.json copied."""
    from cassini_talk.library import LibraryScanner

    source = corpus.meetings[session].directory
    folder = destination / source.name
    folder.mkdir(parents=True)
    for path in source.iterdir():
        if path.suffix == ".wav":
            os.link(path, folder / path.name)
        else:
            shutil.copy2(path, folder / path.name)
    (meeting,) = LibraryScanner(destination).scan().meetings
    return meeting


@dataclass
class Run:
    meeting: object  # rescanned after the writing-down, with its published words
    words: dict  # job id -> words the child sent for that track
    summary: dict  # the child's process_tracks result
    rejoined: list  # captureTrackIds of every diarization rejoin
    keep: set | None = None  # the decoded corpus track ids; None decodes every track

    def diagnostics(self, code):
        return {d["track"]: d for d in self.summary["diagnostics"] if d["code"] == code}


def run_pipeline(corpus, monkeypatch, destination, *, model=None, probability=1.0, keep=None, session=CALL):
    """Write down one corpus meeting with the real child, queue and publisher.

    `keep` names the corpus track ids to decode; the rest take the D56 silent
    path, so a mutant run reads only what its check needs.
    """
    from test_silent_tracks import in_process_child, write_down

    from cassini_talk.library import LibraryScanner

    monkeypatch.setenv("XDG_CACHE_HOME", str(destination / "cache"))
    meeting = copy_meeting(corpus, destination / "library", session)
    decoded, rejoined, sent, events = set(), [], [], []
    keep = None if keep is None else set(keep)
    client = in_process_child(
        monkeypatch, decoded, rejoined, sent, model=model, probability=probability, keep=keep, events=events
    )
    write_down(destination / "library", meeting.directory, client)
    words = {}
    for event in events:
        if event["type"] == "progress":
            words.setdefault(event["trackId"], []).extend(event["words"])
    (summary,) = (event for event in events if event["type"] == "summary")
    (after,) = LibraryScanner(destination / "library").scan().meetings
    return Run(after, words, summary, rejoined, keep)


class Mute:
    """A recognizer that hears nothing, so only scripted words are written."""

    def transcribe(self, samples, language, word_timestamps):
        return dict(text="", language="en", words=[])


def label(job, word):
    return f"{job}:{word[0]}-{word[1]}"


def scripted(monkeypatch, script):
    """VAD segments recognise `script[job id]`: session-ms words, all in the track's first segment.

    The words reach the child's write-down as an engine reports them, past
    G11; the track's later segments find nothing. Fallback windows still
    decode with the run's model.
    """
    from cassini_talk import speech_pipeline as pipeline

    original, sent = pipeline.recognise_segment, set()

    def recognise(model, path, segment, offset_ms=0, **kwargs):
        value, words, language, digest = original(model, path, segment, offset_ms, **kwargs)
        if segment.get("reason") == "noVadWindow":
            return value, words, language, digest
        job = Path(path).name
        words = [dict(startMs=a, endMs=b, text=label(job, (a, b))) for a, b in script.get(job, []) if job not in sent]
        sent.add(job)
        return dict(value, text=" ".join(w["text"] for w in words)), words, language, digest

    monkeypatch.setattr(pipeline, "recognise_segment", recognise)
