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
