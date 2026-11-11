# SPDX-License-Identifier: GPL-3.0-or-later
"""D96: real documents, hostile archive metadata, cancellation and exact dedup."""

import json
import stat
import struct
import threading
import zipfile
from concurrent.futures import CancelledError
from contextlib import contextmanager
from pathlib import Path

import pytest

from cassini_talk import archive_import, batch_import, received
from cassini_talk.library import LibraryScanner

FIXTURES = Path(__file__).parent / "conformance/opus"
ONE = FIXTURES / "001-minimal-v1.opus"
TWO = FIXTURES / "002-plain-audio.opus"


def zip_file(path, files, *, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return path


def documents(root):
    return list(root.glob("Received-*"))


def test_many_inputs_and_nested_zip_paths_keep_original_bytes_and_names(tmp_path):
    archive = zip_file(
        tmp_path / "Notes.zip",
        [
            ("English notes/First meeting.opus", ONE.read_bytes()),
            ("English notes/Second meeting.ogg", TWO.read_bytes()),
            ("Read me.txt", b"Synthetic English fixtures."),
            ("empty/", b""),
            ("nested.zip", b"not recursively opened"),
        ],
        compression=zipfile.ZIP_DEFLATED,
    )
    root = tmp_path / "Library"
    result = batch_import.import_files([ONE, archive, tmp_path / "unsupported.wav"], root)
    assert (result.imported, result.duplicates, result.ignored, result.failed) == (2, 1, 3, 0)
    assert len(documents(root)) == 2 and not list(root.glob(".received-*"))
    metadata = [json.loads((p / "meeting.json").read_text()) for p in documents(root)]
    assert {v["received"]["originalName"] for v in metadata} == {"001-minimal-v1.opus", "Second meeting.opus"}
    for directory in documents(root):
        value = json.loads((directory / "meeting.json").read_text())["received"]
        original = directory / value["originalName"]
        assert original.read_bytes() in (ONE.read_bytes(), TWO.read_bytes())
    assert result.last_key in {m.key for m in LibraryScanner(root).scan().meetings}


def test_archive_basename_and_folder_are_not_replaced_with_private_staging_names(tmp_path):
    archive = zip_file(tmp_path / "Collection.zip", [("English notes/Planning.opus", ONE.read_bytes())])
    root = tmp_path / "Library"
    result = batch_import.import_files([archive], root)
    assert result.imported == 1
    (directory,) = documents(root)
    value = json.loads((directory / "meeting.json").read_text())["received"]
    assert value["originalName"] == "Planning.opus" and value["originalFolderName"] == "English notes"


@pytest.mark.parametrize(
    "name", ["../escape.opus", "/escape.opus", "C:/escape.opus", "folder\\escape.opus", "a/../../escape.opus"]
)
def test_unsafe_entry_rejects_archive_before_any_document_is_published(tmp_path, name):
    path = zip_file(tmp_path / "Unsafe.zip", [("Safe.opus", ONE.read_bytes()), (name, TWO.read_bytes())])
    root = tmp_path / "Library"
    result = batch_import.import_files([path], root)
    assert result.imported == 0 and result.failed == 1
    assert result.errors == [("Unsafe.zip", "unsafe")]
    assert not root.exists()
    assert not (tmp_path / "escape.opus").exists()


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_special_entries_are_never_followed_or_materialized(tmp_path, kind):
    entry = zipfile.ZipInfo("Shortcut.opus")
    entry.create_system = 3
    entry.external_attr = (kind | 0o600) << 16
    path = zip_file(tmp_path / "Special.zip", [(entry, b"../Synthetic target")])
    result = batch_import.import_files([path], tmp_path / "Library")
    assert result.errors == [("Special.zip", "unsafe")]


def test_embedded_nul_name_is_rejected_before_zipfile_truncates_its_display_name(tmp_path):
    path = zip_file(tmp_path / "Nul.zip", [("one.opus", ONE.read_bytes())])
    path.write_bytes(path.read_bytes().replace(b"one.opus", b"one\0opus"))
    assert batch_import.import_files([path], tmp_path / "Library").errors == [("Nul.zip", "unsafe")]


def test_encrypted_flag_is_rejected_before_opening_members(tmp_path):
    path = zip_file(tmp_path / "Password.zip", [("English.opus", ONE.read_bytes())])
    data = bytearray(path.read_bytes())
    at = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, at + 8, 1)
    path.write_bytes(data)
    assert batch_import.import_files([path], tmp_path / "Library").errors == [("Password.zip", "encrypted")]


@pytest.mark.parametrize("field,value", [(8, archive_import.MAX_ENTRIES + 1), (12, archive_import.MAX_DIRECTORY + 1)])
def test_declared_count_or_directory_size_is_rejected_before_zipfile_allocation(tmp_path, monkeypatch, field, value):
    path = zip_file(tmp_path / "Bomb.zip", [("English.opus", ONE.read_bytes())])
    data = bytearray(path.read_bytes())
    at = data.rindex(b"PK\x05\x06")
    if field == 8:
        struct.pack_into("<HH", data, at + 8, value, value)
    else:
        struct.pack_into("<L", data, at + field, value)
    path.write_bytes(data)

    def forbidden(*_args, **_kwargs):
        pytest.fail("ZipFile allocated metadata before rejecting the declared bound")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    assert batch_import.import_files([path], tmp_path / "Library").errors == [("Bomb.zip", "limit")]


def test_forged_small_count_still_checks_actual_central_directory_entry_count(tmp_path):
    path = zip_file(tmp_path / "Forged count.zip", [(f"{i}.txt", b"") for i in range(archive_import.MAX_ENTRIES + 1)])
    data = bytearray(path.read_bytes())
    at = data.rindex(b"PK\x05\x06")
    struct.pack_into("<HH", data, at + 8, 1, 1)
    path.write_bytes(data)
    assert batch_import.import_files([path], tmp_path / "Library").errors == [("Forged count.zip", "limit")]


def test_declared_uncompressed_bytes_and_total_batch_entries_are_bounded(tmp_path, monkeypatch):
    path = zip_file(tmp_path / "Large.zip", [("Ignored.txt", b"x" * 100)])
    monkeypatch.setattr(archive_import, "MAX_BYTES", 50)
    assert batch_import.import_files([path], tmp_path / "Library").errors == [("Large.zip", "limit")]
    monkeypatch.setattr(archive_import, "MAX_BYTES", 8 * 1024**3)
    monkeypatch.setattr(archive_import, "MAX_ENTRIES", 1)
    result = batch_import.import_files([ONE, TWO], tmp_path / "Library")
    assert result.imported == 1 and result.failed == 1


def test_zip64_end_record_imports_small_real_document(tmp_path):
    path = zip_file(tmp_path / "ZIP64.zip", [("English.opus", ONE.read_bytes())])
    data = path.read_bytes()
    at = data.rindex(b"PK\x05\x06")
    _, disk, cd_disk, per_disk, count, length, offset, comment = struct.unpack("<4s4H2LH", data[at:])
    end64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, disk, cd_disk, per_disk, count, length, offset)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, at, 1)
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 65535, 65535, 0xFFFFFFFF, 0xFFFFFFFF, comment)
    path.write_bytes(data[:at] + end64 + locator + end)
    assert batch_import.import_files([path], tmp_path / "Library").imported == 1


def test_fake_trailing_end_record_in_comment_cannot_bypass_metadata_bound(tmp_path, monkeypatch):
    path = zip_file(tmp_path / "Ambiguous.zip", [("English.opus", ONE.read_bytes())])
    data = bytearray(path.read_bytes())
    at = data.rindex(b"PK\x05\x06")
    fake = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 0xFFFFFFFF, 0, 100)
    struct.pack_into("<H", data, at + 20, len(fake))
    data.extend(fake)
    path.write_bytes(data)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *_a, **_kw: pytest.fail("Ambiguous EOCD reached ZipFile"))
    assert batch_import.import_files([path], tmp_path / "Library").errors == [("Ambiguous.zip", "damaged")]


def test_bad_member_crc_is_counted_and_later_valid_member_still_imports(tmp_path):
    path = zip_file(tmp_path / "Partial.zip", [("Bad.opus", ONE.read_bytes()), ("Good.opus", TWO.read_bytes())])
    data = bytearray(path.read_bytes())
    name_len, extra_len = struct.unpack_from("<HH", data, 26)
    data[30 + name_len + extra_len] ^= 0xFF
    path.write_bytes(data)
    result = batch_import.import_files([path], tmp_path / "Library")
    assert result.imported == 1 and result.failed == 1 and result.errors == [("Bad.opus", "damaged")]


def test_cancel_after_first_commit_keeps_that_document_and_removes_extraction(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    scratch = tmp_path / "Scratch"
    scratch.mkdir()
    real = batch_import.tempfile.TemporaryDirectory
    monkeypatch.setattr(batch_import.tempfile, "TemporaryDirectory", lambda **kw: real(dir=scratch, **kw))
    path = zip_file(tmp_path / "Cancel.zip", [("First.opus", ONE.read_bytes()), ("Second.opus", TWO.read_bytes())])
    cancel = threading.Event()

    def published(result):
        if result.imported == 1:
            cancel.set()

    result = batch_import.import_files([path], root, cancel=cancel, progress=published)
    assert result.cancelled and result.imported == 1 and result.failed == 0
    assert len(documents(root)) == 1 and not list(root.glob(".received-*")) and not list(scratch.iterdir())


def test_cancel_during_real_copy_cleans_partial_and_never_publishes(tmp_path, monkeypatch):
    path = tmp_path / "Held.opus"
    path.write_bytes(b"x" * (2 * 1024**2))
    root = tmp_path / "Library"
    cancel = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    original = received.regular_input

    @contextmanager
    def gated(path):
        with original(path) as stream:

            class Gate:
                def __getattr__(self, name):
                    return getattr(stream, name)

                def read(self, n):
                    if stream.tell() >= 1024**2:
                        entered.set()
                        assert release.wait(30), "copy barrier was never released"
                    return stream.read(n)

            yield Gate()

    monkeypatch.setattr(received, "regular_input", gated)
    errors = []

    def worker():
        try:
            received.import_received(path, root, cancel=cancel)
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(30), "real copy never reached second block"
        assert list(root.glob(".received-*"))
        cancel.set()
        release.set()
        assert done.wait(30), "cancelled copy did not finish"
    finally:
        release.set()
        thread.join(30)
    assert len(errors) == 1 and isinstance(errors[0], CancelledError)
    assert not list(root.iterdir())


def test_known_regular_file_length_completes_without_an_eof_read(tmp_path, monkeypatch):
    original = received.regular_input

    @contextmanager
    def no_eof(path):
        with original(path) as stream:

            class Source:
                def __getattr__(self, name):
                    return getattr(stream, name)

                def read(self, n):
                    assert stream.tell() < Path(path).stat().st_size, "unnecessary EOF read"
                    return stream.read(n)

            yield Source()

    monkeypatch.setattr(received, "regular_input", no_eof)
    assert received.import_received(ONE, tmp_path / "Library").has_audio


def test_corrupted_existing_copy_does_not_win_digest_dedup(tmp_path):
    root = tmp_path / "Library"
    first = received.import_received(ONE, root)
    first.audio_file.write_bytes(b"Synthetic damaged original")
    result = batch_import.import_files([ONE], root)
    assert result.imported == 1 and result.duplicates == 0 and len(documents(root)) == 2
