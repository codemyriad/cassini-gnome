# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E402 - runtime-dependent fixtures follow explicit dependency checks.
"""R61: a scanner cannot clean a live writer's uncommitted journal file."""

import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
except ValueError:
    pytest.skip("Publication fixtures require GStreamer", allow_module_level=True)

from library_v2_support import simple_session

from cassini_talk.library import LibraryScanner
from cassini_talk.library_v2 import finalize, fsutil, journal
from cassini_talk.trash_cleanup import TrashCleanup


def _writer(root, sid, name, ready, release, response):
    layout = journal.Layout(Path(root))
    original = os.replace

    def rename(source, destination, *args, **kwargs):
        if str(source).endswith(".json.partial"):
            # write_durable has closed/fsynced the partial. Only its rename is
            # held back; the real cross-process LibraryLock remains held.
            ready.set()
            if not release.wait(10):
                raise TimeoutError("Scanner never released the journal writer")
        return original(source, destination, *args, **kwargs)

    fsutil.os.replace = rename
    try:
        with journal.LibraryLock(layout):
            journal.write(
                layout,
                journal.new_entry(
                    "delete",
                    origin="Recording",
                    session_id=sid,
                    phase="intent",
                    session=layout.relative(layout.session("Recording", sid)),
                    oldPath=name,
                    newPath=None,
                ),
            )
        response.send((True, None))
    except BaseException as error:
        response.send((False, f"{type(error).__name__}: {error}"))
    finally:
        response.close()


@pytest.mark.parametrize("reader", ["scanner", "trash-cleanup"])
def test_read_during_journal_fsync_and_rename_does_not_remove_the_live_partial(tmp_path, monkeypatch, reader):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    sid, directory = simple_session(tmp_path, 0.1)
    published = finalize.finalize(tmp_path, sid)
    cleanup = TrashCleanup(SimpleNamespace(root=tmp_path / "private-voices/store"))
    cleanup.add(directory)
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    receiver, sender = context.Pipe(duplex=False)
    writer = context.Process(target=_writer, args=(str(tmp_path), sid, published.name, ready, release, sender))
    writer.start()
    sender.close()
    try:
        assert ready.wait(10), "Writer did not fsync its partial"
        partials = list(journal.Layout(tmp_path).journal.glob("*.json.partial"))
        assert len(partials) == 1
        if reader == "scanner":
            snapshot = LibraryScanner(tmp_path).scan()
            assert len(snapshot.meetings) == 1, "An uncommitted delete must not hide the meeting"
        else:
            cleanup.retry()
        survived = partials[0].exists()
    finally:
        release.set()
    assert receiver.poll(10), "Writer did not report its rename result"
    succeeded, error = receiver.recv()
    receiver.close()
    writer.join(timeout=10)
    assert not writer.is_alive(), "Journal writer did not exit"
    assert survived and succeeded, f"Read-only scan removed the writer's partial: {error}"
    assert writer.exitcode == 0
    # The committed delete is still respected by the following scan.
    assert not LibraryScanner(tmp_path).scan().meetings


def test_only_locked_recovery_cleans_an_abandoned_journal_partial(tmp_path):
    sid, _directory = simple_session(tmp_path, 0.1)
    finalize.finalize(tmp_path, sid)
    partial = journal.Layout(tmp_path).journal / "abandoned.json.partial"
    partial.write_bytes(b"An interrupted, uncommitted write")
    assert len(LibraryScanner(tmp_path).scan().meetings) == 1
    assert partial.read_bytes() == b"An interrupted, uncommitted write"
    journal.recover(tmp_path)
    assert not partial.exists()
    assert len(LibraryScanner(tmp_path).scan().meetings) == 1
