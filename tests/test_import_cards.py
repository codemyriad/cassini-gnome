# SPDX-License-Identifier: GPL-3.0-or-later
"""L-25 W4.4-W4.6: drop classification from the shared types list, Importing… and
failed-import (Needs attention) list cards, without rebuilding meeting rows."""

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("GTK/Adwaita window integration runs in distro jobs", allow_module_level=True)
from gi.repository import Gio  # noqa: E402

from cassini_talk.library_window import LibraryWindow  # noqa: E402


class FakeRows:
    def __init__(self):
        self.children = []

    def insert(self, widget, index):
        self.children.insert(index, widget)

    def remove(self, widget):
        self.children.remove(widget)


class Card:
    def __init__(self, kind, *values):
        self.kind, self.values, self.parent = kind, values, None

    def get_parent(self):
        return self.parent


def window_with_cards():
    rows = FakeRows()
    window = SimpleNamespace(
        now_ms=1_793_107_080_000,
        tz="UTC",
        rows=rows,
        monitor=SimpleNamespace(_closed=False),
        snapshot=None,
        _source_snapshot=None,
        _active_imports={},
        _failed_imports={},
        _import_row_widgets=[],
        _rendered_imports=(),
        _rows_layout=None,
        _list_signature=("library", ()),
        opened=[],
    )
    window._day_heading = lambda title: Card("heading", title)

    def importing(stem, filename, started_ms):
        card = Card("importing", stem, filename)
        card.parent = rows
        return card

    def failed(stem, filename, failed_ms, raw):
        card = Card("failed", stem, filename, raw)
        card.parent = rows
        return card

    window._importing_row = importing
    window._failed_import_row = failed
    window.open_files = lambda files: window.opened.append(tuple(f.get_path() for f in files))
    for name in (
        "_sync_import_rows",
        "_clear_import_rows",
        "_begin_import_entries",
        "_finish_import_entries",
        "_add_failed_imports",
        "_dismiss_failed_import",
        "_retry_failed_import",
    ):
        setattr(window, name, getattr(LibraryWindow, name).__get__(window))
    window._queue_sidebar_scrolled = lambda: None
    return window


def kinds(window):
    return [(c.kind, *c.values[:2]) for c in window.rows.children if c.kind != "heading"]


def test_failed_import_keeps_a_needs_attention_card_until_retried_or_dismissed(tmp_path):
    window = window_with_cards()
    token = object()
    a, b = str(tmp_path / "Weekly sync.mp3"), str(tmp_path / "Board.m4a")
    window._begin_import_entries(token, (a, b))
    assert kinds(window) == [("importing", "Weekly sync", "Weekly sync.mp3"), ("importing", "Board", "Board.m4a")]

    window._finish_import_entries(token)
    window._add_failed_imports((b,))
    assert kinds(window) == [("failed", "Board", "Board.m4a")]

    # A new import of other files keeps the failed card below the active ones.
    other = object()
    window._begin_import_entries(other, (a,))
    assert kinds(window) == [("importing", "Weekly sync", "Weekly sync.mp3"), ("failed", "Board", "Board.m4a")]
    window._finish_import_entries(other)

    # Try again replaces the card with a new import of the same path.
    window._retry_failed_import(b)
    assert window.opened == [(b,)] and window._failed_imports == {}
    assert kinds(window) == []

    window._add_failed_imports((a, b))
    window._dismiss_failed_import(a)
    assert kinds(window) == [("failed", "Board", "Board.m4a")]
    # Starting the same file again removes its stale failed card.
    window._begin_import_entries(object(), (b,))
    assert kinds(window) == [("importing", "Board", "Board.m4a")]


def test_cancelled_import_leaves_no_failed_card(tmp_path):
    window = window_with_cards()
    token = object()
    window._begin_import_entries(token, (str(tmp_path / "talk.wav"),))
    window._finish_import_entries(token)
    assert kinds(window) == [] and window._failed_imports == {}


@pytest.mark.parametrize(
    "name, verdict",
    [
        ("meeting.opus", "valid"),
        ("talk.mp3", "valid"),
        ("talk.M4A", "valid"),
        ("talk.wav", "valid"),
        ("song.ogg", "valid"),
        ("Notes.zip", "valid"),
        ("clip.ogv", "invalid"),
        ("song.flac", "valid"),
        ("song.oga", "invalid"),
        ("video.mp4", "invalid"),
    ],
)
def test_drop_classification_follows_the_shared_types_list(tmp_path, name, verdict):
    result = LibraryWindow._classify_drop_file_sync(Gio.File.new_for_path(str(tmp_path / name)))
    assert result[0] == verdict
    if verdict == "invalid":
        assert result[1] == name


def test_invalid_flac_import_lands_as_a_needs_attention_card_like_invalid_ogg(tmp_path):
    from cassini_talk import batch_import
    from cassini_talk.meeting_state import import_entry_badge

    ogg_flac = tmp_path / "Renamed.flac"
    ogg_flac.write_bytes(b"OggS\x00\x02" + bytes(20) + b"\x01\x2d\x7fFLAC" + bytes(64))
    zero_rate = tmp_path / "Zero rate.flac"
    zero_rate.write_bytes(b"fLaC\x00\x00\x00\x22" + bytes(34) + bytes(256))
    bad_ogg = tmp_path / "Video.ogg"
    bad_ogg.write_bytes(b"OggS\x00\x02" + bytes(20) + b"\x01\x2a\x80theora" + bytes(64))
    window = window_with_cards()
    result = batch_import.import_files([ogg_flac, zero_rate, bad_ogg], tmp_path / "Library")
    assert (result.imported, result.failed) == (0, 3)
    window._add_failed_imports(result.failed_inputs)
    assert kinds(window) == [
        ("failed", "Renamed", "Renamed.flac"),
        ("failed", "Zero rate", "Zero rate.flac"),
        ("failed", "Video", "Video.ogg"),
    ]
    assert import_entry_badge({"failed": True}).kind == "needsAttention"


def test_gio_content_types_for_flac_follow_the_contract():
    from cassini_talk import import_types

    # What GIO reports for a .flac by name (shared-mime-info: audio/flac, alias audio/x-flac).
    by_name, _uncertain = Gio.content_type_guess("song.flac", None)
    assert by_name in ("audio/flac", "audio/x-flac")
    assert import_types.acceptable_name("song.flac", by_name)
    # GIO's Ogg FLAC type and its broad Ogg parents never pass for .flac or the Ogg extensions.
    for mime in ("audio/x-flac+ogg", "audio/x-oggflac"):
        assert not Gio.content_type_is_a(mime, by_name)
        for ext in (".flac", ".ogg", ".opus", ".oga"):
            assert not import_types.acceptable_name("song" + ext, mime)
    for mime in ("audio/ogg", "application/ogg"):
        assert not import_types.acceptable_name("song.flac", mime)


# ---- D-1059: single-file open / Try again feedback is catalog copy, never raw text ----


def damaged_mp3(tmp_path, name="Corrupt G.mp3"):
    """3 KB that no decoder types as audio (deterministic, unlike /dev/urandom)."""
    import hashlib

    path = tmp_path / name
    block = b"".join(hashlib.sha256(bytes([i])).digest() for i in range(94))
    path.write_bytes(block[:3000].replace(b"\xff", b"\x00"))
    return path


def opened_future(path, root):
    from concurrent.futures import Future

    from cassini_talk.received import import_one

    future = Future()
    try:
        meeting, added = import_one(path, root)
    except Exception as error:  # noqa: BLE001 - delivered to _opened_file as the worker would
        future.set_exception(error)
    else:
        future.set_result((meeting, True, added))
    return future


def window_for_open(tmp_path):
    window = window_with_cards()
    window.toasts = []
    window.toast = window.toasts.append
    window._opening_files = 1
    window._import_cancels = set()
    window._opened_file = LibraryWindow._opened_file.__get__(window)
    return window


@pytest.mark.parametrize("lang", ["en", "it", "fr", "es", "pt-BR", "pt-PT"])
@pytest.mark.parametrize("name", ["notes.txt", "clip.oga", "..opus", "README"])
def test_unsupported_open_says_ignored_in_catalog_copy_and_keeps_no_retry_card(tmp_path, lang, name):
    from cassini_talk.i18n import using_language
    from cassini_talk.strings import plural

    source = tmp_path / name
    source.write_text("hello\n")
    window = window_for_open(tmp_path)
    token = object()
    window._begin_import_entries(token, (str(source),))
    with using_language(lang):
        window._opened_file(opened_future(source, tmp_path / "Library"), Gio.File.new_for_path(str(source)), token)
        expected = plural("import.summary.unsupported", 1)
    assert window.toasts == [expected]
    assert "Choose an Ogg Opus recording" not in window.toasts
    # Try again could never succeed: no Needs attention card, and nothing staged.
    assert window._failed_imports == {} and kinds(window) == []
    assert not (tmp_path / "Library").exists()


@pytest.mark.parametrize("lang", ["en", "it", "fr", "es", "pt-BR", "pt-PT"])
def test_damaged_audio_open_and_try_again_show_catalog_failure_never_the_raw_decoder_error(tmp_path, lang, caplog):
    from cassini_talk.i18n import using_language
    from cassini_talk.strings import plural

    source = damaged_mp3(tmp_path)
    window = window_for_open(tmp_path)
    raw = []
    with using_language(lang):
        expected = plural("import.summary.failed", 1)
        for _attempt in ("open", "Try again"):
            token = object()
            window._begin_import_entries(token, (str(source),))
            future = opened_future(source, tmp_path / "Library")
            raw.append(str(future.exception()))
            with caplog.at_level("WARNING", logger="cassini_talk.library_window"):
                window._opened_file(future, Gio.File.new_for_path(str(source)), token)
            window._opening_files = 1
            # The damaged supported file stays retryable (W4.6, as for a truncated FLAC).
            assert kinds(window) == [("failed", "Corrupt G", "Corrupt G.mp3")]
    assert window.toasts == [expected, expected]
    assert raw[0] and all(r not in toast for r in raw for toast in window.toasts)
    # The raw diagnostic stays available in the log only.
    assert sum(raw[0] in record.getMessage() for record in caplog.records) == 2


@pytest.mark.parametrize("lang", ["en", "it", "fr", "es", "pt-BR", "pt-PT"])
def test_failed_folder_open_shows_catalog_copy_not_the_reader_error(tmp_path, lang):
    from concurrent.futures import Future

    from cassini_talk.i18n import using_language
    from cassini_talk.strings import text

    window = window_for_open(tmp_path)
    future = Future()
    future.set_exception(ValueError("session.json: Expecting value: line 1 column 1 (char 0)"))
    with using_language(lang):
        window._opened_file(future, Gio.File.new_for_path(str(tmp_path / "session.json")))
        assert window.toasts == [text("details.folderUnavailable")]
    assert window._failed_imports == {}


# ---- D-1059 / BUG-CLASSES #15: a full disk says so and keeps Try again ----


@pytest.mark.parametrize("lang", ["en", "it", "fr", "es", "pt-BR", "pt-PT"])
def test_storage_full_open_says_free_space_and_try_again_works_after(tmp_path, lang, caplog):
    import errno
    from concurrent.futures import Future

    from cassini_talk.i18n import using_language
    from cassini_talk.strings import plural, text

    source = tmp_path / "Weekly sync.mp3"
    source.write_bytes(b"ID3" + bytes(64))
    window = window_for_open(tmp_path)
    token = object()
    window._begin_import_entries(token, (str(source),))
    future = Future()
    future.set_exception(OSError(errno.ENOSPC, "No space left on device"))
    with using_language(lang):
        with caplog.at_level("WARNING", logger="cassini_talk.library_window"):
            window._opened_file(future, Gio.File.new_for_path(str(source)), token)
        assert window.toasts == [text("import.failed.diskFull")]
        assert window.toasts != [plural("import.summary.failed", 1)]
    assert all("No space left on device" not in toast and "Errno" not in toast for toast in window.toasts)
    # The raw OS text stays a log diagnostic only.
    assert any("No space left on device" in record.getMessage() for record in caplog.records)
    # The Needs attention card stays, so Try again after freeing space reimports the file.
    assert kinds(window) == [("failed", "Weekly sync", "Weekly sync.mp3")]
    window._retry_failed_import(str(source))
    assert window.opened == [(str(source),)] and window._failed_imports == {}


def test_storage_full_in_a_folder_open_says_free_space(tmp_path):
    import errno
    from concurrent.futures import Future

    from cassini_talk.strings import text

    window = window_for_open(tmp_path)
    future = Future()
    future.set_exception(OSError(errno.ENOSPC, "No space left on device"))
    window._opened_file(future, Gio.File.new_for_path(str(tmp_path / "session.json")))
    assert window.toasts == [text("import.failed.diskFull")]
    assert window._failed_imports == {}


def test_audio_conversion_filling_the_disk_says_free_space_and_keeps_try_again(tmp_path, monkeypatch):
    """A real GStreamer filesink ENOSPC during conversion reaches the toast as storage-full."""
    import os
    from concurrent.futures import Future

    from test_import_types import needs_encoder, write_wav

    from cassini_talk import audio_import, received
    from cassini_talk.strings import text

    if needs_encoder.args[0] or not os.path.exists("/dev/full"):
        pytest.skip("GStreamer Opus encode path or /dev/full unavailable")
    real = audio_import.decode_to_opus

    def decode(source, output, **kwargs):
        os.symlink("/dev/full", output)
        return real(source, output, **kwargs)

    monkeypatch.setattr(received, "decode_to_opus", decode)
    source = write_wav(tmp_path / "Weekly sync.wav")
    future = Future()
    try:
        received.import_one(source, tmp_path / "Library")
    except (OSError, ValueError) as error:
        future.set_exception(error)
    window = window_for_open(tmp_path)
    token = object()
    window._begin_import_entries(token, (str(source),))
    window._opened_file(future, Gio.File.new_for_path(str(source)), token)
    assert window.toasts == [text("import.failed.diskFull")]
    assert kinds(window) == [("failed", "Weekly sync", "Weekly sync.wav")]
