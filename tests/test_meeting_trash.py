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


def test_private_cleanup_failure_after_success_is_durable_and_retryable(roots, tmp_path, monkeypatch):
    library, store = roots
    session = capture(library)
    enrol(store, session.directory)
    cleanup = TrashCleanup(store)
    original = store.remove_meeting
    monkeypatch.setattr(store, "remove_meeting", lambda _: (_ for _ in ()).throw(OSError("generated read-only cache")))

    def native(path):
        path.rename(tmp_path / "private-system-trash")
        return True

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
