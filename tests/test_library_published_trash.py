# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - optional encoder/runtime dependencies precede product imports.
"""Actual app Trash dispatch: shared intent, file before cache, durable cleanup."""

import shutil
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("Managed Trash integration runs with the full app runtime", allow_module_level=True)

from library_v2_support import simple_session

from cassini_viewer.library import LibraryScanner
from cassini_viewer.library_v2 import finalize, journal, publish
from cassini_viewer.meeting_trash import trash_owned
from cassini_viewer.trash_cleanup import TrashCleanup


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path / "Library"
    root.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sid, directory = simple_session(root, 0.1)
    result = finalize.finalize(root, sid)
    calls = []
    destination = tmp_path / "private-test-trash"
    destination.mkdir()

    def native_trash(path):
        calls.append(path)
        path.rename(destination / path.name)

    monkeypatch.setattr(publish, "TRASH", native_trash)
    removed = []
    voices = SimpleNamespace(root=tmp_path / "private/voices", remove_meeting=removed.append)
    cleanup = TrashCleanup(voices)
    return root, sid, directory, root / result.name, calls, removed, cleanup, native_trash


def test_app_trash_moves_file_before_working_cache_and_cleans_private_links(managed):
    root, sid, directory, file, calls, removed, cleanup, _trash = managed
    assert trash_owned(directory, cleanup=cleanup) == directory
    assert calls == [file, directory] and removed == [directory]
    assert not file.exists() and not directory.exists()
    assert not LibraryScanner(root).scan().meetings
    assert cleanup._read() == [] and not journal.pending(journal.Layout(root)).entries


def test_partial_trash_keeps_delete_intent_and_private_cleanup_until_replay(managed, monkeypatch):
    root, sid, directory, file, calls, removed, cleanup, native = managed

    def fail_cache(path):
        if path == directory:
            raise OSError("synthetic folder Trash failure after file moved")
        native(path)

    monkeypatch.setattr(publish, "TRASH", fail_cache)
    with pytest.raises(OSError, match="folder Trash failure"):
        trash_owned(directory, cleanup=cleanup)
    assert calls == [file] and directory.exists() and not file.exists()
    assert cleanup._read() == [str(directory)] and not removed
    assert len(journal.pending(journal.Layout(root)).entries) == 1
    assert not LibraryScanner(root).scan().meetings  # Intent suppresses every representation.
    cleanup.retry()
    assert cleanup._read() == [str(directory)] and not removed
    monkeypatch.setattr(publish, "TRASH", native)
    assert not journal.recover(root).kept
    cleanup.retry()
    assert calls == [file, directory] and removed == [directory]
    assert cleanup._read() == [] and not directory.exists()
    assert not journal.pending(journal.Layout(root)).entries


def test_app_trash_handles_a_file_with_no_working_folder(managed):
    root, sid, directory, file, calls, removed, cleanup, _trash = managed
    shutil.rmtree(directory)
    assert trash_owned(directory, cleanup=cleanup) == directory
    assert calls == [file] and removed == [directory]
    assert not file.exists() and not directory.exists()
    assert not LibraryScanner(root).scan().meetings
