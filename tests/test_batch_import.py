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
    "name",
    [
        "../escape.opus",
        "..\\escape.opus",
        "/escape.opus",
        "\\escape.opus",
        "\\\\server\\share\\escape.opus",
        "C:/escape.opus",
        "C:\\escape.opus",
        "C:escape.opus",
        "folder\\..\\escape.opus",
        "a/../../escape.opus",
    ],
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


def test_published_d96_archive_limits_and_boundaries(tmp_path, monkeypatch):
    assert (archive_import.MAX_ENTRIES, archive_import.MAX_BYTES, archive_import.MAX_RATIO) == (
        2_000,
        20_000_000_000,
        100,
    )
    at_limit = zip_file(
        tmp_path / "ExactCount.zip",
        [("Meeting.opus", ONE.read_bytes())] + [(f"note-{i}.txt", b"") for i in range(1_999)],
    )
    exact = batch_import.import_files([at_limit], tmp_path / "LibraryExact")
    assert (exact.imported, exact.ignored, exact.failed) == (1, 1_999, 0)
    over_limit = zip_file(
        tmp_path / "OverCount.zip",
        [("Meeting.opus", ONE.read_bytes())] + [(f"note-{i}.txt", b"") for i in range(2_000)],
    )
    refused = batch_import.import_files([over_limit], tmp_path / "LibraryOver")
    assert refused.imported == 0 and refused.errors == [("OverCount.zip", "limit")]

    path = zip_file(tmp_path / "Large.zip", [("Ignored.txt", b"x" * 100)])
    monkeypatch.setattr(archive_import, "MAX_BYTES", 100)
    assert batch_import.import_files([path], tmp_path / "LibraryByteOk").ignored == 1
    monkeypatch.setattr(archive_import, "MAX_BYTES", 99)
    assert batch_import.import_files([path], tmp_path / "LibraryByteOver").errors == [("Large.zip", "limit")]

    monkeypatch.setattr(archive_import, "MAX_BYTES", 20_000_000_000)
    monkeypatch.setattr(archive_import, "MAX_ENTRIES", 2)
    a1 = zip_file(tmp_path / "One.zip", [("a.opus", ONE.read_bytes()), ("a.txt", b"ok")])
    a2 = zip_file(tmp_path / "Two.zip", [("b.opus", TWO.read_bytes()), ("b.txt", b"ok")])
    per_archive = batch_import.import_files([a1, a2], tmp_path / "LibraryTwoZips")
    assert (per_archive.imported, per_archive.ignored, per_archive.failed) == (2, 2, 0)

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
    path.write_bytes(b"x" * (3 * 1024**2))
    root = tmp_path / "Library"
    cancel = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    reads_after_cancel = []
    original = received.regular_input

    @contextmanager
    def gated(path):
        with original(path) as stream:

            class Gate:
                def __getattr__(self, name):
                    return getattr(stream, name)

                def read(self, n):
                    if cancel.is_set():
                        reads_after_cancel.append(stream.tell())
                    elif stream.tell() >= 1024**2:
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
    assert not reads_after_cancel, f"copy kept reading after cancel at offsets {reads_after_cancel}"
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


def test_one_import_does_not_rebuild_unrelated_published_transcripts(tmp_path, monkeypatch):
    from cassini_talk.library_v2_index import PublishedIndex

    root = tmp_path / "Library"
    root.mkdir()
    (root / "Unrelated.opus").write_bytes(TWO.read_bytes())
    monkeypatch.setattr(PublishedIndex, "combine", lambda *_a, **_kw: pytest.fail("Unrelated published models rebuilt"))
    assert batch_import.import_files([ONE], root).imported == 1


@pytest.mark.parametrize("method", [zipfile.ZIP_LZMA, 93])
def test_dictionary_allocating_zip_methods_are_ignored_without_starting_decoder(tmp_path, monkeypatch, method):
    path = zip_file(tmp_path / "Other compression.zip", [("English.opus", ONE.read_bytes())])
    data = bytearray(path.read_bytes())
    struct.pack_into("<H", data, 8, method)
    struct.pack_into("<H", data, data.index(b"PK\x01\x02") + 10, method)
    path.write_bytes(data)
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *_a, **_kw: pytest.fail("Unbounded dictionary decoder opened"))
    result = batch_import.import_files([path], tmp_path / "Library")
    assert result.ignored == 1 and result.imported == result.failed == 0


def test_progress_totals_include_zip_files_and_nonlocal_files_fail_individually(tmp_path):
    archive = zip_file(tmp_path / "Notes.zip", [("First.opus", ONE.read_bytes()), ("Second.opus", TWO.read_bytes())])
    updates = []
    result = batch_import.import_files([archive, None], tmp_path / "Library", progress=updates.append)
    assert result.total == 3 and result.imported == 2 and result.failed == 1
    assert updates[-1].total == 3 and updates[-1].failed == 1


def test_duplicate_verification_rejects_a_file_that_grows_during_the_hash(tmp_path, monkeypatch):
    path = tmp_path / "Changing.opus"
    path.write_bytes(ONE.read_bytes())
    original = received.regular_input

    @contextmanager
    def changing(name):
        with original(name) as stream:

            class Source:
                def __getattr__(self, attribute):
                    return getattr(stream, attribute)

                def read(self, count):
                    value = stream.read(count)
                    with Path(name).open("ab") as output:
                        output.write(b"Synthetic append")
                    return value

            yield Source()

    monkeypatch.setattr(received, "regular_input", changing)
    with pytest.raises(ValueError, match="changed"):
        received.sha256_file(path)


def test_ratio_over_100_to_1_refuses_archive_in_directory_and_on_actual_bytes(tmp_path, monkeypatch):
    import zlib

    # 1. Directory ratio boundary: exact 100:1 passes, 100:1 + 1 byte refuses whole archive before First.opus imports.
    exact = zip_file(
        tmp_path / "ExactRatio.zip",
        [("First.opus", ONE.read_bytes()), ("Pad.bin", b"A" * 50_000)],
        compression=zipfile.ZIP_DEFLATED,
    )
    data = bytearray(exact.read_bytes())
    cd = data.rindex(b"PK\x01\x02")
    comp_size = struct.unpack_from("<L", data, cd + 20)[0]
    struct.pack_into("<L", data, cd + 24, comp_size * 100)
    exact.write_bytes(data)
    ok_res = batch_import.import_files([exact], tmp_path / "LibExactRatio")
    assert ok_res.imported == 1 and ok_res.ignored == 1 and ok_res.failed == 0

    over = tmp_path / "OverRatio.zip"
    data_over = bytearray(data)
    struct.pack_into("<L", data_over, cd + 24, comp_size * 100 + 1)
    over.write_bytes(data_over)
    bad_res = batch_import.import_files([over], tmp_path / "LibOverRatio")
    assert bad_res.imported == 0 and bad_res.errors == [("OverRatio.zip", "limit")]

    # 2. Forged directory file_size hiding >100:1 deflate expansion is refused on bytes actually read.
    raw_bomb = b"A" * 100_000
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = compressor.compress(raw_bomb) + compressor.flush()
    assert len(raw_bomb) > len(deflated) * 100
    forged_size = 500
    forged_crc = zlib.crc32(raw_bomb[:forged_size]) & 0xFFFFFFFF
    name = b"Disguised.opus"
    local_hdr = struct.pack(
        "<4s5H3L2H", b"PK\x03\x04", 20, 0, 8, 0, 0, forged_crc, len(deflated), forged_size, len(name), 0
    )
    cd_offset = len(local_hdr) + len(name) + len(deflated)
    cd_hdr = struct.pack(
        "<4s6H3L5H2L",
        b"PK\x01\x02",
        20,
        20,
        0,
        8,
        0,
        0,
        forged_crc,
        len(deflated),
        forged_size,
        len(name),
        0,
        0,
        0,
        0,
        0,
        0,
    )
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(cd_hdr) + len(name), cd_offset, 0)
    forged_zip = tmp_path / "ForgedRatio.zip"
    forged_zip.write_bytes(local_hdr + name + deflated + cd_hdr + name + eocd)
    runtime_res = batch_import.import_files([forged_zip], tmp_path / "LibForgedRatio")
    assert runtime_res.imported == 0 and runtime_res.errors == [("ForgedRatio.zip", "limit")]

    # 3. Forged directory file_size hiding cumulative MAX_BYTES overflow (even below 100:1) is refused on actual read.
    payload = ONE.read_bytes() * 4
    c2 = zlib.compressobj(6, zlib.DEFLATED, -15)
    deflated2 = c2.compress(payload) + c2.flush()
    claimed = len(deflated2)
    crc2 = zlib.crc32(payload[:claimed]) & 0xFFFFFFFF
    lh2 = struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, 8, 0, 0, crc2, len(deflated2), claimed, len(name), 0)
    cd_off2 = len(lh2) + len(name) + len(deflated2)
    ch2 = struct.pack(
        "<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, 8, 0, 0, crc2, len(deflated2), claimed, len(name), 0, 0, 0, 0, 0, 0
    )
    eocd2 = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(ch2) + len(name), cd_off2, 0)
    forged_bytes = tmp_path / "ForgedBytes.zip"
    forged_bytes.write_bytes(lh2 + name + deflated2 + ch2 + name + eocd2)
    monkeypatch.setattr(archive_import, "MAX_BYTES", claimed + 10)
    bytes_res = batch_import.import_files([forged_bytes], tmp_path / "LibForgedBytes")
    assert bytes_res.imported == 0 and bytes_res.errors == [("ForgedBytes.zip", "limit")]


def test_aes_strong_and_local_header_encryption_and_overlapping_entries_refuse_whole_archive(tmp_path):
    # WinZip AES (method 99) without traditional flag bit 0 still reports encrypted, not empty/unsupported.
    aes = zip_file(tmp_path / "AES.zip", [("First.opus", ONE.read_bytes()), ("Secret.opus", TWO.read_bytes())])
    data = bytearray(aes.read_bytes())
    second_cd = data.rindex(b"PK\x01\x02")
    second_lh = struct.unpack_from("<L", data, second_cd + 42)[0]
    struct.pack_into("<H", data, second_cd + 10, 99)
    struct.pack_into("<H", data, second_lh + 8, 99)
    aes.write_bytes(data)
    res_aes = batch_import.import_files([aes], tmp_path / "LibAES")
    assert res_aes.imported == 0 and res_aes.errors == [("AES.zip", "encrypted")]

    # Local-header-only encrypted bit on second member refuses whole archive before first member imports.
    local_enc = zip_file(
        tmp_path / "LocalEnc.zip", [("First.opus", ONE.read_bytes()), ("Second.opus", TWO.read_bytes())]
    )
    data_le = bytearray(local_enc.read_bytes())
    lh2 = struct.unpack_from("<L", data_le, data_le.rindex(b"PK\x01\x02") + 42)[0]
    struct.pack_into("<H", data_le, lh2 + 6, 1)
    local_enc.write_bytes(data_le)
    res_le = batch_import.import_files([local_enc], tmp_path / "LibLocalEnc")
    assert res_le.imported == 0 and res_le.errors == [("LocalEnc.zip", "encrypted")]

    # Overlapping local headers (non-recursive zbload-style amplification) refuse whole archive.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        overlap = zip_file(tmp_path / "Overlap.zip", [("Same.opus", ONE.read_bytes()), ("Same.opus", ONE.read_bytes())])
    data_ov = bytearray(overlap.read_bytes())
    struct.pack_into("<L", data_ov, data_ov.rindex(b"PK\x01\x02") + 42, 0)
    overlap.write_bytes(data_ov)
    res_ov = batch_import.import_files([overlap], tmp_path / "LibOverlap")
    assert res_ov.imported == 0 and res_ov.errors == [("Overlap.zip", "damaged")]


def test_data_descriptor_streamed_zip_and_long_utf8_member_names_and_nested_only_archive(tmp_path):
    # Data-descriptor ZIP (bit 3 set, local crc/sizes zeroed as in ZipOutputStream) + >255-byte UTF-8 basename.
    long_name = "Folder/" + ("Réunion-" * 35) + "notes.opus"
    assert len(Path(long_name).name.encode("utf-8")) > 255
    streamed = zip_file(tmp_path / "Streamed.zip", [(long_name, ONE.read_bytes())], compression=zipfile.ZIP_DEFLATED)
    raw = bytearray(streamed.read_bytes())
    flags = struct.unpack_from("<H", raw, 6)[0] | 0x08
    struct.pack_into("<H", raw, 6, flags)
    struct.pack_into("<3L", raw, 14, 0, 0, 0)
    cd = raw.rindex(b"PK\x01\x02")
    struct.pack_into("<H", raw, cd + 8, flags)
    streamed.write_bytes(raw)
    res = batch_import.import_files([streamed], tmp_path / "LibStreamed")
    assert res.imported == 1 and res.failed == 0

    # Nested-only archive counts nested archive in unsupported and reports empty archive.
    nested_only = zip_file(tmp_path / "NestedOnly.zip", [("inner.zip", streamed.read_bytes())])
    nested_res = batch_import.import_files([nested_only], tmp_path / "LibNested")
    assert (nested_res.imported, nested_res.ignored, nested_res.failed) == (0, 1, 0)
    assert nested_res.errors == [("NestedOnly.zip", "empty")]


def test_bulk_dedup_metadata_reads_scale_linearly_instead_of_quadratically(tmp_path, monkeypatch):
    from cassini_talk.portable_writer import rewrite_tags

    def make_files(folder, prefix, count):
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for idx in range(count):
            target = folder / f"{prefix}-{idx}.opus"
            with TWO.open("rb") as src, target.open("wb") as dst:
                rewrite_tags(src, dst, dict(TITLE=f"{prefix} {idx}", COMMENT=f"{prefix}-{idx}"))
            out.append(target)
        return out

    # Compare metadata reads for N=15 vs 2N=30 imports on top of M=20 existing Library entries.
    existing = make_files(tmp_path / "seed", "Seed", 20)
    batch_small = make_files(tmp_path / "small", "Small", 15)
    batch_large = make_files(tmp_path / "large", "Large", 30)

    lib_small = tmp_path / "LibSmall"
    lib_large = tmp_path / "LibLarge"
    assert batch_import.import_files(existing, lib_small).imported == 20
    assert batch_import.import_files(existing, lib_large).imported == 20

    real_read = received.read_metadata
    reads = []

    def counted_read(directory):
        reads.append(directory)
        return real_read(directory)

    monkeypatch.setattr(received, "read_metadata", counted_read)
    reads.clear()
    res_small = batch_import.import_files(batch_small + [existing[0], batch_small[0]], lib_small)
    small_reads = len(reads)
    assert (res_small.imported, res_small.duplicates) == (15, 2)
    # Linear O(M + duplicates): 20 initial index reads + 2 duplicate verification reads = 22 (vs >500 quadratic).
    assert small_reads <= 25, small_reads

    reads.clear()
    res_large = batch_import.import_files(batch_large + [existing[0], batch_large[0]], lib_large)
    large_reads = len(reads)
    assert (res_large.imported, res_large.duplicates) == (30, 2)
    assert large_reads <= 25, large_reads


@pytest.mark.parametrize(
    "eocd_disks,loc_disks",
    [
        ((0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF), 1),
        ((0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF), 0),
        ((0xFFFF, 0xFFFF, 1, 1, None, None), 1),
        ((0, 0, 0xFFFF, 1, None, None), 1),
    ],
)
def test_zip64_all_ffff_sentinels_and_libarchive_locator_are_accepted(tmp_path, eocd_disks, loc_disks):
    path = zip_file(tmp_path / "Sentinels.zip", [("English.opus", ONE.read_bytes())])
    data = path.read_bytes()
    at = data.rindex(b"PK\x05\x06")
    _, disk, cd_disk, per_disk, count, length, offset, comment = struct.unpack("<4s4H2LH", data[at:])
    end64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, disk, cd_disk, per_disk, count, length, offset)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, at, loc_disks)
    d0, d1, d2, d3, d4, d5 = eocd_disks
    end = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        d0,
        d1,
        d2,
        d3,
        length if d4 is None else d4,
        offset if d5 is None else d5,
        comment,
    )
    path.write_bytes(data[:at] + end64 + locator + end)
    res = batch_import.import_files([path], tmp_path / "Library")
    assert res.imported == 1 and res.failed == 0


def test_classic_65535_count_without_zip64_and_unsentineled_zip64_smuggling_report_limit(tmp_path, monkeypatch):
    # Classic non-ZIP64 EOCD with 65,535 entries (no PK\x06\x07 locator) is tooLarge ("limit"), not "damaged".
    classic_max = zip_file(tmp_path / "Classic65535.zip", [("English.opus", ONE.read_bytes())])
    smuggled = zip_file(tmp_path / "Smuggled64.zip", [("English.opus", ONE.read_bytes())])
    raw = bytearray(classic_max.read_bytes())
    at = raw.rindex(b"PK\x05\x06")
    struct.pack_into("<HH", raw, at + 8, 0xFFFF, 0xFFFF)
    classic_max.write_bytes(raw)

    # Unsentineled ZIP64 locator + oversized ZIP64 directory cannot bypass preflight into ZipFile._EndRecData64.
    base = smuggled.read_bytes()
    eocd_at = base.rindex(b"PK\x05\x06")
    _, disk, cd_disk, per_disk, count, length, offset, comment = struct.unpack("<4s4H2LH", base[eocd_at:])
    huge_dir = archive_import.MAX_DIRECTORY + 1
    end64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, huge_dir, offset)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, eocd_at, 1)
    unsentineled_end = struct.pack("<4s4H2LH", b"PK\x05\x06", disk, cd_disk, per_disk, count, length, offset, comment)
    smuggled.write_bytes(base[:eocd_at] + end64 + locator + unsentineled_end)

    monkeypatch.setattr(zipfile, "ZipFile", lambda *_a, **_kw: pytest.fail("ZipFile opened oversized archive"))
    assert batch_import.import_files([classic_max], tmp_path / "Lib1").errors == [("Classic65535.zip", "limit")]
    assert batch_import.import_files([smuggled], tmp_path / "Lib2").errors == [("Smuggled64.zip", "limit")]


def test_macos_finder_sidecars_and_directory_entries_match_d96_counts(tmp_path):
    unix_dir = zipfile.ZipInfo("UnixDir")
    unix_dir.create_system = 3
    unix_dir.external_attr = (stat.S_IFDIR | 0o755) << 16
    finder = zip_file(
        tmp_path / "Finder.zip",
        [
            ("Meetings/", b""),
            ("Meetings/Standup.opus", ONE.read_bytes()),
            ("__MACOSX/", b""),
            ("__MACOSX/Meetings/", b""),
            ("__MACOSX/Meetings/._Standup.opus", b"\x00\x05\x16\x07AppleDouble"),
            ("Meetings/._ResourceFork.opus", b"\x00\x05\x16\x07AppleDouble"),
            ("WindowsDir\\", b""),
            (unix_dir, b""),
        ],
        compression=zipfile.ZIP_DEFLATED,
    )
    res = batch_import.import_files([finder], tmp_path / "LibFinder")
    assert (res.total, res.imported, res.ignored, res.failed) == (3, 1, 2, 0)
    assert not res.errors

    # Archive with only Finder sidecars and directories reports empty, with 0 failed.
    sidecar_only = zip_file(
        tmp_path / "FinderEmpty.zip",
        [("__MACOSX/", b""), ("__MACOSX/._Note.opus", b"\x00\x05\x16\x07AppleDouble")],
    )
    empty_res = batch_import.import_files([sidecar_only], tmp_path / "LibFinderEmpty")
    assert (empty_res.imported, empty_res.ignored, empty_res.failed) == (0, 1, 0)
    assert empty_res.errors == [("FinderEmpty.zip", "empty")]

    dirs_only = zip_file(tmp_path / "DirsOnly.zip", [("Meetings/", b""), ("Sub\\Nested\\", b"")])
    dirs_res = batch_import.import_files([dirs_only], tmp_path / "LibDirsOnly")
    assert (dirs_res.imported, dirs_res.ignored, dirs_res.failed) == (0, 0, 0)
    assert dirs_res.errors == [("DirsOnly.zip", "empty")]


def test_local_vs_central_flag_differences_are_tolerated_while_unsafe_local_names_are_refused(tmp_path):
    # Local bit 3 (0x08) + deflate level bits 1-2 (0x06) + omitted UTF-8 bit 11 (0x800) vs central 0x800.
    path = zip_file(
        tmp_path / "MixedFlags.zip",
        [("Réunion/Café.opus", ONE.read_bytes()), ("Second.opus", TWO.read_bytes())],
        compression=zipfile.ZIP_DEFLATED,
    )
    raw = bytearray(path.read_bytes())
    # First local header: clear 0x800, set 0x08 | 0x06, zero local crc/sizes.
    struct.pack_into("<H", raw, 6, 0x000E)
    struct.pack_into("<3L", raw, 14, 0, 0, 0)
    # Second entry: set 0x08 in central header only, keep 0x00 in local header.
    second_cd = raw.rindex(b"PK\x01\x02")
    second_flags = struct.unpack_from("<H", raw, second_cd + 8)[0] | 0x0008
    struct.pack_into("<H", raw, second_cd + 8, second_flags)
    path.write_bytes(raw)
    res = batch_import.import_files([path], tmp_path / "LibFlags")
    assert res.imported == 2 and res.failed == 0

    # Reverse UTF-8 mismatch: CP437 bytes (e.g. Caf\x82.opus) with central flags=0 and local flags=0x800,
    # plus mixed-encoding entry (central CP437 flags=0, local UTF-8 flags=0x800 for same name).
    import zlib

    def raw_zip_entry(local_name, local_flags, central_name, central_flags, payload, offset):
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        lh = (
            struct.pack(
                "<4s5H3L2H",
                b"PK\x03\x04",
                20,
                local_flags,
                0,
                0,
                0,
                crc,
                len(payload),
                len(payload),
                len(local_name),
                0,
            )
            + local_name
            + payload
        )
        ch = (
            struct.pack(
                "<4s6H3L5H2L",
                b"PK\x01\x02",
                20,
                20,
                central_flags,
                0,
                0,
                0,
                crc,
                len(payload),
                len(payload),
                len(central_name),
                0,
                0,
                0,
                0,
                0,
                offset,
            )
            + central_name
        )
        return lh, ch

    cp437_cafe = "Café.opus".encode("cp437")
    assert cp437_cafe == b"Caf\x82.opus"
    lh1, ch1 = raw_zip_entry(cp437_cafe, 0x0800, cp437_cafe, 0x0000, ONE.read_bytes(), 0)
    lh2, ch2 = raw_zip_entry(
        "Réunion.opus".encode(),
        0x0800,
        "Réunion.opus".encode("cp437"),
        0x0000,
        TWO.read_bytes(),
        len(lh1),
    )
    cd_off = len(lh1) + len(lh2)
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 2, 2, len(ch1) + len(ch2), cd_off, 0)
    rev_path = tmp_path / "ReverseFlags.zip"
    rev_path.write_bytes(lh1 + lh2 + ch1 + ch2 + eocd)
    rev_root = tmp_path / "LibRevFlags"
    rev_res = batch_import.import_files([rev_path], rev_root)
    assert rev_res.imported == 2 and rev_res.failed == 0
    assert {json.loads((p / "meeting.json").read_text())["received"]["originalName"] for p in documents(rev_root)} == {
        "Café.opus",
        "Réunion.opus",
    }

    # Unsafe CP437 local path with local_flags=0x800 must still be refused as unsafe.
    bad_lh, bad_ch = raw_zip_entry(b"..\\Caf\x82.opus", 0x0800, b"Safe.opus", 0x0000, ONE.read_bytes(), 0)
    bad_eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(bad_ch), len(bad_lh), 0)
    bad_rev = tmp_path / "UnsafeRev.zip"
    bad_rev.write_bytes(bad_lh + bad_ch + bad_eocd)
    assert batch_import.import_files([bad_rev], tmp_path / "LibUnsafeRev").errors == [("UnsafeRev.zip", "unsafe")]

    # Unsafe Info-ZIP Unicode Path extra (0x7075) in central header refuses archive as unsafe.

    unsafe_unicode = zip_file(tmp_path / "UnicodeEscape.zip", [("Safe.opus", ONE.read_bytes())])
    raw_u = bytearray(unsafe_unicode.read_bytes())
    evil_utf8 = b"../escape.opus"
    upath_payload = struct.pack("<BL", 1, zlib.crc32(b"Safe.opus") & 0xFFFFFFFF) + evil_utf8
    extra_7075 = struct.pack("<HH", 0x7075, len(upath_payload)) + upath_payload
    cd_at = raw_u.rindex(b"PK\x01\x02")
    eocd_at = raw_u.rindex(b"PK\x05\x06")
    struct.pack_into("<H", raw_u, cd_at + 30, len(extra_7075))
    cd_entry_end = eocd_at
    new_raw = bytes(raw_u[:cd_entry_end]) + extra_7075 + bytes(raw_u[eocd_at:])
    new_eocd_at = new_raw.rindex(b"PK\x05\x06")
    patched = bytearray(new_raw)
    old_cd_len = struct.unpack_from("<L", patched, new_eocd_at + 12)[0]
    struct.pack_into("<L", patched, new_eocd_at + 12, old_cd_len + len(extra_7075))
    unsafe_unicode.write_bytes(patched)
    assert batch_import.import_files([unsafe_unicode], tmp_path / "LibUnicode").errors == [
        ("UnicodeEscape.zip", "unsafe")
    ]


@pytest.mark.parametrize("location,extra_id", [("central", 0x9901), ("local", 0x9901), ("central", 0x0017)])
def test_winzip_aes_0x9901_and_strong_encryption_extras_refuse_archive_as_encrypted(tmp_path, location, extra_id):
    # Writer leaves method=8 and flag_bits=0, signaling encryption via extra field 0x9901 or 0x0017.
    path = zip_file(
        tmp_path / f"Enc-{location}-{extra_id:04x}.zip",
        [("First.opus", ONE.read_bytes()), ("Secret.opus", TWO.read_bytes())],
        compression=zipfile.ZIP_DEFLATED,
    )
    raw = bytearray(path.read_bytes())
    extra_record = struct.pack("<HH7s", extra_id, 7, b"\x02\x00AE\x03\x08\x00")
    cd2 = raw.rindex(b"PK\x01\x02")
    eocd = raw.rindex(b"PK\x05\x06")
    if location == "central":
        struct.pack_into("<H", raw, cd2 + 30, len(extra_record))
        rebuilt = bytearray(bytes(raw[:eocd]) + extra_record + bytes(raw[eocd:]))
        new_eocd = rebuilt.rindex(b"PK\x05\x06")
        cd_len = struct.unpack_from("<L", rebuilt, new_eocd + 12)[0]
        struct.pack_into("<L", rebuilt, new_eocd + 12, cd_len + len(extra_record))
        path.write_bytes(rebuilt)
    else:
        lh2 = struct.unpack_from("<L", raw, cd2 + 42)[0]
        fname_len, old_extra_len = struct.unpack_from("<HH", raw, lh2 + 26)
        assert old_extra_len == 0
        struct.pack_into("<H", raw, lh2 + 28, len(extra_record))
        insert_at = lh2 + 30 + fname_len
        rebuilt = bytearray(bytes(raw[:insert_at]) + extra_record + bytes(raw[insert_at:]))
        new_eocd = rebuilt.rindex(b"PK\x05\x06")
        cd_off = struct.unpack_from("<L", rebuilt, new_eocd + 16)[0]
        struct.pack_into("<L", rebuilt, new_eocd + 16, cd_off + len(extra_record))
        path.write_bytes(rebuilt)
    res = batch_import.import_files([path], tmp_path / "LibExtraEnc")
    assert res.imported == 0 and res.errors == [(path.name, "encrypted")]


def test_colon_filenames_and_windows_backslash_separators_import_and_keep_folder_hints(tmp_path):
    archive = zip_file(
        tmp_path / "CrossPlatform.zip",
        [
            ("Notes: Q4/10:30 Standup.opus", ONE.read_bytes()),
            ("Meetings\\", b""),
            ("Meetings\\Afternoon\\14:00 Review.ogg", TWO.read_bytes()),
        ],
        compression=zipfile.ZIP_DEFLATED,
    )
    root = tmp_path / "LibCrossPlatform"
    res = batch_import.import_files([archive], root)
    assert (res.total, res.imported, res.ignored, res.failed) == (2, 2, 0, 0)
    by_name = {}
    for directory in documents(root):
        rec = json.loads((directory / "meeting.json").read_text())["received"]
        by_name[rec["originalName"]] = rec.get("originalFolderName")
    assert by_name == {
        "10:30 Standup.opus": "Notes: Q4",
        "14:00 Review.opus": "Afternoon",
    }


def test_padded_compress_size_bomb_and_second_member_runtime_limit_refuse_and_rollback_archive(tmp_path):
    import zlib

    # Build a >100:1 deflate stream (~45 bytes -> 15,000 bytes), then pad 900 trailing bytes after Z_STREAM_END
    # so declared compress_size (~945) vs file_size (15,000) looks like ~16:1 in Central Directory.
    bomb_plain = b"B" * 15_000
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = comp.compress(bomb_plain) + comp.flush()
    assert len(bomb_plain) > len(deflated) * 100
    padded = deflated + (b"\x00" * 1_000)
    assert len(bomb_plain) <= len(padded) * 100

    # Put a valid .opus FIRST in the same archive: whole-archive refusal must leave 0 notes from this archive!
    good_bytes = ONE.read_bytes()
    good_crc = zlib.crc32(good_bytes) & 0xFFFFFFFF
    good_name = b"FirstValid.opus"
    lh1 = struct.pack(
        "<4s5H3L2H", b"PK\x03\x04", 20, 0, 0, 0, 0, good_crc, len(good_bytes), len(good_bytes), len(good_name), 0
    )
    entry1 = lh1 + good_name + good_bytes

    bomb_crc = zlib.crc32(bomb_plain) & 0xFFFFFFFF
    bomb_name = b"PaddedBomb.opus"
    lh2 = struct.pack(
        "<4s5H3L2H", b"PK\x03\x04", 20, 0, 8, 0, 0, bomb_crc, len(padded), len(bomb_plain), len(bomb_name), 0
    )
    lh2_offset = len(entry1)
    entry2 = lh2 + bomb_name + padded

    cd_offset = len(entry1) + len(entry2)
    ch1 = (
        struct.pack(
            "<4s6H3L5H2L",
            b"PK\x01\x02",
            20,
            20,
            0,
            0,
            0,
            0,
            good_crc,
            len(good_bytes),
            len(good_bytes),
            len(good_name),
            0,
            0,
            0,
            0,
            0,
            0,
        )
        + good_name
    )
    ch2 = (
        struct.pack(
            "<4s6H3L5H2L",
            b"PK\x01\x02",
            20,
            20,
            0,
            8,
            0,
            0,
            bomb_crc,
            len(padded),
            len(bomb_plain),
            len(bomb_name),
            0,
            0,
            0,
            0,
            0,
            lh2_offset,
        )
        + bomb_name
    )
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 2, 2, len(ch1) + len(ch2), cd_offset, 0)
    archive_path = tmp_path / "PaddedBomb.zip"
    archive_path.write_bytes(entry1 + entry2 + ch1 + ch2 + eocd)

    # Seed one pre-existing note in Library to verify rollback never touches pre-existing documents.
    root = tmp_path / "LibRollback"
    existing = received.import_received(TWO, root)
    res = batch_import.import_files([archive_path], root)
    assert res.imported == 0 and res.failed == 1
    assert res.errors == [("PaddedBomb.zip", "limit")]
    assert [d.name for d in documents(root)] == [existing.directory.name]

    # Event-gated overlap: while member 1 is staged and before member 2 triggers runtime limit rollback,
    # a concurrent single-file import of the identical recording must succeed and survive rollback.
    overlap_root = tmp_path / "LibOverlapRollback"
    first_staged, release_bomb = threading.Event(), threading.Event()
    archive_outcome = []

    def gated_progress(state):
        if state.imported == 1 and not first_staged.is_set():
            first_staged.set()
            assert release_bomb.wait(30), "overlap gate timed out"

    worker = threading.Thread(
        target=lambda: archive_outcome.append(
            batch_import.import_files([archive_path], overlap_root, progress=gated_progress)
        )
    )
    worker.start()
    try:
        assert first_staged.wait(30), "first archive member was never staged"
        assert documents(overlap_root) == [], "uncommitted archive member became visible before validation completed"
        standalone = received.import_received(ONE, overlap_root)
        assert [d.name for d in documents(overlap_root)] == [standalone.directory.name]
    finally:
        release_bomb.set()
        worker.join(30)
    assert len(archive_outcome) == 1
    assert archive_outcome[0].imported == 0 and archive_outcome[0].failed == 1
    assert archive_outcome[0].errors == [("PaddedBomb.zip", "limit")]
    assert [d.name for d in documents(overlap_root)] == [standalone.directory.name]
    assert (standalone.directory / "001-minimal-v1.opus").read_bytes() == ONE.read_bytes()
    assert not list(overlap_root.glob(".received-*"))


def test_concurrent_imports_update_digest_cache_and_deduplicate_on_archive_commit(tmp_path):
    # Case 1: Batch [ONE, TWO] paused after ONE; standalone import of TWO runs before batch continues to TWO.
    root = tmp_path / "LibConcurrentBatch"
    after_one, proceed_two = threading.Event(), threading.Event()
    batch_results = []

    def on_batch_progress(state):
        if state.imported == 1 and not after_one.is_set():
            after_one.set()
            assert proceed_two.wait(30), "batch gate timed out"

    t_batch = threading.Thread(
        target=lambda: batch_results.append(batch_import.import_files([ONE, TWO], root, progress=on_batch_progress))
    )
    t_batch.start()
    try:
        assert after_one.wait(30), "batch did not finish first item"
        standalone_two = received.import_received(TWO, root)
        assert standalone_two.directory.exists()
    finally:
        proceed_two.set()
        t_batch.join(30)
    assert len(batch_results) == 1
    assert (batch_results[0].imported, batch_results[0].duplicates, batch_results[0].failed) == (1, 1, 0)
    assert len(documents(root)) == 2
    assert batch_results[0].last_key == standalone_two.key

    # Case 2: Valid archive [ONE, TWO] paused after staging ONE; standalone import of ONE runs before archive commits.
    valid_zip = zip_file(
        tmp_path / "ValidPair.zip", [("First.opus", ONE.read_bytes()), ("Second.opus", TWO.read_bytes())]
    )
    root_zip = tmp_path / "LibConcurrentZip"
    staged_one, proceed_zip = threading.Event(), threading.Event()
    zip_results = []

    def on_zip_progress(state):
        if state.imported == 1 and not staged_one.is_set():
            staged_one.set()
            assert proceed_zip.wait(30), "zip gate timed out"

    t_zip = threading.Thread(
        target=lambda: zip_results.append(batch_import.import_files([valid_zip], root_zip, progress=on_zip_progress))
    )
    t_zip.start()
    try:
        assert staged_one.wait(30), "archive did not stage first item"
        assert documents(root_zip) == []
        standalone_one = received.import_received(ONE, root_zip)
        assert standalone_one.directory.exists()
    finally:
        proceed_zip.set()
        t_zip.join(30)
    assert len(zip_results) == 1
    assert (zip_results[0].imported, zip_results[0].duplicates, zip_results[0].failed) == (1, 1, 0)
    assert len(documents(root_zip)) == 2
    assert not list(root_zip.glob(".received-*"))
