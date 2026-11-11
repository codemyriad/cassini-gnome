# SPDX-License-Identifier: GPL-3.0-or-later
"""D96 bulk stored-.zip export: archive invariants, round-trip dedup, bounds and cancellation."""

import hashlib
import threading
import zipfile
from concurrent.futures import CancelledError
from dataclasses import replace
from pathlib import Path

import pytest

from cassini_talk import archive_import, batch_import, companion, portable, zip_export
from cassini_talk.i18n import LANGUAGES, using_language
from cassini_talk.library import LibraryScanner, LibrarySnapshot
from cassini_talk.meeting_store import atomic_json, update_metadata
from cassini_talk.received import import_received
from cassini_talk.strings import plural, text
from cassini_talk.transcription import REFERENCE
from cassini_talk.zip_export import (
    entry_stem,
    export_meetings_zip,
    exportable_meetings,
    is_exportable,
    unique_entry_name,
)

OPUS = Path(__file__).parent / "conformance/opus"
ASSETS = Path(__file__).parents[1] / "ux/library-vectors/assets"


def test_export_zip_catalog_strings_across_all_locales():
    for lang in LANGUAGES:
        with using_language(lang):
            all_label = text("export.zip.all")
            selected_label = text("export.zip.selected")
            progress_label = text("export.zip.progress", done=2, total=5)
            done_one = plural("export.zip.done", 1)
            done_many = plural("export.zip.done", 4)
            assert all_label and "{" not in all_label
            assert selected_label and "{" not in selected_label
            assert "2" in progress_label and "5" in progress_label and "{" not in progress_label
            assert "1" in done_one and "{" not in done_one
            assert "4" in done_many and "{" not in done_many and done_many != done_one


def test_entry_naming_and_case_insensitive_collision_deduplication():
    seen = set()
    assert unique_entry_name("Budget review", seen) == "Budget review.opus"
    assert unique_entry_name("budget review", seen) == "budget review 2.opus"
    assert unique_entry_name("BUDGET REVIEW", seen) == "BUDGET REVIEW 3.opus"
    assert unique_entry_name("10:30 Standup", seen) == "10:30 Standup.opus"
    assert unique_entry_name("C: Quarterly Plan", seen) == "C- Quarterly Plan.opus"
    assert unique_entry_name(". .C: ._Quarterly Plan", seen) == "C- ._Quarterly Plan.opus"
    assert unique_entry_name(". ._SidecarLike", seen) == "_SidecarLike.opus"
    assert unique_entry_name("..hidden/path\\name\n", seen) == "hidden-path-name-.opus"
    assert unique_entry_name("...", seen) == "Untitled meeting.opus"
    assert unique_entry_name("", seen) == "Untitled meeting 2.opus"
    long_utf8 = unique_entry_name("🙂" * 120, seen)
    assert len(long_utf8.encode("utf-8")) <= 255
    for generated in (
        "Budget review.opus",
        "10:30 Standup.opus",
        entry_stem("Q: Drive prefix") + ".opus",
        long_utf8,
    ):
        archive_import._validate_entry_name(generated)


def test_exportable_meetings_excludes_live_unavailable_missing_and_trashed(tmp_path):
    lib = tmp_path / "Library"
    imported = import_received(OPUS / "001-minimal-v1.opus", lib)
    trashed = import_received(OPUS / "002-plain-audio.opus", lib)
    live = replace(imported, id="live-id", directory=lib / "Recording-live", lifecycle="recording", origin="own")
    saving = replace(imported, id="saving-id", directory=lib / "Recording-saving", lifecycle="saving", origin="own")
    no_audio = replace(imported, id="silent-id", directory=lib / "Recording-empty", has_audio=False)
    missing_pub = replace(imported, id="missing-id", directory=lib / "Recording-missing", published_missing=True)

    class FakeTrash:
        def hides(self, directory):
            return Path(directory).absolute() == trashed.directory.absolute()

    trash = FakeTrash()
    assert is_exportable(imported, trash=trash)
    assert not is_exportable(trashed, trash=trash)
    assert not is_exportable(live, trash=trash)
    assert not is_exportable(saving, trash=trash)
    assert not is_exportable(no_audio, trash=trash)
    assert not is_exportable(missing_pub, trash=trash)

    snapshot = LibrarySnapshot(meetings=(live, saving, no_audio, missing_pub, imported, trashed))
    assert exportable_meetings(snapshot, trash=trash) == (imported,)
    assert exportable_meetings(replace(snapshot, unavailable=True), trash=trash) == ()
    assert exportable_meetings(None, trash=trash) == ()


def test_stored_zip_unedited_round_trip_preserves_exact_bytes_and_deduplicates(tmp_path):
    source_lib = tmp_path / "SourceLibrary"
    one = import_received(OPUS / "001-minimal-v1.opus", source_lib)
    two = import_received(OPUS / "002-plain-audio.opus", source_lib)
    onboarding = import_received(ASSETS / "onboarding.opus", source_lib)
    snapshot = LibraryScanner(source_lib).scan()
    meetings = exportable_meetings(snapshot)
    assert len(meetings) == 3

    progress_steps = []
    out_dir = tmp_path / "Exports"
    archive_path = export_meetings_zip(
        meetings,
        out_dir,
        progress=lambda done, total: progress_steps.append((done, total)),
    )
    assert archive_path == out_dir / "Cassini.zip"
    assert progress_steps == [(1, 3), (2, 3), (3, 3)]
    assert not list(out_dir.glob(".cassini-*"))

    # Output collision publishes Cassini 2.zip without overwriting Cassini.zip.
    second_archive = export_meetings_zip(meetings[:1], out_dir)
    assert second_archive == out_dir / "Cassini 2.zip"

    with zipfile.ZipFile(archive_path) as zf:
        infos = zf.infolist()
        assert len(infos) == 3
        assert all(info.compress_type == zipfile.ZIP_STORED for info in infos)
        assert all("/" not in info.filename and "\\" not in info.filename for info in infos)
        assert all(info.filename.endswith(".opus") for info in infos)
        archive_shas = {hashlib.sha256(zf.read(info.filename)).hexdigest() for info in infos}

    expected_shas = {
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (OPUS / "001-minimal-v1.opus", OPUS / "002-plain-audio.opus", ASSETS / "onboarding.opus")
    }
    assert archive_shas == expected_shas

    # Re-importing into the same Library skips all 3 as exact-byte SHA-256 duplicates.
    same_result = batch_import.import_files([archive_path], source_lib)
    assert (same_result.imported, same_result.duplicates, same_result.failed, same_result.ignored) == (0, 3, 0, 0)

    # Importing into a fresh Library recreates all 3, keeping plain audio plain and Cassini docs verified.
    dest_lib = tmp_path / "DestLibrary"
    fresh_result = batch_import.import_files([archive_path], dest_lib)
    assert (fresh_result.imported, fresh_result.duplicates, fresh_result.failed, fresh_result.ignored) == (3, 0, 0, 0)
    reimported = LibraryScanner(dest_lib).scan().meetings
    assert len(reimported) == 3
    states = set()
    for meeting in reimported:
        parsed = portable.read_meeting(str(meeting.audio_file))
        states.add(
            parsed.state
            if parsed.state == portable.State.PLAIN_AUDIO
            else portable.verify_audio(str(meeting.audio_file), parsed).state
        )
    assert states == {portable.State.OK, portable.State.PLAIN_AUDIO}
    assert {m.id for m in (one, two, onboarding)}  # distinct source IDs


def test_edited_received_and_own_meetings_export_verified_cassini_documents_without_private_schema(tmp_path):
    source_lib = tmp_path / "SourceLibrary"
    received_doc = import_received(ASSETS / "onboarding.opus", source_lib)
    plain_doc = import_received(ASSETS / "plain-title.opus", source_lib)

    # User edits title, speaker name, and notes on received_doc.
    speaker_id = received_doc.speakers[0].id
    speakers = [dict(id=speaker_id, name="Elena", provenance="user", colourIndex=0)]
    update_metadata(
        received_doc.directory,
        title="C: Edited Onboarding",
        titleSource="user",
        speakers=speakers,
        notes=[dict(atMs=1200, text="Follow up with Elena")],
    )

    # User transcribes plain_doc locally (sidecar transcript.json).
    atomic_json(
        plain_doc.directory / "transcript.json",
        dict(
            format=portable.WORDS_FORMAT,
            language="und",
            items=[
                dict(speaker="s1", startMs=0, endMs=25, text="Hello"),
                dict(speaker="s1", startMs=30, endMs=55, text="team."),
            ],
        ),
    )
    update_metadata(
        plain_doc.directory,
        transcript=dict(status="done", reference=dict(REFERENCE)),
        speakers=[dict(id="s1", name="Marco", provenance="user", colourIndex=0)],
    )

    from cassini_talk.library_v2 import xschema

    scanned = {m.directory: m for m in LibraryScanner(source_lib).scan().meetings}
    edited_one = scanned[received_doc.directory]
    ours = xschema.private_data(
        dict(scanned[plain_doc.directory].metadata, namePrefix="2026-11-11--10.00-"),
        origin="Received",
        automatic_title="Plain",
    )
    assert xschema.valid(ours)
    edited_two = replace(
        scanned[plain_doc.directory],
        extensions={
            "codemyriad.io": ours,
            "example.org": {"keep": True},
        },
    )

    archive_path = export_meetings_zip((edited_one, edited_two), tmp_path / "Out")
    with zipfile.ZipFile(archive_path) as zf:
        names = [info.filename for info in zf.infolist()]
        assert "C- Edited Onboarding.opus" in names
        extracted_dir = tmp_path / "Extracted"
        extracted_dir.mkdir()
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_STORED
            target = extracted_dir / info.filename
            target.write_bytes(zf.read(info.filename))
            parsed = portable.read_meeting(str(target))
            assert portable.verify_audio(str(target), parsed).state == portable.State.OK
            if info.filename == "C- Edited Onboarding.opus":
                assert parsed.manifest["meeting"]["title"] == "C: Edited Onboarding"
                assert parsed.manifest["x"]["gocassini.com"]["notes"] == [dict(atMs=1200, text="Follow up with Elena")]
                assert any(s["label"] == "Elena" for s in parsed.manifest["speakers"])
            else:
                assert "codemyriad.io" not in parsed.manifest.get("x", {})
                assert parsed.manifest["x"]["example.org"] == {"keep": True}
                words = portable.load_words(parsed, parsed.default_transcript_id)
                assert [w.text for w in words] == ["Hello", "team."]


def test_single_item_staging_never_accumulates_snapshots_on_disk(tmp_path, monkeypatch):
    lib = tmp_path / "Library"
    meetings = [
        import_received(OPUS / "001-minimal-v1.opus", lib),
        import_received(OPUS / "002-plain-audio.opus", lib),
        import_received(ASSETS / "onboarding.opus", lib),
    ]
    out_dir = tmp_path / "Exports"
    observed_item_counts = []
    real_write = zip_export._write_stored_entry

    def spy_write(archive, stream, entry_name, item, size, crc, cancel):
        items = list(out_dir.glob(".cassini-item-*"))
        observed_item_counts.append(len(items))
        assert len(items) == 1 and items[0] == item
        return real_write(archive, stream, entry_name, item, size, crc, cancel)

    monkeypatch.setattr(zip_export, "_write_stored_entry", spy_write)
    archive_path = export_meetings_zip(meetings, out_dir)
    assert archive_path.is_file()
    assert observed_item_counts == [1, 1, 1]
    assert not list(out_dir.glob(".cassini-*"))


def test_entry_and_byte_limits_refuse_export_and_clean_stage(tmp_path, monkeypatch):
    lib = tmp_path / "Library"
    one = import_received(OPUS / "001-minimal-v1.opus", lib)
    two = import_received(OPUS / "002-plain-audio.opus", lib)
    out_dir = tmp_path / "Limited"

    monkeypatch.setattr(archive_import, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="2,000-entry"):
        export_meetings_zip((one, two), out_dir)
    assert not out_dir.exists() or not list(out_dir.iterdir())

    monkeypatch.setattr(archive_import, "MAX_ENTRIES", 2000)
    one_size = one.audio_file.stat().st_size
    monkeypatch.setattr(archive_import, "MAX_BYTES", one_size + 5)
    with pytest.raises(ValueError, match="20 GB"):
        export_meetings_zip((one, two), out_dir)
    assert not list(out_dir.iterdir())


def test_cancellation_mid_export_and_mid_trash_remove_incomplete_files(tmp_path, monkeypatch):
    lib = tmp_path / "Library"
    one = import_received(OPUS / "001-minimal-v1.opus", lib)
    two = import_received(OPUS / "002-plain-audio.opus", lib)
    out_dir = tmp_path / "Cancelled"
    cancel = threading.Event()
    real_write = zip_export._write_stored_entry

    def cancel_on_second(archive, stream, entry_name, item, size, crc, token):
        real_write(archive, stream, entry_name, item, size, crc, token)
        cancel.set()

    monkeypatch.setattr(zip_export, "_write_stored_entry", cancel_on_second)
    with pytest.raises(CancelledError):
        export_meetings_zip((one, two), out_dir, cancel=cancel)
    assert not list(out_dir.iterdir())

    # Mid-export trash of the second meeting aborts and cleans up the partial archive.
    monkeypatch.setattr(zip_export, "_write_stored_entry", real_write)
    trashed = set()

    def trash_second_on_progress(done, _total):
        if done == 2:
            trashed.add(two.directory.absolute())

    with pytest.raises(ValueError, match="unavailable"):
        export_meetings_zip(
            (one, two),
            out_dir,
            progress=trash_second_on_progress,
            is_trashed=lambda directory: Path(directory).absolute() in trashed,
        )
    assert not list(out_dir.iterdir())


def test_flatpak_unbacked_destination_and_nonregular_source_rejected(tmp_path, monkeypatch):
    lib = tmp_path / "Library"
    one = import_received(OPUS / "001-minimal-v1.opus", lib)
    monkeypatch.setattr(companion, "in_flatpak", lambda: True)
    from cassini_talk import library_location

    monkeypatch.setattr(library_location, "_host_storage", lambda _path: False)
    unbacked = tmp_path / "PrivateTmpfs"
    with pytest.raises(PermissionError):
        export_meetings_zip((one,), unbacked)
    assert not unbacked.exists()

    monkeypatch.setattr(companion, "in_flatpak", lambda: False)
    symlink = tmp_path / "symlink.opus"
    symlink.symlink_to(one.audio_file)
    with pytest.raises((OSError, ValueError)):
        export_meetings_zip((replace(one, audio_file=symlink),), tmp_path / "Out")
    assert not list((tmp_path / "Out").iterdir())


def test_untrusted_and_zero_word_states_preserve_original_or_seal_edits(tmp_path):
    lib = tmp_path / "Library"
    future_doc = import_received(OPUS / "015-unknown-major-version.opus", lib)
    stale_doc = import_received(OPUS / "010-stale-audio.opus", lib)
    broken_doc = import_received(OPUS / "006-missing-chunk.opus", lib)

    # Build a true 0-word verified Cassini .opus document and import it into lib.
    seed_lib = tmp_path / "SeedLibrary"
    seed_plain = import_received(OPUS / "002-plain-audio.opus", seed_lib)
    update_metadata(seed_plain.directory, title="Initial Silent Doc", titleSource="user")
    seed_scanned = LibraryScanner(seed_lib).scan().meetings[0]
    seed_zip = export_meetings_zip((seed_scanned,), tmp_path / "SeedOut")
    seed_opus = tmp_path / "Initial Silent Doc.opus"
    with zipfile.ZipFile(seed_zip) as zf:
        seed_opus.write_bytes(zf.read("Initial Silent Doc.opus"))
    zero_word_doc = import_received(seed_opus, lib)
    assert zero_word_doc.transcript == "none"

    # Unedited UNKNOWN_FORMAT, STALE_AUDIO, INVALID, and 0-word OK documents preserve exact bytes and deduplicate.
    raw_archive = export_meetings_zip((future_doc, stale_doc, broken_doc, zero_word_doc), tmp_path / "RawOut")
    with zipfile.ZipFile(raw_archive) as zf:
        raw_shas = {hashlib.sha256(zf.read(info.filename)).hexdigest() for info in zf.infolist()}
    assert raw_shas == {
        hashlib.sha256((OPUS / "015-unknown-major-version.opus").read_bytes()).hexdigest(),
        hashlib.sha256((OPUS / "010-stale-audio.opus").read_bytes()).hexdigest(),
        hashlib.sha256((OPUS / "006-missing-chunk.opus").read_bytes()).hexdigest(),
        hashlib.sha256(seed_opus.read_bytes()).hexdigest(),
    }
    dedup_result = batch_import.import_files([raw_archive], lib)
    assert (dedup_result.imported, dedup_result.duplicates, dedup_result.failed, dedup_result.ignored) == (0, 4, 0, 0)

    # Locally transcribing an INVALID document and renaming a 0-word OK document both seal verified OK documents.
    atomic_json(
        broken_doc.directory / "transcript.json",
        dict(
            format=portable.WORDS_FORMAT,
            language="en",
            items=[
                dict(speaker="s1", startMs=0, endMs=40, text="Recovered"),
                dict(speaker="s1", startMs=45, endMs=80, text="speech."),
            ],
        ),
    )
    update_metadata(
        broken_doc.directory,
        title="Recovered Meeting",
        titleSource="user",
        transcript=dict(status="done", reference=dict(REFERENCE)),
        speakers=[dict(id="s1", name="Nadia", provenance="user", colourIndex=0)],
    )
    update_metadata(
        zero_word_doc.directory,
        title="Renamed Silent Meeting",
        titleSource="user",
    )

    scanned = {m.directory: m for m in LibraryScanner(lib).scan().meetings}
    sealed_archive = export_meetings_zip(
        (scanned[broken_doc.directory], scanned[zero_word_doc.directory]),
        tmp_path / "SealedOut",
    )
    extracted = tmp_path / "SealedExtracted"
    extracted.mkdir()
    with zipfile.ZipFile(sealed_archive) as zf:
        by_name = {}
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_STORED
            target = extracted / info.filename
            target.write_bytes(zf.read(info.filename))
            parsed = portable.read_meeting(str(target))
            assert portable.verify_audio(str(target), parsed).state == portable.State.OK
            by_name[info.filename] = parsed

    recovered = by_name["Recovered Meeting.opus"]
    assert [w.text for w in portable.load_words(recovered, recovered.default_transcript_id)] == [
        "Recovered",
        "speech.",
    ]
    assert any(s["label"] == "Nadia" for s in recovered.manifest["speakers"])

    silent_renamed = by_name["Renamed Silent Meeting.opus"]
    assert silent_renamed.manifest["meeting"]["title"] == "Renamed Silent Meeting"
    assert silent_renamed.default_transcript_id is None or not portable.load_words(
        silent_renamed, silent_renamed.default_transcript_id
    )


def _write_published_v2_doc(
    root,
    filename,
    *,
    origin="Recording",
    session_id="8b7d3c2e-5f1a-4c9e-9a6b-1d2e3f4a5b6c",
    revision=2,
    title=None,
    automatic_title="Morning Sync",
    status="done",
    eta_minutes=None,
    error=None,
    words=None,
    speakers=None,
    speaker_merges=None,
    notes=None,
):
    from cassini_talk import portable_writer
    from cassini_talk.library_v2 import edit, xschema

    root.mkdir(parents=True, exist_ok=True)
    source = ASSETS / "onboarding.opus"
    with source.open("rb") as stream:
        digest = portable.compute_audio_digest(stream)
    items = (
        [
            dict(speaker="spk-a", startMs=0, endMs=40, text="First"),
            dict(speaker="spk-b", startMs=50, endMs=90, text="second"),
            dict(speaker="spk-a", startMs=100, endMs=150, text="third."),
        ]
        if words is None
        else list(words)
    )
    speaker_meta = (
        [
            dict(id="spk-a", name="Alice", provenance="user", colourIndex=0),
            dict(id="spk-b", name="Bob", provenance="user", colourIndex=1),
        ]
        if speakers is None
        else list(speakers)
    )
    merges = list(speaker_merges or [])
    from cassini_talk.speaker_merges import resolve_merges

    merge_map = resolve_merges(merges, [s["id"] for s in speaker_meta])
    label_by_id = {s["id"]: s["name"] or f"Speaker {idx + 1}" for idx, s in enumerate(speaker_meta)}
    manifest_speakers = [
        dict(id=s["id"], label=label_by_id[merge_map.get(s["id"], s["id"])])
        for s in speaker_meta
        if any(item["speaker"] == s["id"] for item in items)
    ]
    received_meta = (
        dict(
            originalName="shared.opus",
            importedAtMs=1762851600000,
            sha256="a" * 64,
        )
        if origin == "Received"
        else None
    )
    meeting_json = dict(
        version=1,
        id=session_id,
        revision=revision,
        namePrefix="2026-11-11--09.30-",
        namePrefixSource="recorded" if origin == "Recording" else "import-time",
        title=title,
        titleSource="user" if title is not None else "meeting-window",
        notes=list(notes or []),
        speakers=speaker_meta,
        transcript=dict(
            status=status,
            reference=dict(REFERENCE) if origin == "Recording" else None,
        ),
        etaMinutes=eta_minutes,
        error=error,
        quoteOverride=None,
        received=received_meta,
    )
    if merges:
        meeting_json["speakerMerges"] = merges
    ours = xschema.private_data(meeting_json, origin=origin, automatic_title=automatic_title)
    assert xschema.valid(ours)
    extension = {xschema.DOMAIN: ours}
    notes_obj = xschema.notes_domain({}, notes or [])
    if notes_obj:
        extension[xschema.NOTES_DOMAIN] = notes_obj

    displayed_title = title or automatic_title
    words_body = dict(format=portable.WORDS_FORMAT, wordCount=len(items), items=items)
    words_tags, words_ref = portable_writer.chunk_set(
        "CASSINI_TX_WORDS_PAYLOAD_",
        words_body,
        portable_writer.WORDS_MIME,
    )
    shape = dict(
        sampleRate=48000,
        channels=digest["channels"],
        sampleCount=digest["sampleCount"],
        durationMs=digest["durationMs"],
    )
    manifest = dict(
        kind="cassini-portable-meeting",
        version=1,
        profile="ogg-opus",
        meeting=dict(
            id="mtg_" + digest["sha256"],
            title=displayed_title,
            createdAtUtc="2026-11-11T08:30:00.000Z",
            recordedAtUtc="2026-11-11T08:30:00.000Z",
            durationMs=shape["durationMs"],
        ),
        audio=dict(container="ogg", codec="opus", **shape),
        integrity=dict(matchPolicy="exact-opus-audio-v1", opusAudioSha256=digest["sha256"], **shape),
        speakers=manifest_speakers,
        transcripts=[
            dict(
                id="words",
                default=True,
                format=portable.WORDS_FORMAT,
                wordCount=len(items),
                payloadRef=words_ref,
            )
        ],
        x=extension,
    )
    tags = portable_writer.file_tags(manifest, words_tags)
    dest = root / filename
    with source.open("rb") as src_stream, dest.open("wb") as dst_stream:
        portable_writer.rewrite_tags(src_stream, dst_stream, tags)
    edit.verify(dest)
    return dest, meeting_json


def test_published_library_documents_copy_verbatim_and_materialize_only_uncommitted_edits(tmp_path):
    lib = tmp_path / "Library"
    own_path, _ = _write_published_v2_doc(
        lib,
        "2026-11-11--09.30-Committed Standup.opus",
        origin="Recording",
        session_id="8b7d3c2e-5f1a-4c9e-9a6b-1d2e3f4a5b6c",
        title="Committed Standup",
        speaker_merges=[{"from": "spk-b", "into": "spk-a"}],
        notes=[dict(atMs=25, text="Already committed inside file")],
    )
    rec_path, _ = _write_published_v2_doc(
        lib,
        "2026-11-11--10.00-Published Received.opus",
        origin="Received",
        session_id="2a4b6c8d-1e3f-4a5b-8c9d-0e1f2a3b4c5d",
        automatic_title="Published Received",
    )

    scanned = {m.published_file: m for m in LibraryScanner(lib).scan().meetings}
    own_meeting = scanned[own_path]
    rec_meeting = scanned[rec_path]
    assert own_meeting.origin == "own" and "codemyriad.io" in own_meeting.extensions
    assert rec_meeting.origin == "received" and "codemyriad.io" in rec_meeting.extensions

    archive_path = export_meetings_zip((own_meeting, rec_meeting), tmp_path / "Out")
    with zipfile.ZipFile(archive_path) as zf:
        exported_own = zf.read("Committed Standup.opus")
        exported_rec = zf.read("Published Received.opus")
    assert exported_own == own_path.read_bytes()
    assert exported_rec == rec_path.read_bytes()

    # Importing into a fresh Library and re-exporting without edits remains byte-identical.
    dest_lib = tmp_path / "DestLibrary"
    imported = batch_import.import_files([archive_path], dest_lib)
    assert (imported.imported, imported.duplicates, imported.failed, imported.ignored) == (2, 0, 0, 0)
    reexported = export_meetings_zip(exportable_meetings(LibraryScanner(dest_lib).scan()), tmp_path / "Out2")
    with zipfile.ZipFile(reexported) as zf:
        assert zf.read("Committed Standup.opus") == own_path.read_bytes()
        assert zf.read("Published Received.opus") == rec_path.read_bytes()

    # An uncommitted local edit on a published meeting materializes the edit and strips stale codemyriad.io.
    draft_edited = replace(
        own_meeting,
        title="Uncommitted Draft Standup",
        notes=(dict(atMs=75, text="Uncommitted note"),),
    )
    edited_archive = export_meetings_zip((draft_edited,), tmp_path / "EditedOut")
    extracted = tmp_path / "Uncommitted Draft Standup.opus"
    with zipfile.ZipFile(edited_archive) as zf:
        extracted.write_bytes(zf.read("Uncommitted Draft Standup.opus"))
    assert extracted.read_bytes() != own_path.read_bytes()
    parsed = portable.read_meeting(str(extracted))
    assert portable.verify_audio(str(extracted), parsed).state == portable.State.OK
    assert parsed.manifest["meeting"]["title"] == "Uncommitted Draft Standup"
    assert "codemyriad.io" not in parsed.manifest.get("x", {})
    assert parsed.manifest["x"]["gocassini.com"]["notes"] == [dict(atMs=75, text="Uncommitted note")]
    assert [w.text for w in portable.load_words(parsed, parsed.default_transcript_id)] == [
        "First",
        "second",
        "third.",
    ]


def test_stored_transcript_preserved_across_queued_running_and_failed_statuses(tmp_path):
    lib = tmp_path / "Library"
    queued_sid = "11111111-2222-4333-8444-555555555555"
    running_sid = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    failed_sid = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"

    queued_path, queued_meta = _write_published_v2_doc(
        lib,
        "2026-11-11--09.00-Queued Retry.opus",
        session_id=queued_sid,
        automatic_title="Queued Retry",
        status="done",
    )
    running_path, running_meta = _write_published_v2_doc(
        lib,
        "2026-11-11--09.15-Running Retry.opus",
        session_id=running_sid,
        automatic_title="Running Retry",
        status="done",
    )
    failed_path, _ = _write_published_v2_doc(
        lib,
        "2026-11-11--09.30-Failed Retry.opus",
        session_id=failed_sid,
        automatic_title="Failed Retry",
        status="failed",
        error=dict(id="transcribe-failed", action="Try again"),
    )

    # Overlay queued and running statuses via the Library v2 hidden working sessions, as in production.
    (lib / ".cassini/meta").mkdir(parents=True, exist_ok=True)
    for sid, base_meta, status, eta in (
        (queued_sid, queued_meta, "queued", 3),
        (running_sid, running_meta, "running", 1),
    ):
        session_dir = lib / ".cassini/sessions" / f"Recording-{sid}"
        session_dir.mkdir(parents=True)
        working_meta = dict(base_meta)
        working_meta["received"] = None
        working_meta["published"] = True
        working_meta["revision"] = base_meta["revision"] + 1
        working_meta["transcript"] = dict(status=status, reference=None)
        working_meta["etaMinutes"] = eta
        atomic_json(session_dir / "meeting.json", working_meta)

    by_title = {m.title: m for m in LibraryScanner(lib).scan().meetings}
    assert by_title["Queued Retry"].transcript == "queued"
    assert by_title["Running Retry"].transcript == "running"
    assert by_title["Failed Retry"].transcript == "failed"
    for title in ("Queued Retry", "Running Retry", "Failed Retry"):
        assert [w.text for w in by_title[title].words] == ["First", "second", "third."]

    # 1. Unedited published meetings copy verbatim and preserve all 3 words across queued, running, failed.
    unedited_zip = export_meetings_zip(
        (by_title["Queued Retry"], by_title["Running Retry"], by_title["Failed Retry"]),
        tmp_path / "UneditedRetryOut",
    )
    with zipfile.ZipFile(unedited_zip) as zf:
        assert zf.read("Queued Retry.opus") == queued_path.read_bytes()
        assert zf.read("Running Retry.opus") == running_path.read_bytes()
        assert zf.read("Failed Retry.opus") == failed_path.read_bytes()

    # 2. Locally edited published meetings AND v1 sidecar-referenced meetings preserve all 3 words via write_cassini_opus.
    v1_lib = tmp_path / "V1Library"
    v1_received = import_received(OPUS / "002-plain-audio.opus", v1_lib)
    atomic_json(
        v1_received.directory / "transcript.json",
        dict(
            format=portable.WORDS_FORMAT,
            language="en",
            items=[
                dict(speaker="s1", startMs=0, endMs=30, text="First"),
                dict(speaker="s1", startMs=35, endMs=70, text="second"),
                dict(speaker="s1", startMs=75, endMs=110, text="third."),
            ],
        ),
    )
    v1_variants = []
    for status in ("queued", "running", "failed"):
        update_metadata(
            v1_received.directory,
            title=f"V1 {status}",
            titleSource="user",
            transcript=dict(status=status, reference=dict(REFERENCE)),
            etaMinutes=2 if status in ("queued", "running") else None,
            error=dict(id="transcribe-failed", action="Try again") if status == "failed" else None,
            speakers=[dict(id="s1", name="Dana", provenance="user", colourIndex=0)],
        )
        (scanned_v1,) = LibraryScanner(v1_lib).scan().meetings
        assert scanned_v1.transcript == status
        assert [w.text for w in scanned_v1.words] == ["First", "second", "third."]
        v1_variants.append(replace(scanned_v1, id=f"{scanned_v1.id}-{status}", directory=v1_lib / f"Received-{status}"))

    edited_published = [
        replace(by_title["Queued Retry"], title="Edited queued"),
        replace(by_title["Running Retry"], title="Edited running"),
        replace(by_title["Failed Retry"], title="Edited failed"),
    ]
    rewritten_zip = export_meetings_zip((*edited_published, *v1_variants), tmp_path / "RewrittenRetryOut")
    check_dir = tmp_path / "RewrittenChecked"
    check_dir.mkdir()
    with zipfile.ZipFile(rewritten_zip) as zf:
        assert len(zf.infolist()) == 6
        for info in zf.infolist():
            member_path = check_dir / info.filename
            member_path.write_bytes(zf.read(info.filename))
            parsed = portable.read_meeting(str(member_path))
            assert portable.verify_audio(str(member_path), parsed).state == portable.State.OK
            assert [w.text for w in portable.load_words(parsed, parsed.default_transcript_id)] == [
                "First",
                "second",
                "third.",
            ]


def test_root_level_and_received_non_ok_documents_preserve_exact_bytes_by_verified_state(tmp_path):
    sources = (
        OPUS / "010-stale-audio.opus",
        OPUS / "015-unknown-major-version.opus",
        OPUS / "006-missing-chunk.opus",
        OPUS / "002-plain-audio.opus",
    )
    root_meetings = []
    for index, src in enumerate(sources):
        sub_root = tmp_path / f"RootLibrary-{index}"
        sub_root.mkdir()
        (sub_root / src.name).write_bytes(src.read_bytes())
        (scanned_root,) = exportable_meetings(LibraryScanner(sub_root).scan())
        root_meetings.append(replace(scanned_root, collection_key=f"file/root-{index}"))

    assert len(root_meetings) == 4
    stale_root = next(m for m in root_meetings if m.audio_file.name == "010-stale-audio.opus")
    assert stale_root.metadata == {} and stale_root.transcript == "damaged"

    # Also test a Received-* folder whose meeting.json still says status="done" after its audio became STALE_AUDIO.
    rec_lib = tmp_path / "ReceivedLibrary"
    initially_ok = import_received(OPUS / "003-multichunk-v1.opus", rec_lib)
    stale_bytes = (OPUS / "010-stale-audio.opus").read_bytes()
    initially_ok.audio_file.write_bytes(stale_bytes)
    (rescan_stale_received,) = LibraryScanner(rec_lib).scan().meetings
    pre_rescan_stale_received = replace(
        initially_ok,
        id="pre-rescan-stale",
        directory=rec_lib / "Received-pre-rescan-stale",
    )

    archive_path = export_meetings_zip(
        (*root_meetings, rescan_stale_received, pre_rescan_stale_received),
        tmp_path / "NonOkOut",
    )
    with zipfile.ZipFile(archive_path) as zf:
        infos = zf.infolist()
        assert len(infos) == 6
        exported_bytes = [zf.read(info.filename) for info in infos]

    expected_multiset = sorted(
        [
            *(hashlib.sha256(src.read_bytes()).hexdigest() for src in sources),
            hashlib.sha256(stale_bytes).hexdigest(),
            hashlib.sha256(stale_bytes).hexdigest(),
        ]
    )
    actual_multiset = sorted(hashlib.sha256(payload).hexdigest() for payload in exported_bytes)
    assert actual_multiset == expected_multiset
    # Verify none of the stale payloads were rewritten into State.OK documents.
    stale_check = tmp_path / "check-stale.opus"
    for payload in exported_bytes:
        if hashlib.sha256(payload).hexdigest() == hashlib.sha256(stale_bytes).hexdigest():
            stale_check.write_bytes(payload)
            parsed = portable.read_meeting(str(stale_check))
            assert portable.verify_audio(str(stale_check), parsed).state == portable.State.STALE_AUDIO
