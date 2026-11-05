# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved-on-exit intent uses the same metadata transaction as the queue."""

import pytest
from test_library import capture, write_metadata

from cassini_talk.capture_guard import queue_saved_for_later
from cassini_talk.meeting_store import LOCK, read_metadata
from cassini_talk.recording_session import RecordingSession
from cassini_talk.transcription import REFERENCE

WITHOUT_GI = """
import ast, importlib, sys
from importlib.abc import MetaPathFinder
from pathlib import Path
from types import SimpleNamespace


class NoGi(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "gi" or name.startswith("gi."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)


sys.meta_path.insert(0, NoGi())
package = Path(sys.argv[1])
modules = set()
for owner in ("capture_guard", "installed_build", "build_refresh"):
    modules.add(owner)
    for node in ast.walk(ast.parse((package / (owner + ".py")).read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            modules.update([node.module] if node.module else [alias.name for alias in node.names])
for module in sorted(modules):
    importlib.import_module("cassini_talk." + module)
from cassini_talk.capture_guard import capture_targets

app = SimpleNamespace(get_windows=lambda: [], _browser_snapshot={}, _browser_answered_at=None)
assert capture_targets(app) == ([], False)
print(" ".join(sorted(modules)))
"""


def test_quit_and_build_checks_import_without_pygobject():
    """CI's minimal environment has no PyGObject. capture_guard and
    installed_build (Quit's and the build refresh's capture checks) need no
    GLib, yet capture_targets' lazy import of capture_activity loaded GLib at
    module level and failed there (ModuleNotFoundError: gi). Every module those
    three import, at module level or inside a function, must import without gi."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import cassini_talk

    package = Path(cassini_talk.__file__).parent
    env = dict(os.environ, PYTHONPATH=str(package.parent))
    result = subprocess.run(
        [sys.executable, "-c", WITHOUT_GI, str(package)], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "capture_activity" in result.stdout.split()


def test_close_save_does_not_queue_empty_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = RecordingSession(tmp_path, source="gnome")
    session.add_track("mic", "Me", "microphone", 48000, 1)
    session.finish()
    queue_saved_for_later(session.directory)
    assert read_metadata(session.directory)["transcript"]["status"] == "none"


def test_close_save_reads_and_updates_under_queue_lock(tmp_path, monkeypatch):
    from cassini_talk import meeting_store

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = capture(tmp_path)
    write_metadata(session.directory, transcript=dict(status="failed", reference=REFERENCE))
    original = meeting_store.read_metadata
    reads = []

    def locked_read(directory):
        assert LOCK._is_owned(), "Read-modify-write must share the transcription lock"
        reads.append(directory)
        return original(directory)

    monkeypatch.setattr(meeting_store, "read_metadata", locked_read)
    queue_saved_for_later(session.directory)
    result = original(session.directory)
    assert reads and result["transcript"] == dict(status="queued", reference=REFERENCE)
    assert result["error"] is None and result["etaMinutes"] is None


def test_close_save_never_overwrites_newer_running_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = capture(tmp_path)
    write_metadata(session.directory, transcript=dict(status="running", reference=REFERENCE))
    before = (session.directory / "meeting.json").read_bytes()
    queue_saved_for_later(session.directory)
    assert (session.directory / "meeting.json").read_bytes() == before


def test_concurrent_browser_stop_is_already_successful(tmp_path, monkeypatch):
    from cassini_talk import browser_service
    from cassini_talk.capture_guard import stop_browser_captures

    path = str(tmp_path / "Recording-one")
    active = True

    def request(action="status", **values):
        nonlocal active
        if action == "stop":
            active = False
            raise ValueError("This meeting is not recording")
        return dict(statuses=[dict(id="one:2", path=path, recording=active)], meetings=[])

    monkeypatch.setattr(browser_service, "request", request)
    assert stop_browser_captures() == [path]


@pytest.mark.parametrize("meeting_present", [True, False])
def test_stop_waits_for_an_existing_browser_save(tmp_path, monkeypatch, meeting_present):
    from cassini_talk import browser_service
    from cassini_talk.capture_guard import stop_browser_captures

    path = str(tmp_path / "Recording-one")
    attempts = []

    def request(action="status", **values):
        if action == "stop":
            attempts.append(values["id"])
            if len(attempts) == 1:
                raise ValueError("A recording command is still being processed")
        active = len(attempts) < 2
        return dict(statuses=[dict(id="one:2", path=path, recording=active)], meetings=[dict(id="one:2", busy=active)])

    monkeypatch.setattr(browser_service, "request", request)
    assert stop_browser_captures() == [path]
    assert len(attempts) == 2


def test_remember_browser_paths_before_library_scan_finishes(tmp_path):
    from types import SimpleNamespace

    from cassini_talk.capture_guard import remember_capture_paths

    path = str(tmp_path / "Recording-new")
    window = SimpleNamespace(_activity_source=SimpleNamespace(paths={"new": path}, snapshot={"new": "recording"}))
    app = SimpleNamespace(get_windows=lambda: [window])
    assert remember_capture_paths(app) == {path}


@pytest.mark.parametrize("reason", ["user", "disk", "error", "appQuit"])
def test_browser_stop_records_the_same_requested_cause_in_optional_details(tmp_path, monkeypatch, reason):
    from cassini_talk import browser_service
    from cassini_talk.capture_guard import stop_browser_captures

    monkeypatch.setenv("CASSINI_LIBRARY_DIR", str(tmp_path))
    session = capture(tmp_path)
    path = str(session.directory)

    def request(action="status", **values):
        if action == "stop-all":
            assert values["reason"] == reason
            return dict(statuses=[], stoppedPaths=[path])
        return dict(protocol=2, statuses=[dict(recording=True, path=path)])

    monkeypatch.setattr(browser_service, "request", request)
    assert stop_browser_captures(reason=reason) == [path]
    assert read_metadata(session.directory)["recordingDetails"]["end"]["reason"] == reason


@pytest.mark.parametrize("owner", ["application", "window"])
def test_stopping_browser_capture_is_busy_and_its_path_is_retained(tmp_path, owner):
    from types import SimpleNamespace

    from cassini_talk.capture_guard import capture_targets, remember_capture_paths

    path = str(tmp_path / "Recording-12345678-1234-1234-1234-123456789abc")
    statuses = dict(statuses=[dict(recording=False, stopping=True, path=path)])
    window = SimpleNamespace(_browser_snapshot=statuses if owner == "window" else {})
    app = SimpleNamespace(
        get_windows=lambda: [window], native_captures={}, _browser_snapshot=statuses if owner == "application" else {}
    )
    assert capture_targets(app) == ([], True)
    assert remember_capture_paths(app) == {path}
