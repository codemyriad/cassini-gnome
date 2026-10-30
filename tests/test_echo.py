# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared D38 hand-authored flags and unchanged PCM/metadata projections."""

import hashlib
import json
import math
import struct
import wave
from pathlib import Path

import pytest

from cassini_viewer.echo import WaveLevels, echo_indices

ROOT = Path(__file__).with_name("bleed")
VECTORS = json.loads((ROOT / "echo-vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["id"])
def test_shared_echo_vector(case):
    original = json.dumps(case, ensure_ascii=False)
    assert len(case["words"]) == len(case["expectedEcho"])
    assert echo_indices(case["words"], case["tracks"]) == {i for i, flag in enumerate(case["expectedEcho"]) if flag}
    assert json.dumps(case, ensure_ascii=False) == original


def test_exact_shared_echo_bytes_and_synthetic_source_pins():
    for path, expected in json.loads((ROOT / "UPSTREAM.json").read_text())["sha256"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path


@pytest.mark.parametrize("rate,channels", [(16000, 1), (48000, 2)])
def test_rms_uses_meeting_offset_stereo_average_and_bounded_unchanged_pcm(tmp_path, rate, channels):
    pytest.importorskip("numpy")  # Optional source-only profile; offline probe always requires it.
    path = tmp_path / "mic.wav"
    frames = ([1000] if channels == 1 else [2000, 0]) * rate
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, 2, rate, 0, "NONE", ""))
        stream.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    before = path.read_bytes()
    with WaveLevels([dict(id="mic", path=str(path), offsetMs=500)]) as levels:
        expected = 20 * math.log10(1000 / 32768)
        assert levels.level("mic", 500, 1000) == pytest.approx(expected, abs=1e-9)
        assert levels.level("mic", 0, 500) < -200
        assert levels.level("mic", 1400, 2000) == pytest.approx(expected, abs=1e-9)
    assert path.read_bytes() == before


def test_echo_reader_ignores_only_boolean_true_and_keeps_raw_words(tmp_path):
    from test_library import capture, write_metadata

    from cassini_viewer.export_model import markdown, model_projection
    from cassini_viewer.library import LibraryScanner, LibrarySnapshot
    from cassini_viewer.search_index import SearchIndex
    from cassini_viewer.transcription import REFERENCE

    library = tmp_path / "Library"
    session = capture(library)
    words = [
        dict(speaker="me", startMs=0, endMs=100, text="Echoonly", echo=True),
        dict(speaker="others", startMs=200, endMs=400, text="Original"),
        dict(speaker="me", startMs=500, endMs=800, text="Own speech", echo=False),
    ]
    body = dict(format="cassini.words.v1", items=words)
    path = session.directory / "transcript.json"
    path.write_text(json.dumps(body))
    before = path.read_bytes()
    write_metadata(session.directory, transcript=dict(status="done", reference=REFERENCE))
    meeting = LibraryScanner(library).scan().meetings[0]
    assert [w.text for w in meeting.words] == ["Original", "Own speech"]
    assert len(meeting.turns) == 2 and {s.id: s.talk_ms for s in meeting.speakers} == {"others": 200, "me": 300}
    assert "Echoonly" not in markdown(model_projection(meeting))
    index = SearchIndex(library, tmp_path / "cache")
    try:
        index.sync(LibrarySnapshot(meetings=(meeting,)))
        assert not index.query("Echoonly", now_ms=0, tz="UTC")
        assert index.query("Original", now_ms=0, tz="UTC")
    finally:
        index.close()
    assert path.read_bytes() == before


def test_embedded_v1_accepts_additive_members_and_skips_echoes(tmp_path):
    import copy
    import runpy

    from cassini_viewer import portable
    from cassini_viewer.portable_writer import MANIFEST_MIME, WORDS_MIME, chunk_set, rewrite_tags

    path = Path(__file__).parent / "conformance/opus/001-minimal-v1.opus"
    meeting = portable.read_meeting(str(path))
    body = dict(
        format="cassini.words.v1",
        wordCount=2,
        items=[
            dict(speaker="a", startMs=0, endMs=10, text="echo", echo=True),
            dict(speaker="b", startMs=20, endMs=30, text="original", futureUnknown={"keep": True}),
        ],
    )
    manifest = copy.deepcopy(meeting.manifest)
    entry = manifest["transcripts"][0]
    word_tags, ref = chunk_set(entry["payloadRef"]["prefix"], body, WORDS_MIME)
    entry.update(payloadRef=ref, wordCount=2)
    manifest_tags, _ = chunk_set(portable.MANIFEST_PREFIX, manifest, MANIFEST_MIME)
    _, old_tags, _ = portable.parse_header(path.read_bytes())
    tags = dict(old_tags)
    tags.update(word_tags)
    tags.update(manifest_tags)
    target = tmp_path / "echo.opus"
    with path.open("rb") as source, target.open("wb") as output:
        rewrite_tags(source, output, tags)
    # Frozen pre-D38 reader accepts the additive field and retains every item.
    legacy = runpy.run_path(str(Path(__file__).parent / "export/cassini-read-pure.py"))["read_file"]
    result, _ = legacy(target)
    assert result["state"] == "unverified" and result["words"] == body["items"]
    updated = portable.read_meeting(str(target))
    assert [w.text for w in portable.load_words(updated, updated.default_transcript_id)] == ["original"]


def test_queue_keeps_echo_items_in_sidecar_and_recomputes_flags_on_retry(tmp_path, monkeypatch):
    from test_library import capture
    from test_transcription import until

    from cassini_viewer.library import LibraryScanner
    from cassini_viewer.meeting_store import read_metadata, update_metadata
    from cassini_viewer.transcription import TranscriptionQueue

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = capture(tmp_path)
    finished = []

    class Client:
        flag = True

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def close(self):
            pass

        def process_tracks(self, tracks, notify):
            for order, track in enumerate(tracks):
                notify(
                    dict(
                        type="progress",
                        trackId=track["id"],
                        trackOrder=order,
                        words=[dict(startMs=order * 100, endMs=order * 100 + 50, text=f"word{order}")],
                        language="en",
                        etaMinutes=0,
                    )
                )
            if self.flag:
                notify(dict(type="echo", indexes=[0]))
            return dict(diagnostics=[])

    queue = TranscriptionQueue(lambda _: None, finished.append, client_factory=Client, bundle=tmp_path)
    try:
        assert queue.enqueue(session.directory)
        until(lambda: bool(finished))
        body = json.loads((session.directory / "transcript.json").read_text())
        assert len(body["items"]) == 2 and body["items"][0]["echo"] is True
        assert [w.text for w in LibraryScanner(tmp_path).scan().meetings[0].words] == ["word1"]
        # A retry of a saved partial must recompute, rather than carrying the
        # earlier echo decision into the fresh recognition output.
        update_metadata(
            session.directory, transcript=dict(read_metadata(session.directory)["transcript"], status="failed")
        )
        Client.flag = False
        assert queue.enqueue(session.directory)
        until(lambda: len(finished) == 2)
        body = json.loads((session.directory / "transcript.json").read_text())
        assert all("echo" not in word for word in body["items"])
        assert len(LibraryScanner(tmp_path).scan().meetings[0].words) == 2
    finally:
        queue.close()
