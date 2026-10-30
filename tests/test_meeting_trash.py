# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual Trash API and recoverable private bookkeeping, only in isolated roots."""

import contextlib
import json
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
from gi.repository import GLib  # noqa: E402

from cassini_viewer.meeting_trash import trash_owned  # noqa: E402
from cassini_viewer.trash_cleanup import TrashCleanup  # noqa: E402
from cassini_viewer.voice_store import VoiceStore  # noqa: E402


@pytest.fixture
def roots(tmp_path, monkeypatch):
    library = tmp_path / "Library"
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(library))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = VoiceStore(tmp_path / "private" / "voices")
    return library, store


def test_native_gio_trash_keeps_the_whole_folder_and_original_bytes(roots):
    library, store = roots
    session = capture(library)
    before = {p.name: p.read_bytes() for p in session.directory.iterdir()}
    native = Path(GLib.get_user_data_dir()) / "Trash" / "files"
    prior = set(native.iterdir()) if native.exists() else set()
    trash_owned(session.directory, cleanup=TrashCleanup(store))
    assert not session.directory.exists()
    candidates = set(native.iterdir()) - prior
    restored = next(p for p in candidates if p.name.startswith(session.directory.name))
    assert {p.name: p.read_bytes() for p in restored.iterdir()} == before


def test_invalid_external_folder_does_not_reach_native_trash(roots, tmp_path):
    _library, store = roots
    external = capture(tmp_path / "outside")
    called = []
    with pytest.raises(ValueError):
        trash_owned(external.directory, voices=store, trash=lambda p: called.append(p))
    assert external.directory.exists() and not called


def test_failed_native_trash_keeps_audio_and_prints(roots):
    library, store = roots
    session = capture(library)
    enrol(store, session.directory)
    before = {p.name: p.read_bytes() for p in session.directory.iterdir()}
    cache = store.root / "meetings" / (session.directory.name + ".json")
    original = cache.read_bytes()
    cleanup = TrashCleanup(store)
    with pytest.raises(OSError):
        trash_owned(session.directory, cleanup=cleanup, trash=lambda _: False)
    assert {p.name: p.read_bytes() for p in session.directory.iterdir()} == before
    assert cache.read_bytes() == original
    assert json.loads(cleanup.path.read_text())["pending"] == []


def test_busy_capture_cannot_be_trashed(roots):
    from cassini_viewer.recording_session import RecordingSession

    library, store = roots
    session = RecordingSession(None, source="gnome")
    called = []
    try:
        with pytest.raises(ValueError, match="still recording"):
            trash_owned(session.directory, voices=store, trash=lambda p: called.append(p))
        assert session.directory.exists() and not called
    finally:
        session.finish()


@pytest.mark.parametrize("already_missing", [False, True])
def test_private_cleanup_failure_after_success_is_durable_and_retryable(roots, tmp_path, monkeypatch, already_missing):
    library, store = roots
    session = capture(library)
    enrol(store, session.directory)
    cleanup = TrashCleanup(store)
    original = store.remove_meeting
    monkeypatch.setattr(store, "remove_meeting", lambda _: (_ for _ in ()).throw(OSError("generated read-only cache")))

    def native(path):
        path.rename(tmp_path / "private-system-trash")
        return True

    if already_missing:
        session.directory.rename(tmp_path / "private-system-trash")
    trash_owned(session.directory, cleanup=cleanup, trash=native)
    assert not session.directory.exists()
    assert json.loads(cleanup.path.read_text())["pending"] == [str(session.directory)]
    monkeypatch.setattr(store, "remove_meeting", original)
    cleanup.retry()
    assert not (store.root / "meetings" / (session.directory.name + ".json")).exists()
    assert json.loads(cleanup.path.read_text())["pending"] == []
    assert store.snapshot()["voices"][0]["name"] == "Alice"


def test_cancellation_exclusion_spans_trash_and_private_cleanup(roots, tmp_path):
    library, store = roots
    session = capture(library)
    calls = []

    @contextlib.contextmanager
    def cancel(directory):
        assert directory == session.directory
        calls.append("cancelled-and-waited")
        yield
        calls.append("unblocked")

    def native(path):
        assert calls == ["cancelled-and-waited"]
        path.rename(tmp_path / "private-system-trash")
        calls.append("native-trash")
        return True

    trash_owned(session.directory, queue=SimpleNamespace(cancel_meeting=cancel), voices=store, trash=native)
    assert calls == ["cancelled-and-waited", "native-trash", "unblocked"]


def test_hidden_save_after_trash_is_a_removed_path_not_an_error(roots, tmp_path):
    from cassini_viewer.capture_guard import queue_saved_for_later

    library, store = roots
    session = capture(library)

    def native(path):
        path.rename(tmp_path / "private-system-trash")
        return True

    trash_owned(session.directory, voices=store, trash=native)
    assert queue_saved_for_later(str(session.directory)) is None
    assert not session.directory.exists()


def test_trash_drains_closed_speaker_and_export_writers_off_the_caller(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import Future, ThreadPoolExecutor

    from cassini_viewer import meeting_trash as module

    class Writer(Future):
        def __init__(self):
            super().__init__()
            self.waited = threading.Event()

        def result(self, *args, **kwargs):
            self.waited.set()
            return super().result(*args, **kwargs)

    speaker, export = Writer(), Writer()
    meeting = SimpleNamespace(directory=tmp_path / "Recording-12345678-1234-4234-8234-123456789abc")
    page = SimpleNamespace(
        meeting=meeting, _speaker_future=None, speakers_dialog=SimpleNamespace(closed=True, future=speaker)
    )
    window = SimpleNamespace(
        meeting_page=page,
        _loaded_directory=None,
        snapshot=None,
        export_dialog=SimpleNamespace(closed=True, future=export, meeting=meeting),
        back_to_library=lambda: None,
    )
    holds, removed = [], threading.Event()
    app = SimpleNamespace(
        get_windows=lambda: [window],
        settings_dialog=None,
        comfort=None,
        transcriber=None,
        hold=lambda: holds.append(True),
        release=lambda: holds.pop(),
        discard_meeting_paths=lambda _: None,
    )
    controller = module.MeetingTrash.__new__(module.MeetingTrash)
    controller.app, controller.cleanup = app, None
    controller.hidden, controller.operations, controller.errors = {}, {}, {}
    controller.toast, controller.waiters, controller.failed_flush = None, [], False
    controller.executor = ThreadPoolExecutor(1)
    operation = module.PendingTrash(meeting, window, waits=controller._prepare(meeting))
    controller.operations[meeting.directory] = operation

    def native(*_args, **_kwargs):
        assert speaker.done() and export.done(), "Trash ran before a closed writer finished"
        removed.set()
        return meeting.directory

    monkeypatch.setattr(module, "trash_owned", native)
    try:
        # A pending writer must not block the UI-side commit call.
        with ThreadPoolExecutor(1) as caller:
            caller.submit(controller.commit, operation).result(timeout=2)
        assert speaker.waited.wait(2) and not removed.is_set()
        speaker.set_result(None)
        assert export.waited.wait(2) and not removed.is_set()
        export.set_exception(RuntimeError("generated already-closed export failure"))
        assert removed.wait(2), "a failed-but-finished writer prevented Trash"
        controller.executor.shutdown(wait=True)
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        assert not holds and not controller.operations
    finally:
        for future in (speaker, export):
            if not future.done():
                future.set_result(None)
        caller.shutdown(wait=True)
        controller.executor.shutdown(wait=True)


def test_unavailable_library_root_does_not_discard_private_prints(roots):
    library, store = roots
    session = capture(library)
    enrol(store, session.directory)
    cache = store.root / "meetings" / (session.directory.name + ".json")
    before = cache.read_bytes()
    library.rename(library.with_name("disconnected-library"))
    with pytest.raises(FileNotFoundError, match="unavailable"):
        trash_owned(session.directory, cleanup=TrashCleanup(store))
    assert cache.read_bytes() == before


@pytest.mark.parametrize("failure", [False, True])
def test_flush_reports_failure_to_restart_and_never_calls_success_on_failure(tmp_path, failure):
    from concurrent.futures import Future

    from cassini_viewer.meeting_trash import MeetingTrash

    held, called = [True], []
    meeting = SimpleNamespace(directory=tmp_path / "Recording-test", key="test")
    window = SimpleNamespace(present=lambda: called.append("present"))
    operation = SimpleNamespace(meeting=meeting, window=window, phase="committing")
    app = SimpleNamespace(get_windows=lambda: [], discard_meeting_paths=lambda _: None, release=lambda: held.pop())
    trash = MeetingTrash.__new__(MeetingTrash)
    trash.app = app
    trash.operations = {meeting.directory: operation}
    trash.waiters, trash.errors, trash.hidden = [], {}, {}
    trash.failed_flush = False
    trash._refresh = lambda: None
    assert trash.flush(lambda: called.append("success"), failed=lambda: called.append("failed"))
    result = Future()
    if failure:
        result.set_exception(OSError("generated Trash write failure"))
    else:
        result.set_result(None)
    trash._complete(operation, result)
    assert called == (["present", "failed"] if failure else ["success"])
    assert not held and not trash.waiters and not trash.operations and not trash.failed_flush
