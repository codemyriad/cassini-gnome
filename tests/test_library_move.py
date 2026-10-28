# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-folder publication and failure safety, without changing the app's location."""

import errno
import shutil
import tempfile
from pathlib import Path

import pytest

from cassini_viewer import library_move

NAME = "Recording-10000000-0000-4000-8000-000000000001"
SECOND = "Received-20000000-0000-4000-8000-000000000002"


def folder(root, name=NAME):
    path = root / name
    path.mkdir(parents=True)
    (path / "session.json").write_bytes(b'{"opaque":"preserve exact bytes"}\n')
    (path / "track.wav").write_bytes(bytes(range(256)) * 2000)
    return path


def test_same_filesystem_moves_one_complete_folder_and_preserves_extras(tmp_path):
    root, target = tmp_path / "source", tmp_path / "target"
    source = folder(root)
    (root / "unrelated.txt").write_text("keep")
    (source / "extra").mkdir()
    (source / "extra/notes.txt").write_text("opaque addon data")
    result = library_move.move_meetings(root, target)
    assert len(result.moved) == 1 and not result.failed
    assert not source.exists()
    assert (target / NAME / "extra/notes.txt").read_text() == "opaque addon data"
    assert (target / NAME / "track.wav").read_bytes() == bytes(range(256)) * 2000
    assert (root / "unrelated.txt").read_text() == "keep"


def test_existing_destination_is_never_replaced_and_failed_source_stays(tmp_path):
    root, target = tmp_path / "source", tmp_path / "target"
    source = folder(root)
    existing = folder(target)
    (existing / "session.json").write_bytes(b"different meeting")
    result = library_move.move_meetings(root, target)
    assert len(result.failed) == 1 and not result.moved
    assert (existing / "session.json").read_bytes() == b"different meeting"
    assert (source / "session.json").read_bytes() == b'{"opaque":"preserve exact bytes"}\n'


def test_deferred_live_folder_is_untouched_while_other_meeting_moves(tmp_path):
    root, target = tmp_path / "source", tmp_path / "target"
    live = folder(root)
    folder(root, SECOND)
    result = library_move.move_meetings(root, target, deferred={NAME})
    assert result.deferred == (live,)
    assert live.is_dir() and (target / SECOND).is_dir() and not (target / NAME).exists()


def cross_filesystem(monkeypatch, source):
    rename = library_move._rename

    def force_cross_device(old, new):
        if old == source and new.name == NAME:
            raise OSError(errno.EXDEV, "Cross-device link")
        return rename(old, new)

    monkeypatch.setattr(library_move, "_rename", force_cross_device)


def test_cross_filesystem_copy_checks_bytes_and_publishes_once(tmp_path, monkeypatch):
    root, target = tmp_path / "source", tmp_path / "target"
    source = folder(root)
    (source / "link").symlink_to("track.wav")
    cross_filesystem(monkeypatch, source)
    result = library_move.move_meetings(root, target)
    assert len(result.moved) == 1 and not result.failed
    assert not source.exists()
    assert (target / NAME / "track.wav").read_bytes() == bytes(range(256)) * 2000
    assert (target / NAME / "link").is_symlink()
    assert not list(target.glob(".cassini-moving-*"))


def test_copy_failure_or_concurrent_source_edit_keeps_original(tmp_path, monkeypatch):
    root, target = tmp_path / "source", tmp_path / "target"
    source = folder(root)
    cross_filesystem(monkeypatch, source)
    original = library_move._copy_file

    def changed(old, new):
        result = original(old, new)
        (source / "new-note").write_bytes(b"new data after copy began")
        return result

    monkeypatch.setattr(library_move, "_copy_file", changed)
    result = library_move.move_meetings(root, target)
    assert len(result.failed) == 1 and source.is_dir() and not (target / NAME).exists()
    assert (source / "new-note").read_bytes() == b"new data after copy began"
    assert not list(target.glob(".cassini-moving-*"))


def test_symlinked_meeting_is_not_followed_or_moved(tmp_path):
    root, target = tmp_path / "source", tmp_path / "target"
    outside = folder(tmp_path / "outside")
    root.mkdir()
    (root / NAME).symlink_to(outside, target_is_directory=True)
    result = library_move.move_meetings(root, target)
    assert not result.moved and outside.is_dir() and (root / NAME).is_symlink()
    assert not (target / NAME).exists()


def test_destination_inside_a_meeting_is_rejected_before_changes(tmp_path):
    root = tmp_path / "source"
    source = folder(root)
    with pytest.raises(ValueError, match="inside"):
        library_move.move_meetings(root, source / "new-folder")
    assert source.is_dir() and not (source / "new-folder").exists()


def test_real_cross_device_move_publishes_identical_bytes(tmp_path):
    shared = Path("/dev/shm")
    if (
        not shared.is_dir()
        or shared.stat().st_dev == tmp_path.stat().st_dev
        or shutil.disk_usage(shared).free < 1024**2
    ):
        pytest.skip("A separate writable temporary filesystem is unavailable")
    source = folder(tmp_path / "source")
    expected = {p.name: p.read_bytes() for p in source.iterdir()}
    with tempfile.TemporaryDirectory(prefix="cassini-move-test-", dir=shared) as temporary:
        target = Path(temporary)
        result = library_move.move_meetings(source.parent, target)
        assert len(result.moved) == 1 and not result.failed
        assert not source.exists()
        assert {p.name: p.read_bytes() for p in (target / NAME).iterdir()} == expected


def test_failed_destination_sync_keeps_source_and_removes_unrouted_copy(tmp_path, monkeypatch):
    root, target = tmp_path / "source", tmp_path / "target"
    source = folder(root)
    cross_filesystem(monkeypatch, source)
    sync = library_move._sync

    def fail_publication(path):
        if path == target and (target / NAME).exists():
            raise OSError(errno.EIO, "Destination sync failed")
        sync(path)

    monkeypatch.setattr(library_move, "_sync", fail_publication)
    result = library_move.move_meetings(root, target)
    assert len(result.failed) == 1 and not result.moved
    assert source.is_dir() and (source / "track.wav").stat().st_size == 512000
    assert not (target / NAME).exists() and not list(target.glob(".cassini-moving-*"))


def test_full_destination_preserves_source_before_copy(tmp_path, monkeypatch):
    root, target = tmp_path / "source", tmp_path / "target"
    source = folder(root)
    cross_filesystem(monkeypatch, source)
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(library_move.shutil, "disk_usage", lambda _: usage._replace(free=1))
    result = library_move.move_meetings(root, target)
    assert len(result.failed) == 1 and source.is_dir()
    assert not list(target.iterdir())
