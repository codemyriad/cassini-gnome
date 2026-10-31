# SPDX-License-Identifier: GPL-3.0-or-later
"""D24 name/merge/Undo persistence, malicious histories and resolved consumers."""

import hashlib
from contextlib import closing
from pathlib import Path

import pytest

from cassini_talk import portable
from cassini_talk.export_model import markdown, model_projection
from cassini_talk.exporting import export_meeting
from cassini_talk.library import LibraryScanner, _speakers
from cassini_talk.meeting_store import atomic_json, read_metadata, update_metadata
from cassini_talk.portable_writer import descriptors
from cassini_talk.recording_session import RecordingSession
from cassini_talk.search_index import SearchIndex
from cassini_talk.speaker_edits import current_meeting, merge_into, name_value, save_name, undo_merge
from cassini_talk.speaker_merges import apply_merges, resolve_merges, speaker_metrics


@pytest.fixture
def meeting(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    session = RecordingSession(root, source="gnome")
    session.add_track("system", None, "system", 16000, 1)
    session.append("system", b"\x01\x00" * 48000, 0)
    session.finish()
    speakers = [
        dict(id=identifier, name=name, provenance="user", colourIndex=index, private="keep")
        for index, (identifier, name) in enumerate((("a", "Alice"), ("b", "Bob"), ("c", "Carol")))
    ]
    words = [
        dict(speaker=s, startMs=i * 500, endMs=i * 500 + 400, text=word, lowConfidenceSpeaker=i == 1)
        for i, (s, word) in enumerate((("a", "alpha"), ("b", "bravo"), ("c", "charlie")))
    ]
    atomic_json(session.directory / "transcript.json", dict(format="cassini.words.v1", language="en", items=words))
    update_metadata(
        session.directory,
        speakers=speakers,
        notes=[dict(atMs=0, text="kept")],
        private="untouched",
        transcript=dict(
            status="done", reference=dict(kind="sidecar", file="transcript.json", format="cassini.words.v1")
        ),
    )
    return current_meeting(session.directory)


def hashes(directory):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.name not in ("meeting.json", ".meeting.lock")
    }


def test_trimmed_name_clear_and_revision_preserve_other_fields(meeting):
    before = hashes(meeting.directory)
    revision = read_metadata(meeting.directory)["revision"]
    saved = save_name(meeting.directory, "a", "  Alicia  ")
    assert saved["revision"] == revision + 1 and saved["version"] == 1
    assert saved["speakers"][0] == dict(id="a", name="Alicia", provenance="user", colourIndex=0, private="keep")
    assert saved["notes"] == [dict(atMs=0, text="kept")] and saved["private"] == "untouched"
    assert current_meeting(meeting.directory).speaker_names[0] == "Alicia"
    cleared = save_name(meeting.directory, "a", "   ")
    assert cleared["revision"] == revision + 2
    assert cleared["speakers"][0]["name"] is None and cleared["speakers"][0]["provenance"] == "unnamed"
    assert current_meeting(meeting.directory).speaker_names[0] == "Speaker 1"
    assert cleared["speakers"][0]["nameCleared"] is True
    renamed = save_name(meeting.directory, "a", "Alicia")
    assert "nameCleared" not in renamed["speakers"][0]
    assert hashes(meeting.directory) == before


@pytest.mark.parametrize("value", ["x" * 65, "x\ny", "x\ty", "x\x00y", "x\u202ey"])
def test_invalid_name_never_writes(meeting, value):
    before = (meeting.directory / "meeting.json").read_bytes()
    with pytest.raises(ValueError):
        save_name(meeting.directory, "a", value)
    assert (meeting.directory / "meeting.json").read_bytes() == before
    assert name_value("名" * 64) == "名" * 64


def test_source_names_can_be_changed_too(meeting):
    value = read_metadata(meeting.directory)
    value["speakers"][0].update(name="Me", provenance="source")
    update_metadata(meeting.directory, speakers=value["speakers"])
    assert save_name(meeting.directory, "a", "New name")["speakers"][0]["provenance"] == "user"


def test_chained_history_undo_and_hidden_speaker_guards(meeting):
    original = (meeting.directory / "transcript.json").read_bytes()
    before = hashes(meeting.directory)
    merge_into(meeting.directory, "a", "b")
    assert [w.speaker for w in current_meeting(meeting.directory).words] == ["b", "b", "c"]
    merged = merge_into(meeting.directory, "b", "c")
    assert merged["speakerMerges"] == [{"from": "a", "into": "b"}, {"from": "b", "into": "c"}]
    assert current_meeting(meeting.directory).speaker_names == ("Carol",)
    for source, target in (("a", "c"), ("c", "a"), ("c", "c")):
        with pytest.raises(ValueError):
            merge_into(meeting.directory, source, target)
    with pytest.raises(ValueError):
        save_name(meeting.directory, "a", "Hidden")
    undo_merge(meeting.directory)
    restored = current_meeting(meeting.directory)
    assert [w.speaker for w in restored.words] == ["b", "b", "c"]
    assert restored.speaker_names == ("Bob", "Carol")
    undo_merge(meeting.directory)
    assert current_meeting(meeting.directory).speaker_names == ("Alice", "Bob", "Carol")
    assert (meeting.directory / "transcript.json").read_bytes() == original and hashes(meeting.directory) == before


def test_reader_ignores_unknown_ids_self_edges_and_cycles(meeting):
    history = [
        {"from": "a", "into": "b"},
        {"from": "b", "into": "a"},
        {"from": "unknown", "into": "c"},
        {"from": "c", "into": "unknown"},
        {"from": "c", "into": "c"},
    ]
    assert resolve_merges(history, {"a", "b", "c"}) == {"a": "b"}
    update_metadata(meeting.directory, speakerMerges=history)
    result = current_meeting(meeting.directory)
    assert [w.speaker for w in result.words] == ["b", "b", "c"]
    assert result.words[1].low_confidence
    assert not resolve_merges(
        [{"from": "a", "into": "b"}, {"from": "b", "into": "c"}, {"from": "c", "into": "a"}], set()
    )


def test_metrics_union_overlap_and_choose_longest_consecutive_turn():
    words = (
        portable.Word("a", 0, 1000, "one"),
        portable.Word("a", 500, 1500, "two"),
        portable.Word("b", 1600, 2000, "interruption"),
        portable.Word("a", 2000, 16000, "long"),
        portable.Word("a", 17000, 19000, "tail"),
    )
    metrics = speaker_metrics(words)
    assert metrics["a"] == dict(talkMs=17500, sample=(2000, 12000))
    assert metrics["b"] == dict(talkMs=400, sample=(1600, 2000))
    assert speaker_metrics([portable.Word("a", 0, 0, "zero")])["a"] == dict(talkMs=0, sample=None)
    assert apply_merges(words, [], ()) is words


def test_explicit_unnamed_overrides_received_label_but_absent_entry_inherits():
    embedded = type("Embedded", (), {"speakers": [portable.Speaker("a", "Embedded Alice")]})()
    words = [portable.Word("a", 0, 100, "Hello")]
    assert _speakers(words, [], embedded)[0].name == "Embedded Alice"
    assert _speakers(words, [dict(id="a", name=None, provenance="unnamed")], embedded)[0].name == "Speaker 1"


def test_resolved_names_and_merges_reach_search_and_both_exports(meeting, tmp_path):
    before = hashes(meeting.directory)
    merge_into(meeting.directory, "a", "b")
    save_name(meeting.directory, "b", "Robert")
    current = current_meeting(meeting.directory)
    body = markdown(model_projection(current))
    assert "Robert" in body and "Alice" not in body and "Bob" not in body
    with closing(SearchIndex(meeting.directory.parent, tmp_path / "search")) as index:
        index.sync(LibraryScanner(meeting.directory.parent).scan())
        hits = index.query("alpha", speaker="Robert", now_ms=0, tz="UTC")
        assert len(hits) == 1 and not index.query("alpha", speaker="Alice", now_ms=0, tz="UTC")
    # Descriptor/word consumers need no GStreamer in the core profile.
    asset = Path(__file__).parents[1] / "ux/library-vectors/assets/plain-title.opus"
    with asset.open("rb") as stream:
        head, _comments, _vendor = portable.read_header(stream)
    tags, _manifest = descriptors(current, dict(channels=head[9], sampleCount=144000, durationMs=3000, sha256="0" * 64))
    exported = portable.interpret(head, list(tags.items()))
    assert {s.id: s.label for s in exported.speakers} == {"b": "Robert", "c": "Carol"}
    assert [w.speaker for w in portable.load_words(exported, exported.default_transcript_id)] == ["b", "b", "c"]
    assert hashes(meeting.directory) == before


def test_resolved_names_and_merges_survive_actual_audio_export(meeting, tmp_path):
    pytest.importorskip("gi")  # Actual PCM encoding runs in GTK-equipped source/package profiles.
    before = hashes(meeting.directory)
    merge_into(meeting.directory, "a", "b")
    save_name(meeting.directory, "b", "Robert")
    current = current_meeting(meeting.directory)
    output = tmp_path / "Export"
    output.mkdir()
    path = export_meeting(current, output)
    exported = portable.read_meeting(str(path))
    assert {s.id: s.label for s in exported.speakers} == {"b": "Robert", "c": "Carol"}
    words = portable.load_words(exported, exported.default_transcript_id)
    assert [w.speaker for w in words] == ["b", "b", "c"]
    assert hashes(meeting.directory) == before
    assert not (output / "speaker-turns.json").exists()


def test_outside_and_symlink_folders_are_never_edited(meeting, tmp_path, monkeypatch):
    import shutil

    outside = tmp_path / "outside" / meeting.directory.name
    outside.parent.mkdir()
    shutil.copytree(meeting.directory, outside)
    before = hashes(outside), (outside / "meeting.json").read_bytes()
    link = meeting.directory.parent / ("Recording-" + "b" * 8 + "-0000-4000-8000-000000000001")
    link.symlink_to(outside, target_is_directory=True)
    for directory in (outside, link):
        with pytest.raises(ValueError):
            save_name(directory, "a", "Never written")
        with pytest.raises(ValueError):
            merge_into(directory, "a", "b")
        with pytest.raises(ValueError):
            undo_merge(directory)
    assert (hashes(outside), (outside / "meeting.json").read_bytes()) == before


def test_received_name_clear_uses_the_retained_scan_cache_without_rehashing(meeting, monkeypatch):
    from cassini_talk import library
    from cassini_talk.received import import_received

    source = Path(__file__).parents[1] / "ux/library-vectors/assets/onboarding.opus"
    imported = import_received(source, meeting.directory.parent)
    scanner = LibraryScanner(meeting.directory.parent)
    current = next(m for m in scanner.scan().meetings if m.directory == imported.directory)
    identifier = current.speakers[0].id
    assert current.speakers[0].name == "Silvio"
    before = hashes(imported.directory)

    def unchanged_audio_must_stay_cached(*_args):
        pytest.fail("A speaker metadata edit re-read/re-verified unchanged received audio")

    monkeypatch.setattr(library, "_opus", unchanged_audio_must_stay_cached)
    save_name(imported.directory, identifier, "New name", scanner=scanner)
    updated = next(
        m for m in scanner.scan(changed_directories={imported.directory}).meetings if m.directory == imported.directory
    )
    assert updated.speakers[0].name == "New name" and updated.speakers[0].provenance == "user"
    save_name(imported.directory, identifier, "", scanner=scanner)
    cleared = next(
        m for m in scanner.scan(changed_directories={imported.directory}).meetings if m.directory == imported.directory
    )
    assert cleared.speakers[0].name == "Speaker 1" and cleared.speakers[0].provenance == "unnamed"
    assert hashes(imported.directory) == before


@pytest.mark.parametrize(
    "value,const,expected",
    [(True, True, True), (False, True, False), (1, True, False), (True, 1, False), (1.0, 1, True)],
)
def test_schema_boolean_constants_are_distinct_from_numbers(value, const, expected):
    from cassini_talk.library_schema import valid

    assert valid(value, {"const": const}) is expected
