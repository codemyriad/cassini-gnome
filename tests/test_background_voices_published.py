# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime-dependent fixtures follow explicit dependency checks.
"""D112 (L-86): background voices ride in the Library file's x….meeting.speakers and survive reopening."""

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication tests require GStreamer", allow_module_level=True)

from library_v2_support import simple_session

from cassini_talk import meeting_store, portable
from cassini_talk.library import LibraryScanner
from cassini_talk.library_edits import commit_metadata, transaction
from cassini_talk.library_v2 import finalize, xschema
from cassini_talk.speaker_edits import restore_speakers, save_speakers


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    sid, directory = simple_session(tmp_path, 0.1)
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    return tmp_path, sid, directory, tmp_path / result.name


def audio_digest(path):
    with path.open("rb") as stream:
        return portable.compute_audio_digest(stream)["sha256"]


def file_speakers(root):
    (meeting,) = LibraryScanner(root).scan().meetings
    embedded = portable.read_meeting(str(meeting.audio_file))
    return meeting, embedded


def test_background_voice_is_stored_in_the_library_file_and_undo_removes_it(published):
    root, _sid, directory, path = published
    body = dict(
        format=portable.WORDS_FORMAT,
        items=[
            dict(speaker="a", startMs=0, endMs=20, text="first"),
            dict(speaker="b", startMs=20, endMs=40, text="second"),
        ],
    )
    speakers = [
        dict(id=i, name=n, provenance="user", colourIndex=j) for j, (i, n) in enumerate([("a", "Alice"), ("b", "Bob")])
    ]
    with transaction(directory):
        commit_metadata(
            directory,
            meeting_store.read_metadata(directory),
            dict(speakers=speakers, transcript=dict(status="done", reference=None)),
            words=body,
        )
    digest = audio_digest(path)
    _saved, previous = save_speakers(directory, {"b": {"background": True}})
    meeting, embedded = file_speakers(root)
    stored = {s["id"]: s for s in embedded.manifest["x"][xschema.DOMAIN]["meeting"]["speakers"]}
    assert stored["b"]["background"] is True and stored["b"]["name"] == "Bob"
    # Manifest speakers keep every voice with its label; the words are unchanged.
    assert {s.id: s.label for s in embedded.speakers} == {"a": "Alice", "b": "Bob"}
    assert [w.speaker for w in portable.load_words(embedded, embedded.default_transcript_id)] == ["a", "b"]
    assert audio_digest(meeting.audio_file) == digest
    # Reopening reads it back.
    assert meeting.background_speakers == {"b"} and meeting.speaker_names == ("Alice",)
    restore_speakers(directory, previous)
    meeting, embedded = file_speakers(root)
    stored = {s["id"]: s for s in embedded.manifest["x"][xschema.DOMAIN]["meeting"]["speakers"]}
    assert "background" not in stored["b"] and meeting.speaker_names == ("Alice", "Bob")
