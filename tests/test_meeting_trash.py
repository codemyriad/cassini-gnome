# SPDX-License-Identifier: GPL-3.0-or-later
"""Move to Trash, Undo/Restore and purge through the app API (D100, D-1032), isolated roots.

The meeting moves into the Library's own `.cassini/trash/<entry-id>/` (S-12 §7.1): the system
Trash and the Trash portal are never called, so Undo works the same in the Flatpak.
"""

import contextlib
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_library import capture
from test_voice_store import enrol

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:
    pytest.skip("GIO/Adwaita available in distro jobs", allow_module_level=True)
from gi.repository import Gio, GLib  # noqa: E402

from cassini_talk import meeting_trash as module  # noqa: E402
from cassini_talk.library import LibraryScanner  # noqa: E402
from cassini_talk.library_v2 import journal  # noqa: E402
from cassini_talk.library_v2 import trash as storage  # noqa: E402
from cassini_talk.voice_store import VoiceStore  # noqa: E402


@pytest.fixture
def roots(tmp_path, monkeypatch):
    library = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(library))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = VoiceStore(tmp_path / "private" / "voices")

    def no_system_trash(*_args, **_kwargs):
        raise AssertionError("D100: a deleted meeting never goes through the system Trash or portal")

    monkeypatch.setattr(Gio.File, "trash", no_system_trash)
    return library, store


def fingerprint(directory):
    return {
        p.relative_to(directory).as_posix(): p.read_bytes() for p in sorted(Path(directory).rglob("*")) if p.is_file()
    }


def meeting_for(library, directory):
    return next(m for m in LibraryScanner(library).scan().meetings if m.directory == directory)


def test_v1_meeting_moves_whole_into_the_library_trash_and_restores_byte_identical(roots):
    library, store = roots
    session = capture(library)
    before = fingerprint(session.directory)
    meeting = meeting_for(library, session.directory)
    root, entry_id = module.trash_meeting(meeting)
    assert root == library.resolve() and not session.directory.exists()
    entry = library / ".cassini/trash" / entry_id
    assert fingerprint(entry / session.directory.name) == before
    assert not LibraryScanner(library).scan().meetings
    (row,) = storage.entries(library)
    assert (row.entry_id, row.title, row.duration_ms) == (entry_id, meeting.title, meeting.duration_ms)
    assert storage.restore(library, entry_id) is None
    assert fingerprint(session.directory) == before
    assert [m.directory for m in LibraryScanner(library).scan().meetings] == [session.directory]
    assert not storage.entries(library) and not os.listdir(library / ".cassini/journal")


def test_invalid_external_folder_is_refused_before_anything_moves(roots, tmp_path):
    _library, _store = roots
    external = capture(tmp_path / "outside")
    meeting = meeting_for(tmp_path / "outside", external.directory)
    with pytest.raises(ValueError, match="outside"):
        module.trash_meeting(meeting)
    assert external.directory.exists() and not (tmp_path / "outside/.cassini").exists()


def test_busy_capture_is_refused_and_nothing_changes(roots):
    from cassini_talk.recording_session import RecordingSession

    library, _store = roots
    session = RecordingSession(None, source="gnome")
    try:
        meeting = SimpleNamespace(
            directory=session.directory,
            published_file=None,
            origin="own",
            id=session.directory.name.split("-", 1)[1],
            title="Live",
            started_at_ms=None,
            duration_ms=None,
        )
        with pytest.raises(storage.Refused, match="activity lease"):
            module.trash_meeting(meeting)
        assert session.directory.exists() and not storage.entries(library)
    finally:
        session.finish()


def test_cancellation_exclusion_spans_the_move(roots):
    library, _store = roots
    session = capture(library)
    meeting = meeting_for(library, session.directory)
    calls = []
    original = storage.move_to_trash

    @contextlib.contextmanager
    def cancel(directory):
        assert directory == session.directory
        calls.append("cancelled-and-waited")
        yield
        calls.append("unblocked")

    def move(*args, **kwargs):
        assert calls == ["cancelled-and-waited"]
        calls.append("moved")
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(storage, "move_to_trash", move)
        module.trash_meeting(meeting, queue=SimpleNamespace(cancel_meeting=cancel))
    assert calls == ["cancelled-and-waited", "moved", "unblocked"]


def test_trash_keeps_private_prints_until_purge_and_never_unenrols(roots):
    library, store = roots
    session = capture(library)
    enrol(store, session.directory)
    cache = store.root / "meetings" / (session.directory.name + ".json")
    root, entry_id = module.trash_meeting(meeting_for(library, session.directory))
    assert cache.exists(), "a restorable meeting keeps its private prints"
    identity = storage.entries(root)[0].identity
    storage.purge(root, entry_id)
    module.forget_purged(store, root, [identity])
    assert not cache.exists()
    assert store.snapshot()["voices"][0]["name"] == "Alice"
    assert not list((library / ".cassini/trash").iterdir())


def test_unavailable_library_root_moves_nothing(roots):
    library, store = roots
    session = capture(library)
    meeting = meeting_for(library, session.directory)
    library.rename(library.with_name("disconnected-library"))
    with pytest.raises((OSError, ValueError, storage.Refused)):
        module.trash_meeting(meeting)
    assert (library.with_name("disconnected-library") / session.directory.name).exists()


class Writer:
    def __init__(self):
        from concurrent.futures import Future

        self.future = Future()
        self.waited = threading.Event()
        original = self.future.result

        def result(*args, **kwargs):
            self.waited.set()
            return original(*args, **kwargs)

        self.future.result = result


def controller_for(app):
    from concurrent.futures import ThreadPoolExecutor

    controller = module.MeetingTrash.__new__(module.MeetingTrash)
    controller.app, controller.cleanup = app, None
    controller.hidden, controller.operations, controller.errors, controller.absent = {}, {}, {}, set()
    controller.toast, controller.waiters, controller.failed_flush = None, [], False
    controller.listeners = []
    controller.executor = ThreadPoolExecutor(1)
    controller._refresh = lambda: None
    return controller


def drain(controller, until):
    """Event-gated: finish queued worker jobs, then dispatch their GTK callbacks (no sleeps)."""
    context = GLib.MainContext.default()
    for _ in range(20):
        controller.executor.submit(lambda: None).result(timeout=10)
        while context.pending():
            context.iteration(False)
        if until():
            return
    assert until()


def test_move_waits_for_closed_writers_off_the_ui_thread_and_undo_restores(tmp_path, monkeypatch):
    speaker, export = Writer(), Writer()
    meeting = SimpleNamespace(
        directory=tmp_path / "Recording-12345678-1234-4234-8234-123456789abc", key="k", title="Weekly sync"
    )
    page = SimpleNamespace(
        meeting=meeting, _speaker_future=None, speakers_dialog=SimpleNamespace(closed=True, future=speaker.future)
    )
    toasts = []
    window = SimpleNamespace(
        meeting_page=page,
        _loaded_directory=None,
        snapshot=None,
        export_dialog=SimpleNamespace(closed=True, future=export.future, meeting=meeting),
        back_to_library=lambda: None,
        toasts=SimpleNamespace(add_toast=toasts.append),
        present=lambda: None,
        search_page=None,
        recording_page=None,
        _external_meeting=None,
        _pending_selection=None,
    )
    holds = []
    app = SimpleNamespace(
        get_windows=lambda: [window],
        settings_dialog=None,
        comfort=None,
        transcriber=None,
        hold=lambda: holds.append(True),
        release=lambda: holds.pop(),
        discard_meeting_paths=lambda _: None,
        _quitting=False,
        library_relocator=None,
    )
    meeting.lifecycle = "saved"
    controller = controller_for(app)
    moved, restored = threading.Event(), []

    def trash_meeting(m, **_kwargs):
        assert speaker.future.done() and export.future.done(), "the move ran before a closed writer finished"
        moved.set()
        return tmp_path, "5d0c3a7e-0000-4000-8000-0000000000e1"

    monkeypatch.setattr(module, "trash_meeting", trash_meeting)
    monkeypatch.setattr(storage, "restore", lambda root, entry: restored.append((root, entry)))
    try:
        controller.request(meeting, window)
        assert controller.hides(meeting.directory) and len(toasts) == 1 and toasts[0].get_timeout() == 5
        assert speaker.waited.wait(2) and not moved.is_set()
        speaker.future.set_result(None)
        assert export.waited.wait(2) and not moved.is_set()
        export.future.set_exception(RuntimeError("generated already-closed export failure"))
        assert moved.wait(2), "a failed-but-finished writer prevented the move"
        drain(controller, lambda: not controller.operations)
        assert controller.hides(meeting.directory) and not holds
        toasts[0].emit("button-clicked")
        drain(controller, lambda: restored and not controller.operations)
        assert restored == [(tmp_path, "5d0c3a7e-0000-4000-8000-0000000000e1")]
        assert not controller.hides(meeting.directory) and window._pending_selection == "k" and not holds
    finally:
        controller.executor.shutdown(wait=True)


@pytest.mark.parametrize("failure", [False, True])
def test_flush_reports_failure_and_never_calls_success_on_failure(tmp_path, failure):
    from concurrent.futures import Future

    held, called = [True], []
    meeting = SimpleNamespace(directory=tmp_path / "Recording-test", key="test")
    toast = SimpleNamespace(dismiss=lambda: None)
    window = SimpleNamespace(present=lambda: called.append("present"), _pending_selection=None)
    operation = module.PendingTrash(meeting, window, toast=toast)
    app = SimpleNamespace(get_windows=lambda: [], discard_meeting_paths=lambda _: None, release=lambda: held.pop())
    trash = controller_for(app)
    trash.operations = {meeting.directory: operation}
    assert trash.flush(lambda: called.append("success"), failed=lambda: called.append("failed"))
    result = Future()
    if failure:
        result.set_exception(OSError("generated Trash write failure"))
    else:
        result.set_result((tmp_path, "5d0c3a7e-0000-4000-8000-0000000000e1"))
    trash._moved(operation, result)
    assert called == (["present", "failed"] if failure else ["success"])
    assert not held and not trash.waiters and not trash.operations and not trash.failed_flush
    assert trash.errors == ({meeting.directory.absolute(): "failed"} if failure else {})
    trash.executor.shutdown(wait=True)


def test_startup_maintenance_recovers_and_expires(roots, monkeypatch):
    library, store = roots
    session = capture(library)
    root, entry_id = module.trash_meeting(meeting_for(library, session.directory))
    controller = controller_for(SimpleNamespace(voice_store=store))
    real_expire = storage.expire
    monkeypatch.setattr(storage, "expire", lambda r: real_expire(r, storage.parse_utc("2026-01-01T00:00:00Z")))
    controller._maintain()
    assert [e.entry_id for e in storage.entries(root)] == [entry_id], "not 30 days yet"
    monkeypatch.setattr(storage, "expire", lambda r: real_expire(r, storage.parse_utc("2099-01-01T00:00:00Z")))
    controller._maintain()
    assert not storage.entries(root) and not journal.pending(journal.Layout(root)).entries
    assert not (library / session.directory.name).exists()
    controller.executor.shutdown(wait=True)
