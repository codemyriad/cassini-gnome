# SPDX-License-Identifier: GPL-3.0-or-later
"""D-1059: an unsupported file is ignored (never a retryable failure), a damaged
supported file is a failure, and neither carries user-facing English."""

import hashlib

import pytest

from cassini_talk import batch_import
from cassini_talk.i18n import LANGUAGES, using_language
from cassini_talk.received import UnsupportedImport, import_one, importable_name
from cassini_talk.strings import ENTRIES, plural


def damaged_mp3(tmp_path):
    path = tmp_path / "Corrupt G.mp3"
    path.write_bytes(b"".join(hashlib.sha256(bytes([i])).digest() for i in range(94))[:3000].replace(b"\xff", b"\x00"))
    return path


@pytest.mark.parametrize("name", ["notes.txt", "README", "clip.oga", "video.ogv", "..opus", ".mp3", "talk.mp4"])
def test_unsupported_name_raises_unsupported_before_touching_the_library(tmp_path, name):
    source = tmp_path / name
    source.write_text("hello\n")
    assert not importable_name(name)
    with pytest.raises(UnsupportedImport):
        import_one(source, tmp_path / "Library")
    assert not (tmp_path / "Library").exists()


@pytest.mark.parametrize("name", ["talk.mp3", "Talk.M4A", "talk.wav", "talk.flac", "talk.opus", "talk.ogg"])
def test_decided_names_are_importable(name):
    assert importable_name(name)


def test_missing_supported_file_is_a_failure_not_unsupported(tmp_path):
    with pytest.raises(ValueError) as caught:
        import_one(tmp_path / "Gone.mp3", tmp_path / "Library")
    assert not isinstance(caught.value, UnsupportedImport)


@pytest.mark.parametrize(
    "name, data",
    [
        ("Video.ogg", b"OggS\x00\x02" + bytes(20) + b"\x01\x2a\x80theora" + bytes(64)),
        ("Renamed.flac", b"OggS\x00\x02" + bytes(20) + b"\x01\x2d\x7fFLAC" + bytes(64)),
    ],
)
def test_wrong_content_in_a_supported_name_stays_a_retryable_failure(tmp_path, name, data):
    source = tmp_path / name
    source.write_bytes(data)
    with pytest.raises(ValueError) as caught:
        import_one(source, tmp_path / "Library")
    assert not isinstance(caught.value, UnsupportedImport)
    assert not str(caught.value).startswith("Choose ")
    result = batch_import.import_files([source], tmp_path / "Library")
    assert (result.failed, result.ignored, result.failed_inputs) == (1, 0, [str(source)])


def test_batch_counts_unsupported_as_ignored_and_damaged_as_failed(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("hello\n")
    nameless = tmp_path / "..mp3"
    nameless.write_bytes(damaged_mp3(tmp_path).read_bytes())
    # Damaged without needing a decoder: an .ogg that is not Ogg Opus.
    damaged = tmp_path / "Video.ogg"
    damaged.write_bytes(b"OggS\x00\x02" + bytes(20) + b"\x01\x2a\x80theora" + bytes(64))
    result = batch_import.import_files([notes, nameless, damaged], tmp_path / "Library")
    assert (result.ignored, result.failed) == (2, 1)
    # Only the damaged supported file gets a Needs attention card (W4.6).
    assert result.failed_inputs == [str(damaged)]


def test_received_never_carries_the_stale_opus_only_copy():
    from pathlib import Path

    import cassini_talk.received as received

    assert "Choose an Ogg Opus" not in Path(received.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("lang", LANGUAGES)
def test_single_file_feedback_copy_exists_in_every_language(lang):
    for identifier in ("import.summary.unsupported", "import.summary.failed"):
        entry = ENTRIES[identifier]
        assert "gnome" in entry["platforms"]
        with using_language(lang):
            copy = plural(identifier, 1)
        assert "1" in copy and "{" not in copy
        if lang != "en":
            assert copy == entry["l10n"][lang]["plural"]["one"].format(n="1")


# ---- BUG-CLASSES #15: storage-full is its own message, never "damaged" or raw OS text ----


def no_space():
    import errno

    return OSError(errno.ENOSPC, "No space left on device")


def test_storage_full_is_recognised_directly_and_through_a_wrapped_cause():
    import errno

    from cassini_talk import import_feedback

    assert import_feedback.storage_full(no_space())
    assert import_feedback.storage_full(OSError(errno.EDQUOT, "Disk quota exceeded"))
    try:
        try:
            raise no_space()
        except OSError as error:
            raise ValueError("copy failed") from error
    except ValueError as wrapped:
        assert import_feedback.storage_full(wrapped)
    assert not import_feedback.storage_full(OSError(errno.EACCES, "Permission denied"))
    assert not import_feedback.storage_full(ValueError("damaged"))
    assert import_feedback.failure_reason(no_space()) == "diskFull"
    assert import_feedback.failure_reason(ValueError("damaged")) == "damaged"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_single_storage_full_failure_asks_to_free_space(lang):
    from cassini_talk import import_feedback
    from cassini_talk.strings import text

    entry = ENTRIES["import.failed.diskFull"]
    assert "gnome" in entry["platforms"]
    with using_language(lang):
        copy = import_feedback.single_failure(no_space())
        assert copy == text("import.failed.diskFull")
        assert import_feedback.single_failure(ValueError("x")) == plural("import.summary.failed", 1)
    if lang == "en":
        assert copy == "There isn’t enough space to import this file. Free some space and try again."
    assert "No space left" not in copy and "{" not in copy


@pytest.mark.parametrize("lang", LANGUAGES)
def test_batch_storage_full_keeps_a_retry_card_and_says_free_space(tmp_path, monkeypatch, lang):
    from cassini_talk import import_feedback
    from cassini_talk.strings import text

    def full(*_args, **_kwargs):
        raise no_space()

    monkeypatch.setattr(batch_import, "import_one", full)
    source = tmp_path / "Weekly sync.opus"
    source.write_bytes(b"OggS" + bytes(64))
    result = batch_import.import_files([source], tmp_path / "Library")
    assert (result.failed, result.failed_inputs) == (1, [str(source)])
    assert result.errors == [("Weekly sync.opus", "diskFull")]
    with using_language(lang):
        message = import_feedback.summary(result)
        assert plural("import.summary.failed", 1) in message
        assert text("import.failed.diskFull") in message
    assert "No space left" not in message and "Errno" not in message


def test_zip_member_storage_full_says_free_space_not_damaged(tmp_path, monkeypatch):
    import zipfile

    from cassini_talk import import_feedback
    from cassini_talk.strings import text

    def full(*_args, **_kwargs):
        raise no_space()

    monkeypatch.setattr(batch_import, "stage_one", full)
    archive = tmp_path / "Notes.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Planning.opus", b"OggS" + bytes(64))
    result = batch_import.import_files([archive], tmp_path / "Library")
    assert result.failed == 1 and result.errors == [("Planning.opus", "diskFull")]
    message = import_feedback.summary(result)
    assert text("import.failed.diskFull") in message
    assert text("import.zip.failed.damaged") not in message and "No space left" not in message
