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
