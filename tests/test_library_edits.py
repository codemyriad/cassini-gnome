# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime-dependent fixtures follow explicit dependency checks.
"""App integration preserves file truth and routes mutations through one journal."""

import threading
from concurrent.futures import CancelledError

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication tests require GStreamer", allow_module_level=True)

from library_v2_support import simple_session

from cassini_viewer import meeting_store, portable
from cassini_viewer.library import LibraryScanner
from cassini_viewer.library_edits import commit_metadata, transaction
from cassini_viewer.library_v2 import finalize, journal, publish, xschema


@pytest.fixture
def published(tmp_path):
    sid, directory = simple_session(tmp_path, 0.1)
    result = finalize.finalize(tmp_path, sid, zone="Europe/Rome")
    return tmp_path, sid, directory, tmp_path / result.name


def commit(directory, **changes):
    with transaction(directory):
        return commit_metadata(directory, meeting_store.read_metadata(directory), changes)


def audio_digest(path):
    with path.open("rb") as stream:
        return portable.compute_audio_digest(stream)["sha256"]


def test_app_title_and_notes_are_one_journaled_file_edit(published):
    root, sid, directory, original = published
    digest = audio_digest(original)
    changes = dict(title="Changed title", titleSource="user", notes=[dict(atMs=5, text="Keep notes")])
    updated = commit(directory, **changes)
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.title == "Changed title" and "Changed title" in meeting.audio_file.name
    assert not original.exists() and len(list(root.glob("*.opus"))) == 1
    assert audio_digest(meeting.audio_file) == digest
    embedded = portable.read_meeting(str(meeting.audio_file))
    ours = embedded.manifest["x"][xschema.DOMAIN]
    assert ours["revision"] == updated["revision"] == meeting_store.read_metadata(directory)["revision"]
    assert ours["meeting"]["title"] == "Changed title"
    assert embedded.manifest["x"][xschema.NOTES_DOMAIN]["notes"] == changes["notes"]
    assert not journal.pending(journal.Layout(root)).entries


def test_completed_words_and_metadata_commit_together_with_original_speaker_ids(published):
    root, sid, directory, original = published
    digest = audio_digest(original)
    body = dict(
        format=portable.WORDS_FORMAT,
        language="en",
        items=[
            dict(speaker="original-a", startMs=0, endMs=40, text="first"),
            dict(speaker="original-b", startMs=40, endMs=90, text="second", lowConfidenceSpeaker=True),
        ],
    )
    speakers = [
        dict(id=s, name=name, provenance="user", colourIndex=i)
        for i, (s, name) in enumerate([("original-a", "Alice"), ("original-b", "Bob")])
    ]
    changes = dict(
        speakers=speakers,
        transcript=dict(status="done", reference=None),
        speakerMerges=[{"from": "original-a", "into": "original-b"}],
    )
    with transaction(directory):
        commit_metadata(directory, meeting_store.read_metadata(directory), changes, words=body)
    embedded = portable.read_meeting(str(original))
    assert audio_digest(original) == digest
    assert [w.speaker for w in portable.load_words(embedded, embedded.default_transcript_id)] == [
        "original-a",
        "original-b",
    ]
    assert portable.load_words(embedded, embedded.default_transcript_id)[1].low_confidence
    assert embedded.manifest["x"][xschema.DOMAIN]["meeting"]["transcript"]["status"] == "done"
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.transcript == "done" and [w.speaker for w in meeting.words] == ["original-b", "original-b"]
    # Removing history restores the originals; it never rewrites or flattens words.
    commit(directory, speakerMerges=[])
    (restored,) = LibraryScanner(root).scan().meetings
    assert [w.speaker for w in restored.words] == ["original-a", "original-b"]
    assert [s.name for s in restored.speakers] == ["Alice", "Bob"]


def test_nested_same_root_transaction_and_concurrent_independent_edits(published):
    root, sid, directory, original = published
    errors = []
    gate = threading.Barrier(2)

    def change(changes):
        try:
            gate.wait(timeout=3)
            with transaction(directory), transaction(directory):
                commit_metadata(directory, meeting_store.read_metadata(directory), changes)
        except Exception as error:
            errors.append(error)

    workers = [
        threading.Thread(target=change, args=(changes,), daemon=True)
        for changes in (
            dict(title="Concurrent title", titleSource="user"),
            dict(notes=[dict(atMs=0, text="Concurrent note")]),
        )
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert not any(worker.is_alive() for worker in workers), "Nested filesystem locks deadlocked"
    assert not errors
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.title == "Concurrent title" and meeting.notes[0]["text"] == "Concurrent note"


def test_cancel_immediately_before_commit_keeps_previous_file_and_metadata(published):
    root, sid, directory, original = published
    before = original.read_bytes(), (directory / "meeting.json").read_bytes()
    checks = []

    def active():
        checks.append(True)
        if len(checks) == 2:
            raise CancelledError()

    with transaction(directory), pytest.raises(CancelledError):
        commit_metadata(
            directory, meeting_store.read_metadata(directory), dict(title="Cancelled"), before_commit=active
        )
    assert (original.read_bytes(), (directory / "meeting.json").read_bytes()) == before
    assert not journal.pending(journal.Layout(root)).entries
    assert list(journal.Layout(root).staging.iterdir()) == []


def test_missing_published_file_is_never_reconstructed_by_an_edit(published):
    root, sid, directory, original = published
    original.unlink()
    before = (directory / "meeting.json").read_bytes()
    with pytest.raises(FileNotFoundError):
        commit(directory, title="Do not republish")
    assert (directory / "meeting.json").read_bytes() == before
    assert not list(root.glob("*.opus"))


def test_transaction_is_required_and_identity_members_cannot_be_changed(published):
    root, sid, directory, original = published
    current = meeting_store.read_metadata(directory)
    with pytest.raises(RuntimeError, match="transaction"):
        commit_metadata(directory, current, dict(title="Unprotected"))
    with transaction(directory), pytest.raises(ValueError, match="Identity"):
        commit_metadata(directory, current, dict(revision=100))


def test_edit_preserves_foreign_x_alternate_body_unknown_tags_and_vendor(published):
    from cassini_viewer.library_v2 import edit
    from cassini_viewer.portable_writer import MANIFEST_MIME, WORDS_MIME, chunk_set

    root, sid, directory, original = published
    manifest = portable.read_meeting(str(original)).manifest
    manifest["x"]["future.example"] = dict(opaque=[1, "keep", None])
    manifest["x"][xschema.DOMAIN]["future"] = {"keep": True}
    alt_tags, reference = chunk_set(
        "CASSINI_TX_ALTERNATE_PAYLOAD_",
        dict(format=portable.WORDS_FORMAT, wordCount=0, items=[], opaque="preserve body"),
        WORDS_MIME,
    )
    manifest["transcripts"].append(
        dict(id="alternate", default=False, format=portable.WORDS_FORMAT, wordCount=0, payloadRef=reference)
    )
    replacements, _ = chunk_set(edit.MANIFEST_PREFIX, manifest, MANIFEST_MIME)
    replacements.update(alt_tags)
    replacements["CASSINI_TRANSCRIPT_IDS"] = "words,alternate"
    with original.open("rb") as stream:
        vendor, fields = edit.comment_fields(stream)
    opaque = b"X-FUTURE=\xff\x00keep exact bytes"
    fields = edit.edited_fields(fields, replacements) + [opaque]
    stage = original.with_suffix(".test-stage")
    edit.rewrite(original, stage, fields, vendor)
    edit.verify(stage)
    stage.replace(original)
    digest = audio_digest(original)
    commit(directory, quoteOverride=dict(atMs=0, text="Preserve foreign data"))
    manifest = portable.read_meeting(str(original)).manifest
    assert manifest["x"]["future.example"] == dict(opaque=[1, "keep", None])
    assert manifest["x"][xschema.DOMAIN]["future"] == {"keep": True}
    assert manifest["transcripts"][1]["payloadRef"] == reference
    with original.open("rb") as stream:
        new_vendor, new_fields = edit.comment_fields(stream)
    assert new_vendor == vendor and opaque in new_fields
    for tag, value in alt_tags.items():
        assert f"{tag}={value}".encode() in new_fields
    assert audio_digest(original) == digest


def test_file_truth_and_recovery_after_transcript_replacement_before_metadata(published, monkeypatch):
    root, sid, directory, original = published
    before = meeting_store.read_metadata(directory)

    def die(point):
        if point == "replaced":
            raise SystemExit("synthetic process exit after file publication")

    monkeypatch.setattr(publish, "CHECKPOINT", die)
    body = dict(format=portable.WORDS_FORMAT, items=[dict(speaker="s", startMs=0, endMs=90, text="Kept")])
    with transaction(directory), pytest.raises(SystemExit):
        commit_metadata(directory, before, dict(transcript=dict(status="done", reference=None)), words=body)
    assert meeting_store.read_metadata(directory) == before
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.transcript == "done" and meeting.words[0].text == "Kept"
    assert len(journal.pending(journal.Layout(root)).entries) == 1
    monkeypatch.setattr(publish, "CHECKPOINT", None)
    report = journal.recover(root)
    assert not report.kept and not journal.pending(journal.Layout(root)).entries
    assert meeting_store.read_metadata(directory)["transcript"]["status"] == "done"
    assert len(list(root.glob("*.opus"))) == 1


def test_existing_transcription_queue_commits_words_after_concurrent_title_edit(published, monkeypatch):
    from cassini_viewer.transcription import TranscriptionQueue

    root, sid, directory, original = published
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "private-config"))
    progress_written, finish_job, completed = threading.Event(), threading.Event(), threading.Event()
    original_digest = audio_digest(original)
    pcm = {p.name: p.read_bytes() for p in directory.iterdir() if p.suffix == ".wav" or p.name == "session.json"}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return self

        def close(self):
            finish_job.set()

        def process_tracks(self, tracks, progress):
            progress(
                dict(
                    type="progress",
                    trackId=tracks[0]["id"],
                    trackOrder=0,
                    language="en",
                    etaMinutes=0,
                    words=[dict(startMs=0, endMs=90, text="Published words")],
                )
            )
            progress_written.set()
            assert finish_job.wait(5), "Test did not release recognition worker"
            return dict(diagnostics=[])

    queue = TranscriptionQueue(
        lambda _directory: None, lambda _directory: completed.set(), root=root, bundle=root, client_factory=Client
    )
    try:
        assert queue.enqueue(directory)
        assert progress_written.wait(5)
        # Progress is transient: it does not rewrite all Opus headers/words for
        # each segment. User edits meanwhile still commit through the journal.
        assert original.exists()
        assert portable.load_words(portable.read_meeting(str(original)), "words") == []
        meeting_store.update_metadata(directory, title="Typed while writing", titleSource="user")
        finish_job.set()
        assert completed.wait(5)
        (meeting,) = LibraryScanner(root).scan().meetings
        assert meeting.transcript == "done" and meeting.title == "Typed while writing"
        assert [w.text for w in meeting.words] == ["Published words"]
        assert meeting.audio_file.name != original.name
        assert audio_digest(meeting.audio_file) == original_digest
        assert all((directory / name).read_bytes() == data for name, data in pcm.items())
        assert not journal.pending(journal.Layout(root)).entries
    finally:
        finish_job.set()
        queue.close()


def test_file_only_metadata_edit_does_not_create_a_working_folder(published, monkeypatch):
    import shutil

    root, sid, directory, original = published
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "private-config"))
    shutil.rmtree(directory)
    digest = audio_digest(original)
    from cassini_viewer.meeting_rename import rename_meeting

    updated = rename_meeting(directory, "Standalone file title")
    (meeting,) = LibraryScanner(root).scan().meetings
    assert meeting.title == "Standalone file title" and meeting.metadata["revision"] == updated["revision"]
    assert not directory.exists()
    assert audio_digest(meeting.audio_file) == digest
    assert meeting_store.read_metadata(directory)["title"] == "Standalone file title"


def test_export_reuses_published_audio_without_tracks_and_preserves_recording_date(published, monkeypatch):
    import shutil

    from cassini_viewer.exporting import export_meeting

    root, sid, directory, original = published
    source = portable.read_meeting(str(original))
    source_digest = audio_digest(original)
    shutil.rmtree(directory)
    (meeting,) = LibraryScanner(root).scan().meetings

    def no_encoder(*_args, **_kwargs):
        raise AssertionError("Published audio was re-encoded instead of reused")

    monkeypatch.setattr("cassini_viewer.exporting.encode_pcm", no_encoder)
    exported = export_meeting(meeting, root / "Documents", tz="Asia/Tokyo")
    result = portable.read_meeting(str(exported))
    assert result.is_readable and audio_digest(exported) == source_digest
    assert result.tags["DATE"] == source.tags["DATE"]
    assert result.recorded_at_local == source.recorded_at_local
    assert result.tags["CASSINI_CREATED_AT"] == result.manifest["meeting"]["createdAtUtc"]
    assert result.manifest["audioEncode"] == source.manifest["audioEncode"]
    assert not directory.exists()


def test_published_quotes_and_merged_labels_are_file_content_and_undo_restores_names(published):
    root, _sid, directory, path = published
    body = dict(
        format=portable.WORDS_FORMAT,
        items=[
            dict(speaker="a", startMs=0, endMs=20, text='"hello"', future=7),
            dict(speaker="b", startMs=20, endMs=40, text='unpaired"'),
        ],
    )
    speakers = [
        dict(id=i, name=n, provenance="user", colourIndex=j) for j, (i, n) in enumerate([("a", "Alice"), ("b", "Bob")])
    ]
    digest = audio_digest(path)
    with transaction(directory):
        commit_metadata(
            directory,
            meeting_store.read_metadata(directory),
            dict(
                speakers=speakers,
                transcript=dict(status="done", reference=None),
                speakerMerges=[{"from": "a", "into": "b"}],
            ),
            words=body,
        )
    embedded = portable.read_meeting(str(path))
    assert {speaker.id: speaker.label for speaker in embedded.speakers} == {"a": "Bob", "b": "Bob"}
    words = portable.load_words(embedded, embedded.default_transcript_id)
    assert [(w.speaker, w.text) for w in words] == [("a", "“hello”"), ("b", "unpaired")]
    assert audio_digest(path) == digest
    commit(directory, speakerMerges=[])
    embedded = portable.read_meeting(str(path))
    assert {speaker.id: speaker.label for speaker in embedded.speakers} == {"a": "Alice", "b": "Bob"}
    assert audio_digest(path) == digest


def test_root_file_uses_its_stored_label_even_if_it_looks_like_an_unnamed_rank(published):
    from types import SimpleNamespace

    from cassini_viewer.library import _speakers

    words = [portable.Word("a", 0, 10, "hello")]
    embedded = SimpleNamespace(speakers=[portable.Speaker("a", "Speaker 19")])
    local = [dict(id="a", name=None, provenance="unnamed", colourIndex=0)]
    assert _speakers(words, local, embedded, stored_labels=True)[0].name == "Speaker 19"


def test_unrelated_edit_never_relabels_a_stored_unnamed_voice(published):
    from cassini_viewer.library_v2 import edit
    from cassini_viewer.portable_writer import MANIFEST_MIME, chunk_set

    _root, _sid, directory, path = published
    body = dict(format=portable.WORDS_FORMAT, items=[dict(speaker="a", startMs=0, endMs=20, text="hello")])
    with transaction(directory):
        commit_metadata(
            directory,
            meeting_store.read_metadata(directory),
            dict(
                speakers=[dict(id="a", name=None, provenance="unnamed", colourIndex=0)],
                transcript=dict(status="done", reference=None),
            ),
            words=body,
        )
    manifest = portable.read_meeting(str(path)).manifest
    manifest["speakers"][0]["label"] = "Parlante 19"
    tags, _ = chunk_set(edit.MANIFEST_PREFIX, manifest, MANIFEST_MIME)
    with path.open("rb") as stream:
        vendor, fields = edit.comment_fields(stream)
    fields = edit.edited_fields(fields, tags, chunk_prefixes=(edit.MANIFEST_PREFIX,))
    stage = path.with_suffix(".test-stage")
    edit.rewrite(path, stage, fields, vendor)
    edit.verify(stage)
    stage.replace(path)
    digest = audio_digest(path)
    commit(directory, notes=[dict(atMs=0, text="A note")])
    assert portable.read_meeting(str(path)).speakers[0].label == "Parlante 19"
    assert audio_digest(path) == digest


def test_share_has_resolved_words_without_library_ownership_or_unusable_undo_history(published, tmp_path):
    from cassini_viewer.exporting import export_meeting

    root, _sid, directory, original = published
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
            dict(
                speakers=speakers,
                speakerMerges=[{"from": "a", "into": "b"}],
                notes=[dict(atMs=0, text="Keep this note")],
                transcript=dict(status="done", reference=None),
            ),
            words=body,
        )
    (meeting,) = LibraryScanner(root).scan().meetings
    before = original.read_bytes()
    output = tmp_path / "shared"
    export_meeting(meeting, output, "cassini")
    (share,) = output.glob("*.opus")
    embedded = portable.read_meeting(str(share))
    assert "codemyriad.io" not in embedded.manifest.get("x", {})
    assert embedded.manifest["x"]["gocassini.com"]["notes"] == [dict(atMs=0, text="Keep this note")]
    assert {w.speaker for w in portable.load_words(embedded, embedded.default_transcript_id)} == {"b"}
    assert original.read_bytes() == before
    assert audio_digest(share) == audio_digest(original)
    commit(directory, speakerMerges=[])
    (restored,) = LibraryScanner(root).scan().meetings
    assert {w.speaker for w in restored.words} == {"a", "b"}


def _replace_stored_labels(path, labels):
    from cassini_viewer.library_v2 import edit
    from cassini_viewer.portable_writer import MANIFEST_MIME, chunk_set

    manifest = portable.read_meeting(str(path)).manifest
    for entry in manifest["speakers"]:
        entry["label"] = labels[entry["id"]]
    tags, _ = chunk_set(edit.MANIFEST_PREFIX, manifest, MANIFEST_MIME)
    with path.open("rb") as stream:
        vendor, fields = edit.comment_fields(stream)
    fields = edit.edited_fields(fields, tags, chunk_prefixes=(edit.MANIFEST_PREFIX,))
    stage = path.with_suffix(".test-stage")
    edit.rewrite(path, stage, fields, vendor)
    edit.verify(stage)
    stage.replace(path)


@pytest.mark.parametrize("edit_kind", ["name", "clear", "colour", "merge-other"])
def test_edit_preserves_other_speakers_stored_unnamed_labels_across_locales(published, monkeypatch, edit_kind):
    from cassini_viewer import library

    root, _sid, directory, path = published
    speakers = [dict(id=s, name=None, provenance="unnamed", colourIndex=i) for i, s in enumerate("abc")]
    body = dict(
        format=portable.WORDS_FORMAT,
        items=[dict(speaker=s, startMs=i * 20, endMs=i * 20 + 20, text=s) for i, s in enumerate("abc")],
    )
    with transaction(directory):
        commit_metadata(
            directory,
            meeting_store.read_metadata(directory),
            dict(speakers=speakers, transcript=dict(status="done", reference=None)),
            words=body,
        )
    _replace_stored_labels(path, dict(a="Parlante 19", b="Parlante 20", c="Parlante 21"))
    digest = audio_digest(path)
    real_text = library.text
    monkeypatch.setattr(
        library,
        "text",
        lambda key, **args: f"Locuteur {args['n']}" if key == "common.speaker.unnamed" else real_text(key, **args),
    )
    expected = dict(a="Parlante 19", b="Parlante 20", c="Parlante 21")
    if edit_kind == "name":
        speakers[1] = dict(speakers[1], name="Beatrice", provenance="user")
        commit(directory, speakers=speakers)
        expected["b"] = "Beatrice"
    elif edit_kind == "clear":
        speakers[1] = dict(speakers[1], nameCleared=True)
        commit(directory, speakers=speakers)
        expected["b"] = "Locuteur 2"
    elif edit_kind == "colour":
        speakers[1] = dict(speakers[1], colourIndex=9)
        commit(directory, speakers=speakers)
    else:
        commit(directory, speakerMerges=[{"from": "b", "into": "c"}])
        expected["b"] = expected["c"]
    embedded = portable.read_meeting(str(path))
    assert {s.id: s.label for s in embedded.speakers} == expected
    assert audio_digest(path) == digest
    # A reader in another locale never substitutes its own unnamed labels.
    monkeypatch.setattr(
        library,
        "text",
        lambda key, **args: f"Sprecher {args['n']}" if key == "common.speaker.unnamed" else real_text(key, **args),
    )
    (meeting,) = LibraryScanner(root).scan().meetings
    assert {s.id: s.name for s in meeting.speakers} == {
        s: expected[s] for s in expected if s != "b" or edit_kind != "merge-other"
    }


def test_produced_labels_use_writer_language_and_readers_keep_stored_words_and_labels(published, monkeypatch):
    from cassini_viewer import library
    from cassini_viewer.export_model import markdown, model_projection
    from cassini_viewer.meeting_text import reading_turns
    from cassini_viewer.transcript import build_turns

    root, _sid, directory, path = published
    source = [
        dict(speaker="a", startMs=0, endMs=10, text='"Buongiorno'),
        dict(speaker="a", startMs=10, endMs=20, text='mondo"'),
        dict(speaker="b", startMs=20, endMs=30, text='orfana"'),
    ]
    real_text = library.text
    monkeypatch.setattr(
        library,
        "text",
        lambda key, **args: f"Parlante {args['n']}" if key == "common.speaker.unnamed" else real_text(key, **args),
    )
    with transaction(directory):
        commit_metadata(
            directory,
            meeting_store.read_metadata(directory),
            dict(
                speakers=[dict(id=s, name=None, provenance="unnamed", colourIndex=i) for i, s in enumerate("ab")],
                transcript=dict(status="done", reference=None),
            ),
            words=dict(format=portable.WORDS_FORMAT, items=source),
        )
    assert source[0]["text"] == '"Buongiorno'  # Writer doesn't mutate recognizer results.
    embedded = portable.read_meeting(str(path))
    assert {s.id: s.label for s in embedded.speakers} == dict(a="Parlante 1", b="Parlante 2")
    expected = ["“Buongiorno", "mondo”", "orfana"]
    assert [w.text for w in portable.load_words(embedded, embedded.default_transcript_id)] == expected
    before = path.read_bytes()
    monkeypatch.setattr(
        library,
        "text",
        lambda key, **args: f"Speaker {args['n']}" if key == "common.speaker.unnamed" else real_text(key, **args),
    )
    (meeting,) = LibraryScanner(root).scan().meetings
    assert [s.name for s in meeting.speakers] == ["Parlante 1", "Parlante 2"]
    assert [w.text for w in meeting.words] == expected and all(w.text_is_stored for w in meeting.words)
    for build in (build_turns, reading_turns):
        assert [turn.text for turn in build(list(meeting.words))] == ["“Buongiorno mondo”", "orfana"]
    output = markdown(model_projection(meeting))
    assert "Parlante 1" in output and "“Buongiorno mondo”" in output and 'orfana"' not in output
    assert path.read_bytes() == before
