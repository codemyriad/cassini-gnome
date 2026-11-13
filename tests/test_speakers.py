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


def test_loaded_listening_turns_follow_merges_and_are_cached_between_scans(meeting):
    scanner = LibraryScanner(meeting.directory.parent)
    before = scanner.scan().meetings[0]
    assert [(t.speaker, t.word_indices) for t in before.listening_turns] == [("a", (0,)), ("b", (1,)), ("c", (2,))]
    assert scanner.scan().meetings[0].listening_turns is before.listening_turns
    merge_into(meeting.directory, "b", "a")
    after = scanner.scan(changed_directories={meeting.directory}).meetings[0]
    assert [(t.speaker, t.word_indices) for t in after.listening_turns] == [("a", (0, 1)), ("c", (2,))]
    undo_merge(meeting.directory)
    restored = scanner.scan(changed_directories={meeting.directory}).meetings[0]
    assert restored.listening_turns == before.listening_turns


def test_speaker_colour_slot_stays_fixed_across_merge_and_rename(meeting):
    from cassini_talk.tokens import speaker_slot

    # 1. Merging speaker b (slot 2) into speaker a (slot 1) leaves speaker c on slot 3 (stored colourIndex 2).
    merge_into(meeting.directory, "b", "a")
    merged = current_meeting(meeting.directory)
    carol = merged.speaker_for("c")
    assert carol.colour_index == 1 and carol.stored_colour_index == 2
    assert speaker_slot(carol) == 3

    # 2. Even if meeting.json had no stored speaker entries, pre-merge first-speech rank fixes c at slot 3 on rename.
    update_metadata(meeting.directory, speakers=[])
    unnamed = current_meeting(meeting.directory)
    unnamed_c = unnamed.speaker_for("c")
    assert unnamed_c.provenance is None and unnamed_c.stored_colour_index == 2
    assert speaker_slot(unnamed_c) == 0
    save_name(meeting.directory, "c", "Carol")
    named = current_meeting(meeting.directory)
    named_c = named.speaker_for("c")
    assert named_c.stored_colour_index == 2 and speaker_slot(named_c) == 3
    stored_entry = next(s for s in read_metadata(meeting.directory)["speakers"] if s["id"] == "c")
    assert stored_entry["colourIndex"] == 2


def test_wp6_speaker_quote_duplicate_notice_markup_and_voice_initial(meeting):
    from cassini_talk import i18n
    from cassini_talk.speaker_edits import duplicate_notice_markup, speaker_quote, voice_initial

    scanned = current_meeting(meeting.directory)
    assert speaker_quote(scanned, "b", (500, 900)) == "bravo"
    assert speaker_quote(scanned, "a") == "alpha"
    assert speaker_quote(scanned, "missing") == ""

    merge_into(meeting.directory, "b", "a")
    merged = current_meeting(meeting.directory)
    assert speaker_quote(merged, "a", (500, 900)) == "alpha bravo"

    assert voice_initial("Giulia Rossi") == "G"
    assert voice_initial("  lea ") == "L"
    assert voice_initial("   ") == "?"

    assert duplicate_notice_markup("Lea") == '<span weight="600">Lea</span> is already a saved voice.'
    assert (
        duplicate_notice_markup("A & B <2>")
        == '<span weight="600">A &amp; B &lt;2&gt;</span> is already a saved voice.'
    )
    with i18n.using_language("fr"):
        assert duplicate_notice_markup("Lea") == '<span weight="600">Lea</span> fait déjà partie des voix mémorisées.'


def _quote_meeting(words, *, listening=True, rows=True, resolve=None):
    from types import SimpleNamespace

    from cassini_talk.meeting_text import reading_turns, speaker_turns

    words = tuple(words)
    meeting = SimpleNamespace(
        words=words,
        listening_turns=speaker_turns(words) if listening else (),
        turns=tuple(reading_turns(words)) if rows else (),
    )
    if resolve is not None:
        meeting.speaker_for = resolve
    return meeting


def test_wp6_speaker_quote_fallback_chain_chips_and_legacy_quotes():
    from types import SimpleNamespace

    from cassini_talk.portable import Word
    from cassini_talk.speaker_edits import speaker_quote

    words = [
        Word("a", 0, 400, "one"),
        Word("b", 500, 900, "first"),
        Word("a", 1000, 1400, "two"),
        Word("b", 1500, 1900, "second"),
    ]
    full = _quote_meeting(words)
    # The sample picks the overlapping later turn; a sample miss falls back to the first turn.
    assert speaker_quote(full, "b", (1500, 1900)) == "second"
    assert speaker_quote(full, "b", (5000, 6000)) == "first"
    assert speaker_quote(full, "b") == "first"
    # No listening turns: reading rows. No rows either: consecutive word runs.
    assert speaker_quote(_quote_meeting(words, listening=False), "b", (1500, 1900)) == "second"
    assert speaker_quote(_quote_meeting(words, listening=False), "b") == "first"
    assert speaker_quote(_quote_meeting(words, listening=False, rows=False), "b") == "first"
    assert speaker_quote(_quote_meeting(words, listening=False, rows=False), "b", (1500, 1900)) == "second"

    # A chip-only speaker lives in row.asides: quote that chip, not every interjection joined.
    chips = [
        Word("a", 0, 400, "one"),
        Word("a", 500, 900, "two"),
        Word("b", 950, 1100, "Yes."),
        Word("a", 1000, 1400, "three"),
        Word("a", 1500, 1900, "four."),
        Word("b", 1950, 2100, "Agreed."),
        Word("a", 2000, 2400, "five"),
        Word("a", 2500, 2900, "six."),
    ]
    rows = _quote_meeting(chips, listening=False)
    assert rows.turns and all(row.speaker == "a" for row in rows.turns)
    assert speaker_quote(rows, "b") == "Yes."

    # D38.5: a lone legacy orphan " displays as nothing in every fallback, so no quote.
    orphan = [Word("a", 0, 400, "hello"), Word("b", 500, 900, '"')]
    for meeting in (
        _quote_meeting(orphan),
        _quote_meeting(orphan, listening=False),
        _quote_meeting(orphan, listening=False, rows=False),
    ):
        assert speaker_quote(meeting, "b") == ""
    paired = [Word("b", 0, 400, '"Hi'), Word("b", 500, 900, 'there"')]
    assert speaker_quote(_quote_meeting(paired, listening=False, rows=False), "b") == "“Hi there”"

    # Merged raw ids resolve once each through speaker_for.
    calls = []

    def resolve(raw):
        calls.append(raw)
        return SimpleNamespace(id="a" if raw in ("a", "b") else raw)

    assert speaker_quote(_quote_meeting(words, resolve=resolve), "a") == "one"
    assert sorted(set(calls)) == sorted(calls)


def test_wp6_speaker_quote_is_bounded_and_linear_on_long_recordings():
    import time

    from cassini_talk.portable import Word
    from cassini_talk.speaker_edits import QUOTE_CHARS, speaker_quote

    def build(count):
        # One long monologue by "a", with many short alternating turns for "b".
        words = [Word("a", i * 300, i * 300 + 250, f"word{i}") for i in range(count)]
        words += [Word("b" if i % 2 else "c", (count + i) * 300, (count + i) * 300 + 250, "ok") for i in range(count)]
        return _quote_meeting(words, rows=False)

    def cpu(meeting):
        started = time.thread_time()
        for identifier in ("a", "b", "missing"):
            speaker_quote(meeting, identifier, (10**9, 10**9 + 1))
        return time.thread_time() - started

    small, large = build(25_000), build(50_000)
    line = speaker_quote(large, "a")
    assert line.startswith("word0 word1") and len(line) <= QUOTE_CHARS
    cpu(small)
    # Scaling, not wall clock (D88): doubling the input stays near-linear, and
    # 100k words cost well under a UI-frame budget per speaker (generous for CI).
    small_cpu, large_cpu = cpu(small), cpu(large)
    assert large_cpu < max(small_cpu, 0.01) * 4
    assert large_cpu < 1.5
