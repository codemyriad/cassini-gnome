# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - fixture dependencies precede encoder imports.
"""D15 moves the root Cassini file together with its optional working session."""

import shutil

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Real publication requires GStreamer", allow_module_level=True)

from library_v2_support import simple_session

from cassini_viewer import library_location as location
from cassini_viewer.library import LibraryCollectionScanner
from cassini_viewer.library_v2 import finalize


def setup(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(old))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    sid, directory = simple_session(old, 0.1)
    return old, new, sid, directory


@pytest.mark.parametrize("session_exists", [True, False])
def test_move_publishes_both_representations_and_retires_routing_only_when_complete(
    tmp_path, monkeypatch, session_exists
):
    old, new, sid, directory = setup(tmp_path, monkeypatch)
    result = finalize.finalize(old, sid)
    file = old / result.name
    before = file.read_bytes()
    if not session_exists:
        shutil.rmtree(directory)
    location.request(new)
    assert location.primary() == new
    assert location.process()["pending"] == []
    assert not file.exists() and (new / result.name).read_bytes() == before
    assert not directory.exists()
    assert (new / ".cassini/sessions" / directory.name).is_dir() is session_exists
    (meeting,) = LibraryCollectionScanner().scan().meetings
    assert meeting.audio_file == new / result.name and not meeting.published_missing
    assert location.owned_directory(directory) == new / ".cassini/sessions" / directory.name


def test_stop_after_move_routes_and_moves_the_newly_published_pair(tmp_path, monkeypatch):
    old, new, sid, directory = setup(tmp_path, monkeypatch)
    location.request(new)  # The recording/saving meeting is still at its old root.
    result = finalize.finalize(old, sid)
    assert location.process()["pending"] == []
    assert not directory.exists() and not (old / result.name).exists()
    (meeting,) = LibraryCollectionScanner().scan().meetings
    assert meeting.audio_file == new / result.name and not meeting.published_missing


def _crash_move(root, config, point, ready):
    import os
    import signal

    from cassini_viewer import library_move_pair

    os.environ["CASSINI_LIBRARY_DIR"] = str(root)
    os.environ["XDG_CONFIG_HOME"] = str(config)

    def checkpoint(name):
        if name == point:
            ready.send(name)
            signal.pause()  # Parent delivers SIGKILL, never a timer-based release.

    library_move_pair.CHECKPOINT = checkpoint
    location.process()


@pytest.mark.parametrize(
    "point",
    [
        "staged",
        "file-published",
        "session-published",
        "published",
        "source-file-retired",
        "source-session-retired",
        "retired",
        "cleaned",
    ],
)
def test_sigkill_replays_each_paired_move_boundary_without_duplicate_rows(tmp_path, monkeypatch, point):
    import multiprocessing

    old, new, sid, directory = setup(tmp_path, monkeypatch)
    result = finalize.finalize(old, sid)
    before = (old / result.name).read_bytes()
    location.request(new)
    context = multiprocessing.get_context("spawn")
    ready, sender = context.Pipe(duplex=False)
    process = context.Process(target=_crash_move, args=(old, tmp_path / "private-config", point, sender))
    try:
        process.start()
        assert ready.poll(10), f"Mover did not reach {point}; exit={process.exitcode}"
        assert ready.recv() == point
        process.kill()
        process.join(5)
        assert not process.is_alive() and process.exitcode < 0
        rows = LibraryCollectionScanner().scan().meetings
        assert len(rows) == 1 and not rows[0].published_missing
        assert location.process()["pending"] == []
        (meeting,) = LibraryCollectionScanner().scan().meetings
        assert meeting.audio_file == new / result.name
        assert meeting.audio_file.read_bytes() == before
        assert not directory.exists() and not (old / result.name).exists()
        assert (new / ".cassini/sessions" / directory.name).is_dir()
        assert not list(new.glob(".cassini-moving-*"))
        assert not list(old.glob(".cassini-moved-*"))
    finally:
        ready.close()
        sender.close()
        if process.is_alive():
            process.kill()
            process.join(5)


@pytest.mark.parametrize("interruption", ["file-published", "published"])
def test_recovery_keeps_a_newer_destination_edit_and_rename(tmp_path, monkeypatch, interruption):
    from cassini_viewer import library_move_pair
    from cassini_viewer.meeting_store import read_metadata, update_metadata

    class Interrupted(BaseException):
        pass

    old, new, sid, directory = setup(tmp_path, monkeypatch)
    result = finalize.finalize(old, sid)
    location.request(new)

    def crash(point):
        if point == interruption:
            raise Interrupted()

    monkeypatch.setattr(library_move_pair, "CHECKPOINT", crash)
    with pytest.raises(Interrupted):
        location.process()
    target = new / ".cassini/sessions" / directory.name
    if interruption == "published":
        assert location.owned_directory(directory) == target
    update_metadata(target, title="Edited at the destination", titleSource="user")
    from cassini_viewer.library import LibraryScanner

    (edited,) = LibraryScanner(new).scan().meetings
    edited_bytes = edited.audio_file.read_bytes()
    monkeypatch.setattr(library_move_pair, "CHECKPOINT", None)
    assert location.process()["pending"] == []
    (meeting,) = LibraryCollectionScanner().scan().meetings
    assert meeting.title == "Edited at the destination" and meeting.audio_file.read_bytes() == edited_bytes
    assert read_metadata(target)["title"] == meeting.title
    assert not (old / result.name).exists() and not directory.exists()


def test_editor_queued_on_old_library_lock_follows_the_completed_pair(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from cassini_viewer import library_move_pair
    from cassini_viewer.library_v2 import journal
    from cassini_viewer.meeting_store import update_metadata

    old, new, sid, directory = setup(tmp_path, monkeypatch)
    finalize.finalize(old, sid)
    location.request(new)
    waiting = threading.Event()
    errors = []
    original = journal.LibraryLock.__enter__

    def enter(lock):
        if threading.current_thread().name == "queued-editor":
            waiting.set()
        return original(lock)

    def edit():
        try:
            update_metadata(directory, title="Queued edit after Move", titleSource="user")
        except BaseException as error:
            errors.append(error)

    editor = threading.Thread(target=edit, name="queued-editor", daemon=True)

    def pause(point):
        if point == "file-published":
            editor.start()
            assert waiting.wait(5), "Editor did not queue behind the old Library lock"

    monkeypatch.setattr(journal.LibraryLock, "__enter__", enter)
    monkeypatch.setattr(library_move_pair, "CHECKPOINT", pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(location.process).result(timeout=10)["pending"] == []
    editor.join(5)
    assert not editor.is_alive(), "Queued editor deadlocked after the move"
    assert errors == []
    (meeting,) = LibraryCollectionScanner().scan().meetings
    assert meeting.title == "Queued edit after Move" and meeting.audio_file.parent == new
    assert not directory.exists() and not list(old.glob("*.opus"))


def test_copy_is_outside_both_library_locks_and_a_changed_source_retries(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from cassini_viewer import library_move_pair
    from cassini_viewer.library_v2 import journal
    from cassini_viewer.meeting_store import update_metadata

    old, new, sid, directory = setup(tmp_path, monkeypatch)
    finalize.finalize(old, sid)
    location.request(new)
    entered, release = threading.Event(), threading.Event()
    original = library_move_pair._copy

    def paused(source, target, **kwargs):
        if str(target).endswith("/audio.opus"):
            entered.set()
            assert release.wait(5), "Test did not release the staged audio copy"
        return original(source, target, **kwargs)

    monkeypatch.setattr(library_move_pair, "_copy", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        moving = executor.submit(location.process)
        try:
            assert entered.wait(5), "Audio staging did not start"
            with journal.LibraryLock(journal.Layout(old), blocking=False):
                pass
            with journal.LibraryLock(journal.Layout(new), blocking=False):
                pass
            update_metadata(directory, title="Edited while copying", titleSource="user")
        finally:
            release.set()
        state = moving.result(timeout=10)
    assert len(state["pending"]) == 1 and directory.exists() and not list(new.glob("*.opus"))
    monkeypatch.setattr(library_move_pair, "_copy", original)
    assert location.process()["pending"] == []
    (meeting,) = LibraryCollectionScanner().scan().meetings
    assert meeting.title == "Edited while copying" and meeting.audio_file.parent == new


@pytest.mark.parametrize("failure", ["source-edit", "collision", "cancel"])
def test_unpublished_stage_is_discarded_and_originals_survive(tmp_path, monkeypatch, failure):
    import threading

    from cassini_viewer import library_move_pair
    from cassini_viewer.meeting_store import update_metadata

    old, new, sid, directory = setup(tmp_path, monkeypatch)
    result = finalize.finalize(old, sid)
    location.request(new)
    cancelled = threading.Event()
    original_copy = library_move_pair._copy
    collision = new / result.name

    def stage_copy(source, target, **kwargs):
        copied = original_copy(source, target, **kwargs)
        if str(target).endswith("/audio.opus"):
            if failure == "collision":
                collision.write_bytes(b"Unrelated destination must survive")
            elif failure == "cancel":
                cancelled.set()
        return copied

    def after_stage(point):
        if point == "staged" and failure == "source-edit":
            update_metadata(directory, title="Changed before commit", titleSource="user")

    monkeypatch.setattr(library_move_pair, "_copy", stage_copy)
    monkeypatch.setattr(library_move_pair, "CHECKPOINT", after_stage)
    state = location.process(cancelled=cancelled)
    assert len(state["pending"]) == 1 and directory.exists() and list(old.glob("*.opus"))
    assert not list(new.glob(".cassini-moving-*"))
    if failure == "collision":
        assert collision.read_bytes() == b"Unrelated destination must survive"
        collision.unlink()
    else:
        assert not list(new.glob("*.opus"))
    cancelled.clear()
    monkeypatch.setattr(library_move_pair, "_copy", original_copy)
    monkeypatch.setattr(library_move_pair, "CHECKPOINT", None)
    assert location.process(retry=failure == "collision")["pending"] == []
    (meeting,) = LibraryCollectionScanner().scan().meetings
    assert meeting.audio_file.parent == new
    if failure == "source-edit":
        assert meeting.title == "Changed before commit"


def test_cross_filesystem_copy_verifies_bytes_and_same_filesystem_links_closed_audio(tmp_path, monkeypatch):
    import errno

    from cassini_viewer import library_move_pair

    old, new, sid, directory = setup(tmp_path, monkeypatch)
    result = finalize.finalize(old, sid)
    original_bytes = (old / result.name).read_bytes()
    original_link = library_move_pair.os.link

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "Test cross-filesystem audio link")

    monkeypatch.setattr(library_move_pair.os, "link", cross_device)
    location.request(new)
    assert location.process()["pending"] == []
    assert (new / result.name).read_bytes() == original_bytes
    monkeypatch.setattr(library_move_pair.os, "link", original_link)
