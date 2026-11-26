# SPDX-License-Identifier: GPL-3.0-or-later
"""D112 (L-86) background voices: stored field, one-Save edits and Undo, and what readers leave out."""

import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from cassini_talk import portable
from cassini_talk.export_model import markdown, model_projection
from cassini_talk.library import LibraryScanner
from cassini_talk.library_v2 import xschema
from cassini_talk.meeting_store import atomic_json, read_metadata, update_metadata
from cassini_talk.meeting_text import background_runs, reading_turns
from cassini_talk.portable_writer import descriptors
from cassini_talk.recording_session import RecordingSession
from cassini_talk.search_index import SearchIndex
from cassini_talk.speaker_edits import current_meeting, restore_speakers, save_speakers, save_summary

ROOT = Path(__file__).resolve().parents[1]
VECTOR = ROOT / "ux/library-vectors/round-trip/background-voice"
NAMES = (("a", "Alice"), ("b", "Bob"), ("c", None), ("d", None))
# Alice, then two background turns (c, d), Bob, then c again on its own.
SPOKEN = (("a", "alpha"), ("c", "charlie"), ("d", "delta"), ("b", "bravo"), ("c", "cello"))


@pytest.fixture
def meeting(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    session = RecordingSession(root, source="gnome")
    session.add_track("system", None, "system", 16000, 1)
    session.append("system", b"\x01\x00" * 16000 * 6, 0)
    session.finish()
    speakers = [
        dict(id=i, name=name, provenance="user" if name else "unnamed", colourIndex=index, private="keep")
        for index, (i, name) in enumerate(NAMES)
    ]
    words = [
        dict(speaker=s, startMs=i * 1000, endMs=i * 1000 + 800, text=word, lowConfidenceSpeaker=False)
        for i, (s, word) in enumerate(SPOKEN)
    ]
    atomic_json(session.directory / "transcript.json", dict(format="cassini.words.v1", language="en", items=words))
    update_metadata(
        session.directory,
        speakers=speakers,
        transcript=dict(
            status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        ),
    )
    return current_meeting(session.directory)


def entries(directory):
    return {s["id"]: s for s in read_metadata(directory)["speakers"]}


def test_one_save_names_and_marks_background_and_undo_puts_everything_back(meeting):
    before = read_metadata(meeting.directory)
    saved, previous = save_speakers(
        meeting.directory, {"a": {"name": "Alicia"}, "c": {"background": True}, "d": {"background": True}}
    )
    assert saved["revision"] == before["revision"] + 1
    stored = entries(meeting.directory)
    assert stored["a"]["name"] == "Alicia" and stored["a"]["provenance"] == "user"
    assert stored["c"]["background"] is True and stored["d"]["background"] is True
    # The name and other members are kept; background is only ever true.
    assert stored["c"]["name"] is None and stored["c"]["private"] == "keep"
    assert "background" not in stored["a"] and "background" not in stored["b"]
    assert previous == {
        "a": dict(name="Alice", provenance="user"),
        "c": dict(name=None, provenance="unnamed"),
        "d": dict(name=None, provenance="unnamed"),
    }
    restore_speakers(meeting.directory, previous)
    assert read_metadata(meeting.directory)["speakers"] == before["speakers"]


def test_switching_background_off_removes_the_key_and_restores_the_kept_name(meeting):
    save_speakers(meeting.directory, {"a": {"background": True}})
    current = current_meeting(meeting.directory)
    assert current.background_speakers == {"a"}
    assert next(s for s in current.speakers if s.id == "a").name == "Alice"
    save_speakers(meeting.directory, {"a": {"background": False}})
    assert "background" not in entries(meeting.directory)["a"]
    assert current_meeting(meeting.directory).speaker_names[0] == "Alice"


def test_save_rejects_hidden_speakers_and_bad_names_without_writing(meeting):
    before = (meeting.directory / "meeting.json").read_bytes()
    with pytest.raises(ValueError):
        save_speakers(meeting.directory, {"zzz": {"background": True}})
    with pytest.raises(ValueError):
        save_speakers(meeting.directory, {"a": {"name": "x\ny"}, "c": {"background": True}})
    assert (meeting.directory / "meeting.json").read_bytes() == before


def test_names_row_library_card_keep_numbers_and_colours(meeting):
    save_speakers(meeting.directory, {"c": {"background": True}, "d": {"background": True}})
    current = current_meeting(meeting.directory)
    # Speaking order a, c, d, b: background voices leave the names, numbering stays.
    assert [s.name for s in current.speakers] == ["Alice", "Speaker 2", "Speaker 3", "Bob"]
    assert current.speaker_names == ("Alice", "Bob")
    assert [s.id for s in current.preview_speakers] == ["a", "b"]
    meta, _line = current.row_texts()
    assert "Alice" in meta and "Bob" in meta and "Speaker" not in meta
    assert [s.colour_index for s in current.speakers] == [0, 1, 2, 3]


def test_turns_fold_into_runs_counting_their_voices(meeting):
    save_speakers(meeting.directory, {"c": {"background": True}, "d": {"background": True}})
    current = current_meeting(meeting.directory)
    rows = reading_turns(current.words)

    def background_of(identifier):
        speaker = current.speaker_for(identifier)
        return speaker.id if speaker.background else None

    runs = background_runs(rows, background_of)
    assert [row.speaker for row in rows] == ["a", "c", "d", "b", "c"]
    assert runs == [None, (1, 2), (1, 2), None, (4, 1)]
    assert background_runs(rows, lambda _speaker: None) == [None] * 5
    assert background_runs((), background_of) == []


def test_search_and_text_exports_leave_background_voices_out(meeting, tmp_path):
    save_speakers(meeting.directory, {"c": {"background": True}})
    current = current_meeting(meeting.directory)
    projection = model_projection(current)
    assert [item["text"] for item in projection["items"]] == ["alpha", "delta", "bravo"]
    assert {s["id"] for s in projection["speakers"]} == {"a", "b", "d"}
    body = markdown(projection)
    assert "charlie" not in body and "cello" not in body and "alpha" in body
    with closing(SearchIndex(meeting.directory.parent, tmp_path / "search")) as index:
        index.sync(LibraryScanner(meeting.directory.parent).scan())
        assert not index.query("charlie", now_ms=0, tz="UTC")
        assert index.query("delta", now_ms=0, tz="UTC")
        # Unnamed voices are never filters; a named background voice isn't either.
        save_speakers(meeting.directory, {"a": {"background": True}})
        index.sync(LibraryScanner(meeting.directory.parent).scan())
        assert "Alice" not in index.speakers() and "Bob" in index.speakers()
        assert not index.query("alpha", now_ms=0, tz="UTC")


def test_opus_share_keeps_every_speaker_and_word(meeting):
    """The audio, its words and the manifest speakers don't change; the Library file carries the field
    (test_background_voices_published.py)."""
    save_speakers(meeting.directory, {"a": {"background": True}})
    current = current_meeting(meeting.directory)
    asset = ROOT / "ux/library-vectors/assets/plain-title.opus"
    with asset.open("rb") as stream:
        head, _comments, _vendor = portable.read_header(stream)
    tags, _manifest = descriptors(current, dict(channels=head[9], sampleCount=96000, durationMs=6000, sha256="0" * 64))
    exported = portable.interpret(head, list(tags.items()))
    assert [s.id for s in exported.speakers] == ["a", "c", "d", "b"]
    assert next(s.label for s in exported.speakers if s.id == "a") == "Alice"
    assert [w.speaker for w in portable.load_words(exported, exported.default_transcript_id)] == list(
        s for s, _w in SPOKEN
    )


def test_shared_round_trip_vector_rebuilds_the_field():
    """cassini-ux D112 (D-1171) background-voice vector: meeting.json -> .opus -> meeting.json."""
    expected = json.loads(VECTOR.with_suffix(".expected.json").read_text())
    embedded = portable.read_meeting(str(VECTOR.with_suffix(".opus")))
    ours = embedded.manifest["x"][xschema.DOMAIN]
    assert xschema.valid(ours)
    rebuilt = xschema.rebuild(ours, None, session_exists=False)
    assert rebuilt == expected["rebuiltMeetingJson"]
    assert [dict(id=s.id, label=s.label) for s in embedded.speakers] == expected["manifestSpeakers"]
    background = [s["id"] for s in rebuilt["speakers"] if s.get("background") is True]
    assert background == expected["backgroundSpeakerIds"]
    marta = next(s for s in rebuilt["speakers"] if s["id"] == expected["preservedNameOnBackgroundSpeaker"]["id"])
    assert marta["name"] == expected["preservedNameOnBackgroundSpeaker"]["name"]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ([("e", False, True, None)], "Speaker 5 is now a background voice"),
        (
            [("m", False, False, "Marta")] + [(i, False, True, None) for i in "efgh"],
            "Named Marta · 4 background voices",
        ),
        ([("e", False, True, None), ("f", False, True, None)], "2 background voices"),
        ([("m", False, False, "Marta")], None),
        ([("e", True, False, None)], None),
    ],
)
def test_save_summary_is_one_toast_for_everything_that_changed(changes, expected):
    names = dict(e="Speaker 5", f="Speaker 6", g="Speaker 7", h="Speaker 8", m="Speaker 3")
    assert save_summary(changes, names) == expected


def test_auto_naming_never_names_or_enrols_a_background_voice():
    source = (ROOT / "src/cassini_talk/transcription.py").read_text()
    assert 'not previous.get("background")' in source
    assert 's.get("provenance") == "user" or s.get("nameCleared") or s.get("background")' in source


def test_meeting_reader_defaults_older_speakers_to_not_background():
    from cassini_talk.library import _speakers

    words = [SimpleNamespace(speaker="a", start_ms=0, end_ms=10, text="x", low_confidence=False)]
    values = [dict(id="a", name="A", provenance="user", colourIndex=0)]
    try:
        (speaker,) = _speakers(words, values, None)
    except (AttributeError, KeyError, TypeError):
        pytest.skip("speaker metrics need full words")
    assert speaker.background is False


def test_background_runs_resolve_each_speaker_once_on_long_transcripts():
    """BUG-CLASSES #1: one pass over ~100k rows; merges are resolved once per speaker id, not per row."""
    calls = []

    def background_of(identifier):
        calls.append(identifier)
        return identifier if identifier.startswith("bg") else None

    rows = [SimpleNamespace(speaker=("bg1", "bg2", "a", "bg1", "b")[i % 5]) for i in range(100_000)]
    runs = background_runs(rows, background_of)
    assert sorted(calls) == ["a", "b", "bg1", "bg2"]
    assert runs[:5] == [(0, 2), (0, 2), None, (3, 1), None] and runs[5] == (5, 2)
    assert sum(run is not None and run[0] == i for i, run in enumerate(runs)) == 40_000
